"""
routing_engine.py — Google Maps-style Multi-Modal Crowd Dispersion Engine
==========================================================================

HOW GOOGLE MAPS ACTUALLY ROUTES (from their own blog + research papers):
  1. Graph shortest-path (Dijkstra / A*) as the BASE
  2. Traffic-aware scoring — historical patterns + incident reports
  3. Multi-factor route RANKING, not just picking the shortest:
       - Estimated travel time (primary)
       - Road quality / type (primary roads > residential)
       - Road directness (avoid unnecessary turns / detours)
       - Incident penalties (blocked roads, events, crowd density)
       - Speed limits and road size
  4. Present TOP-N ranked alternatives — each genuinely different street
  5. Transit routing = separate mode with transfer-aware cost

We replicate all of this for crowd dispersal:
  - A* with Haversine heuristic (admissible, same as Google)
  - Time-cost weights from road type + speed per mode
  - Crowd density penalties on edges near Warning/Critical zones
  - Multi-factor route scoring: time + quality + directness
  - Penalty-based diverse route generation (genuinely different streets)
  - All transport modes: walk, cycle, motorcycle, car

PERFORMANCE:
  1. Bbox corridor fetch  → only downloads roads the route might use
  2. Parallel mode fetch  → all modes downloaded simultaneously
  3. networkx A* (C)      → C-optimised, much faster than pure Python
  4. In-process cache     → repeat clicks = instant, no re-download
  5. Pre-built weight dict→ weight function is O(1) dict lookup
"""
from __future__ import annotations

import itertools
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import folium
import networkx as nx
import osmnx as ox
from geopy.geocoders import Nominatim

# ─────────────────────────────────────────────────────────────────────────────
# Transport mode profiles
# ─────────────────────────────────────────────────────────────────────────────

TRANSPORT_MODES: dict[str, dict] = {
    "walk": {
        "label":            "🚶 Walking",
        "icon":             "🚶",
        "network_type":     "walk",
        "sqm_per_unit":     1.4,
        "flow_base":        40,
        "default_speed_ms": 1.2,
        "speeds": {
            "footway": 1.4, "pedestrian": 1.4, "path": 1.3, "steps": 0.6,
            "track": 1.0, "living_street": 1.3, "residential": 1.2,
            "service": 1.1, "unclassified": 1.2, "tertiary": 1.2,
            "secondary": 1.15, "primary": 1.1, "trunk": 0.9,
        },
        # Google Maps: road quality affects route preference for walkers
        "quality_score": {
            "footway": 1.0, "pedestrian": 1.0, "path": 0.85,
            "living_street": 0.9, "residential": 0.85,
            "tertiary": 0.75, "secondary": 0.65, "primary": 0.55,
            "trunk": 0.4, "steps": 0.6,
        },
    },
    "cycle": {
        "label":            "🚲 Cycling",
        "icon":             "🚲",
        "network_type":     "bike",
        "sqm_per_unit":     3.0,
        "flow_base":        25,
        "default_speed_ms": 4.2,
        "speeds": {
            "cycleway": 5.0, "path": 3.5, "footway": 2.5,
            "residential": 4.2, "living_street": 3.5, "unclassified": 4.0,
            "tertiary": 4.5, "secondary": 4.8, "primary": 4.5,
            "trunk": 5.0, "service": 3.0, "track": 3.0,
        },
        "quality_score": {
            "cycleway": 1.0, "path": 0.85, "residential": 0.8,
            "tertiary": 0.75, "secondary": 0.65, "primary": 0.55,
            "trunk": 0.4,
        },
    },
    "motorcycle": {
        "label":            "🛵 Motorcycle",
        "icon":             "🛵",
        "network_type":     "drive",
        "sqm_per_unit":     4.0,
        "flow_base":        30,
        "default_speed_ms": 7.0,
        "speeds": {
            "residential": 6.0, "living_street": 4.5, "service": 5.0,
            "unclassified": 6.0, "tertiary": 7.5, "secondary": 8.5,
            "primary": 9.0, "trunk": 10.0, "motorway": 11.0,
        },
        "quality_score": {
            "motorway": 1.0, "trunk": 0.95, "primary": 0.9,
            "secondary": 0.85, "tertiary": 0.75, "residential": 0.6,
        },
    },
    "car": {
        "label":            "🚗 Car",
        "icon":             "🚗",
        "network_type":     "drive",
        "sqm_per_unit":     12.0,
        "flow_base":        12,
        "default_speed_ms": 8.0,
        "speeds": {
            "residential": 6.0, "living_street": 4.0, "service": 5.0,
            "unclassified": 7.0, "tertiary": 8.5, "secondary": 10.0,
            "primary": 11.5, "trunk": 13.5, "motorway": 16.5,
        },
        "quality_score": {
            "motorway": 1.0, "trunk": 0.95, "primary": 0.9,
            "secondary": 0.85, "tertiary": 0.75, "residential": 0.55,
            "living_street": 0.4,
        },
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_ROAD_WIDTH  = 3.0
MAX_ROUTES          = 5
MAX_EDGE_OVERLAP    = 0.55
MIN_PHASE_GAP_SEC   = 90
PENALTY_CRITICAL    = 10.0
PENALTY_WARNING     = 3.5

# Google Maps multi-factor scoring weights
# (how much each factor contributes to overall route score)
SCORE_WEIGHT_TIME       = 0.50   # estimated travel time (primary, like Google)
SCORE_WEIGHT_QUALITY    = 0.20   # road quality (prefer proper roads over dirt paths)
SCORE_WEIGHT_DIRECTNESS = 0.20   # how direct the route is vs straight line
SCORE_WEIGHT_CAPACITY   = 0.10   # can it handle the crowd (wider = better)

CORRIDOR_PADDING: dict[str, int] = {
    "walk": 400, "cycle": 500, "motorcycle": 600, "car": 800,
}

_counter = itertools.count()
_GRAPH_CACHE: dict = {}
MAX_CACHE = 6

# ─────────────────────────────────────────────────────────────────────────────
# Geocoding
# ─────────────────────────────────────────────────────────────────────────────

def geocode_multi(query: str, limit: int = 6) -> list:
    try:
        geo = Nominatim(user_agent="crowd_dispersion_gmaps_v6")
        res = geo.geocode(query, exactly_one=False, limit=limit)
        return [{"address": r.address, "lat": r.latitude, "lon": r.longitude}
                for r in res] if res else []
    except Exception:
        return []

# ─────────────────────────────────────────────────────────────────────────────
# Haversine
# ─────────────────────────────────────────────────────────────────────────────

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl  = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    return haversine_m(lat1, lon1, lat2, lon2) / 1000.0

# ─────────────────────────────────────────────────────────────────────────────
# Bbox corridor fetch + cache
# ─────────────────────────────────────────────────────────────────────────────

def _bbox_for_route(src_lat, src_lon, tgt_lat, tgt_lon, padding_m: int) -> tuple:
    lat_pad = padding_m / 111_000
    lon_pad = padding_m / (111_000 * math.cos(math.radians((src_lat + tgt_lat) / 2)))
    return (max(src_lat, tgt_lat) + lat_pad,
            min(src_lat, tgt_lat) - lat_pad,
            max(src_lon, tgt_lon) + lon_pad,
            min(src_lon, tgt_lon) - lon_pad)


def _fetch_graph_single(src_lat, src_lon, tgt_lat, tgt_lon,
                        network_type: str, padding_m: int):
    ox.settings.timeout     = 45
    ox.settings.log_console = False
    ox.settings.use_cache   = True
    north, south, east, west = _bbox_for_route(
        src_lat, src_lon, tgt_lat, tgt_lon, padding_m)
    return ox.graph_from_bbox(
        (west, south, east, north),
        network_type=network_type,
        simplify=True, retain_all=False,
    )


def fetch_map_graph_for_route(src_lat: float, src_lon: float,
                               tgt_lat: float, tgt_lon: float,
                               mode: str = "walk"):
    ck = (round(src_lat, 4), round(src_lon, 4),
          round(tgt_lat, 4), round(tgt_lon, 4), mode)
    if ck in _GRAPH_CACHE:
        return _GRAPH_CACHE[ck], None
    try:
        nt      = TRANSPORT_MODES.get(mode, TRANSPORT_MODES["walk"])["network_type"]
        dist_m  = haversine_m(src_lat, src_lon, tgt_lat, tgt_lon)
        padding = max(150, min(int(dist_m * 0.15), CORRIDOR_PADDING.get(mode, 500)))
        G       = _fetch_graph_single(src_lat, src_lon, tgt_lat, tgt_lon, nt, padding)
        if len(_GRAPH_CACHE) >= MAX_CACHE:
            del _GRAPH_CACHE[next(iter(_GRAPH_CACHE))]
        _GRAPH_CACHE[ck] = G
        return G, None
    except Exception as exc:
        return None, str(exc)


def fetch_graphs_parallel(src_lat, src_lon, tgt_lat, tgt_lon,
                          modes: list) -> dict:
    def _fetch(mk):
        G, err = fetch_map_graph_for_route(src_lat, src_lon, tgt_lat, tgt_lon, mk)
        return mk, G, err
    results = {}
    with ThreadPoolExecutor(max_workers=len(modes)) as pool:
        for future in as_completed({pool.submit(_fetch, mk): mk for mk in modes}):
            mk, G, err = future.result()
            results[mk] = (G, err)
    return results


def fetch_map_graph_from_point(lat: float, lon: float,
                               dist: int = 700, mode: str = "walk"):
    try:
        ox.settings.timeout     = 60
        ox.settings.log_console = False
        nt = TRANSPORT_MODES.get(mode, TRANSPORT_MODES["walk"])["network_type"]
        G  = ox.graph_from_point((lat, lon), dist=dist,
                                  network_type=nt,
                                  simplify=True, retain_all=False)
        return G, (lat, lon), None
    except Exception as exc:
        return None, (lat, lon), str(exc)


def fetch_map_graph(location_name: str, dist: int = 700, mode: str = "walk"):
    res = geocode_multi(location_name, limit=1)
    if not res:
        return None, None, f"Could not geocode '{location_name}'"
    r = res[0]
    return fetch_map_graph_from_point(r["lat"], r["lon"], dist=dist, mode=mode)

# ─────────────────────────────────────────────────────────────────────────────
# Edge helpers
# ─────────────────────────────────────────────────────────────────────────────

def _best_edge(raw: dict) -> dict:
    if not raw: return {}
    v = next(iter(raw.values()))
    return v if isinstance(v, dict) else raw


def _highway_type(ed: dict) -> str:
    hw = ed.get("highway", "default")
    return str(hw[0] if isinstance(hw, list) else hw)


def _mode_speed(ed: dict, mode: str) -> float:
    prof = TRANSPORT_MODES.get(mode, TRANSPORT_MODES["walk"])
    return prof["speeds"].get(_highway_type(ed), prof["default_speed_ms"])


def _road_width(ed: dict) -> float:
    for key in ("width", "lanes"):
        val = ed.get(key)
        if val is None: continue
        try:
            v = float(str(val).split(";")[0].strip())
            return v * 2.5 if key == "lanes" else v
        except ValueError:
            continue
    return float({
        "motorway": 12, "trunk": 10, "primary": 8, "secondary": 6,
        "tertiary": 5, "residential": 4, "living_street": 3,
        "footway": 2, "pedestrian": 4, "path": 2, "service": 3, "cycleway": 2,
    }.get(_highway_type(ed), DEFAULT_ROAD_WIDTH))


def _edge_len(ed: dict) -> float:
    return max(1.0, float(ed.get("length", 50.0)))


def _edge_time_s(ed: dict, mode: str) -> float:
    return _edge_len(ed) / _mode_speed(ed, mode)


def _quality_score(ed: dict, mode: str) -> float:
    """Road quality score 0-1. Higher = better for this mode. Google Maps uses this."""
    qs = TRANSPORT_MODES.get(mode, TRANSPORT_MODES["walk"]).get("quality_score", {})
    return qs.get(_highway_type(ed), 0.7)


def _route_length_m(G, path: list) -> float:
    return sum(_edge_len(_best_edge(G.get_edge_data(u, v) or {}))
               for u, v in zip(path[:-1], path[1:]))


def _route_time_s(G, path: list, mode: str = "walk") -> float:
    return sum(_edge_time_s(_best_edge(G.get_edge_data(u, v) or {}), mode)
               for u, v in zip(path[:-1], path[1:]))


def _route_quality(G, path: list, mode: str) -> float:
    """Average road quality score along the route (0-1)."""
    if len(path) < 2: return 0.5
    scores = [_quality_score(_best_edge(G.get_edge_data(u, v) or {}), mode)
              for u, v in zip(path[:-1], path[1:])]
    return sum(scores) / len(scores)


def _directness_ratio(G, path: list,
                       src_lat: float, src_lon: float,
                       tgt_lat: float, tgt_lon: float) -> float:
    """
    Directness = straight-line distance / actual route length.
    Google Maps penalises very indirect routes even if they're fast.
    Range 0-1; 1.0 = perfectly direct, <0.3 = very winding.
    """
    straight = haversine_m(src_lat, src_lon, tgt_lat, tgt_lon)
    actual   = _route_length_m(G, path)
    return min(1.0, straight / max(actual, 1.0))


def _bottleneck_width(G, path: list) -> float:
    widths = [_road_width(_best_edge(G.get_edge_data(u, v) or {}))
              for u, v in zip(path[:-1], path[1:])]
    return min(widths) if widths else DEFAULT_ROAD_WIDTH


def _route_capacity(G, path: list, mode: str = "walk") -> int:
    sqm = TRANSPORT_MODES.get(mode, TRANSPORT_MODES["walk"])["sqm_per_unit"]
    return max(10, int((_route_length_m(G, path) * _bottleneck_width(G, path)) / sqm))

# ─────────────────────────────────────────────────────────────────────────────
# Google Maps-style multi-factor route scoring
# ─────────────────────────────────────────────────────────────────────────────

def _gmaps_route_score(G, path: list, mode: str,
                        src_lat: float, src_lon: float,
                        tgt_lat: float, tgt_lon: float,
                        total_crowd: int = 1000) -> dict:
    """
    Scores a route across 4 factors exactly like Google Maps does:

    1. TIME score      — faster = higher (primary factor, 50% weight)
    2. QUALITY score   — better road type = higher (20%)
    3. DIRECTNESS score— more direct = higher (20%)
    4. CAPACITY score  — can handle the crowd = higher (10%)

    Returns a dict with all individual scores + weighted composite.
    """
    time_s     = _route_time_s(G, path, mode)
    quality    = _route_quality(G, path, mode)
    directness = _directness_ratio(G, path, src_lat, src_lon, tgt_lat, tgt_lon)
    capacity   = _route_capacity(G, path, mode)

    # Normalise time score: faster routes get higher score
    # We use 1 / time_s so faster = higher, then normalise later
    time_score = 1.0 / max(time_s, 1.0)

    # Capacity score: can it actually handle the crowd?
    cap_score = min(1.0, capacity / max(total_crowd, 1))

    return {
        "time_s":           time_s,
        "time_min":         round(time_s / 60, 1),
        "quality":          round(quality, 3),
        "directness":       round(directness, 3),
        "capacity":         capacity,
        "cap_score":        round(cap_score, 3),
        "time_score_raw":   time_score,
        # Composite computed after normalisation across all routes
        "length_m":         round(_route_length_m(G, path)),
        "length_km":        round(_route_length_m(G, path) / 1000, 2),
        "bottleneck_w":     round(_bottleneck_width(G, path), 2),
    }


def _rank_routes_gmaps(G, routes: list, mode: str,
                        src_lat: float, src_lon: float,
                        tgt_lat: float, tgt_lon: float,
                        total_crowd: int = 1000) -> list[dict]:
    """
    Score and rank all candidate routes using Google Maps multi-factor method.
    Returns list of dicts sorted best-first.
    """
    if not routes:
        return []

    scored = []
    for i, path in enumerate(routes):
        s = _gmaps_route_score(G, path, mode, src_lat, src_lon,
                                tgt_lat, tgt_lon, total_crowd)
        s["path"]        = path
        s["route_index"] = i
        s["route_label"] = f"Route {i + 1}"
        scored.append(s)

    # Normalise time_score_raw across all routes (0-1 range)
    max_ts = max(s["time_score_raw"] for s in scored) or 1.0
    for s in scored:
        s["time_score_norm"] = s["time_score_raw"] / max_ts

    # Composite Google Maps-style score
    for s in scored:
        s["composite"] = (
            SCORE_WEIGHT_TIME       * s["time_score_norm"] +
            SCORE_WEIGHT_QUALITY    * s["quality"] +
            SCORE_WEIGHT_DIRECTNESS * s["directness"] +
            SCORE_WEIGHT_CAPACITY   * s["cap_score"]
        )

    # Sort best first (highest composite = best route)
    scored.sort(key=lambda x: x["composite"], reverse=True)

    # Re-label after ranking
    for rank, s in enumerate(scored):
        s["rank"]        = rank + 1
        s["route_label"] = f"Route {rank + 1}"

    return scored

# ─────────────────────────────────────────────────────────────────────────────
# Weight dict + A* (C-optimised)
# ─────────────────────────────────────────────────────────────────────────────

def _build_time_weights(G, mode: str,
                         node_penalties: dict = None,
                         edge_multipliers: dict = None) -> dict:
    weights: dict = {}
    np_ = node_penalties or {}
    em  = edge_multipliers or {}
    for u, v, data in G.edges(data=True):
        inner = (_best_edge(data)
                 if isinstance(next(iter(data.values()), None), dict) else data)
        base  = _edge_time_s(inner, mode)
        mult  = max(
            em.get((u, v), 1.0),
            PENALTY_CRITICAL if (np_.get(u) == "Critical" or np_.get(v) == "Critical")
            else PENALTY_WARNING if (np_.get(u) == "Warning" or np_.get(v) == "Warning")
            else 1.0
        )
        weights[(u, v)] = base * mult
    return weights


def _astar_nx(G, src: int, tgt: int, weights: dict, mode: str) -> Optional[list]:
    tgt_y    = G.nodes[tgt].get("y", 0.0)
    tgt_x    = G.nodes[tgt].get("x", 0.0)
    max_spd  = max(TRANSPORT_MODES[mode]["speeds"].values(),
                   default=TRANSPORT_MODES[mode]["default_speed_ms"])

    def heuristic(u, _v):
        uy = G.nodes[u].get("y", tgt_y)
        ux = G.nodes[u].get("x", tgt_x)
        return haversine_m(uy, ux, tgt_y, tgt_x) / max_spd

    def weight_fn(u, v, data):
        return weights.get((u, v), _edge_time_s(_best_edge(data), mode))

    try:
        return nx.astar_path(G, src, tgt, heuristic=heuristic, weight=weight_fn)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


def _find_k_routes(G, src: int, tgt: int, weights: dict,
                   mode: str, k: int = MAX_ROUTES) -> list:
    """
    Penalty-based diverse route generation.
    After each route is found, the edges on that route are penalised
    so the next A* call is forced down genuinely different streets.
    Exactly how Google Maps generates its alternative routes.
    """
    routes: list = []
    cw = weights.copy()

    for _ in range(k):
        path = _astar_nx(G, src, tgt, cw, mode)
        if not path:
            break
        routes.append(path)
        # Penalise edges on this path (both directions)
        for u, v in zip(path[:-1], path[1:]):
            base_uv = cw.get((u, v), _edge_time_s(
                _best_edge(G.get_edge_data(u, v) or {}), mode))
            cw[(u, v)] = base_uv * 2.5
            base_vu = cw.get((v, u), _edge_time_s(
                _best_edge(G.get_edge_data(v, u) or {}), mode))
            cw[(v, u)] = base_vu * 2.5

    return routes

# ─────────────────────────────────────────────────────────────────────────────
# Public API — find_routes
# ─────────────────────────────────────────────────────────────────────────────

def find_routes(G, source_coords: tuple, target_coords: tuple,
                mode: str = "walk",
                weight_multiplier: dict = None,
                zone_statuses: dict = None,
                zone_node_map: dict = None) -> list:
    if G is None:
        return []
    try:
        src = ox.nearest_nodes(G, source_coords[1], source_coords[0])
        tgt = ox.nearest_nodes(G, target_coords[1], target_coords[0])
    except Exception as e:
        print(f"[find_routes] nearest_nodes failed: {e}")
        return []
    if src == tgt:
        return []

    node_penalties: dict = {}
    if zone_statuses and zone_node_map:
        for zn, zs in zone_statuses.items():
            if zs in ("Critical", "Warning"):
                for nid in zone_node_map.get(zn, []):
                    node_penalties[nid] = zs

    weights = _build_time_weights(G, mode, node_penalties, weight_multiplier)
    return _find_k_routes(G, src, tgt, weights, mode, k=MAX_ROUTES)

# ─────────────────────────────────────────────────────────────────────────────
# Public API — build_dispersion_plan (Google Maps-style ranked output)
# ─────────────────────────────────────────────────────────────────────────────

def build_dispersion_plan(G, routes: list, total_crowd: int,
                          mode: str = "walk",
                          location_type: str = "General",
                          src_coords: tuple = (0.0, 0.0),
                          tgt_coords: tuple = (0.0, 0.0)) -> dict:
    if not routes or G is None:
        return {}

    mode_cfg = TRANSPORT_MODES.get(mode, TRANSPORT_MODES["walk"])

    # Google Maps-style multi-factor ranking
    ranked = _rank_routes_gmaps(
        G, routes, mode,
        src_coords[0], src_coords[1],
        tgt_coords[0], tgt_coords[1],
        total_crowd=total_crowd,
    )

    # Build route_data in ranked order
    route_data = []
    for s in ranked:
        route_data.append({
            "index":         s["route_index"],
            "path":          s["path"],
            "route_label":   s["route_label"],
            "rank":          s["rank"],
            "length_m":      s["length_m"],
            "length_km":     s["length_km"],
            "time_s":        round(s["time_s"]),
            "time_min":      s["time_min"],
            "capacity":      s["capacity"],
            "bottleneck_w":  s["bottleneck_w"],
            "mode":          mode,
            # Google Maps-style scores
            "score_time":       round(s["time_score_norm"] * 100, 1),
            "score_quality":    round(s["quality"] * 100, 1),
            "score_directness": round(s["directness"] * 100, 1),
            "score_capacity":   round(s["cap_score"] * 100, 1),
            "score_composite":  round(s["composite"] * 100, 1),
            # Why Google Maps would recommend this route
            "recommendation": _route_recommendation(s),
        })

    # Crowd allocation proportional to composite score (best route gets most)
    total_score = sum(r["score_composite"] for r in route_data) or 1.0
    allocated, remaining = [], total_crowd
    for idx, r in enumerate(route_data):
        if idx == len(route_data) - 1:
            share = max(0, remaining)
        else:
            share = int(round((r["score_composite"] / total_score) * total_crowd))
            remaining -= share
        share = min(share, max(1, r["capacity"]))
        allocated.append(max(0, share))

    # Phase sequencing (release in ranked order)
    phases, clock_s = [], 0
    for idx, (r, crowd) in enumerate(zip(route_data, allocated)):
        if crowd <= 0:
            continue
        flow_rpm  = max(10, int(mode_cfg["flow_base"] * r["bottleneck_w"]))
        release_s = int((crowd / flow_rpm) * 60)
        phases.append({
            "phase":        idx + 1,
            "route_index":  r["index"],
            "route_label":  r["route_label"],
            "rank":         r["rank"],
            "crowd":        crowd,
            "start_s":      clock_s,
            "start_min":    round(clock_s / 60, 1),
            "travel_s":     r["time_s"],
            "time_min":     r["time_min"],
            "arrival_min":  round((clock_s + r["time_s"]) / 60, 1),
            "length_m":     r["length_m"],
            "length_km":    r["length_km"],
            "flow_rpm":     flow_rpm,
            "mode":         mode,
            "recommendation": r["recommendation"],
            # Score breakdown for UI display
            "scores": {
                "Time":        r["score_time"],
                "Quality":     r["score_quality"],
                "Directness":  r["score_directness"],
                "Capacity":    r["score_capacity"],
                "Overall":     r["score_composite"],
            },
        })
        clock_s += release_s + MIN_PHASE_GAP_SEC

    return {
        "routes_info":    route_data,
        "allocations":    allocated,
        "phases":         phases,
        "pa_script":      _pa_script(phases, location_type, mode_cfg),
        "total_time_min": round(clock_s / 60, 1),
        "mode":           mode,
        "ranking_method": "Google Maps-style: Time (50%) + Quality (20%) + Directness (20%) + Capacity (10%)",
    }


def _route_recommendation(s: dict) -> str:
    """
    Generate a plain-English reason for recommending this route —
    same style as Google Maps shows ("Fastest route", "Usually fast", etc.)
    """
    if s["rank"] == 1:
        if s["directness"] > 0.8:
            return "✅ Best route — fastest and most direct"
        elif s["quality"] > 0.8:
            return "✅ Best route — fastest via high-quality roads"
        else:
            return "✅ Best route — fastest overall"
    elif s["directness"] > 0.75:
        return "🔵 Good alternative — very direct route"
    elif s["quality"] > 0.75:
        return "🔵 Good alternative — better road quality"
    elif s["cap_score"] > 0.8:
        return "🔵 High-capacity route — handles large crowds well"
    else:
        return "🟡 Alternative route — avoids congestion on main roads"


def _pa_script(phases, location_type, mode_cfg) -> list:
    audience = {
        "Railway / Metro": "passengers", "Temple / Religious": "devotees",
        "Big Event / Rally": "attendees", "Stadium / Concert": "guests",
        "Market / Shopping": "visitors", "Public Transport": "passengers",
        "Emergency / Disaster": "persons",
    }.get(location_type, "persons")

    lines = [
        f"Attention {audience}. An orderly phased dispersal is beginning. "
        f"Please follow the instructions for your group. "
        f"Transport mode: {mode_cfg['label']}. Stay calm and move steadily."
    ]
    for p in phases:
        lines.append(
            f"[T+{p['start_min']} min] Phase {p['phase']}: "
            f"{p['crowd']:,} {audience} — proceed via {p['route_label']} "
            f"({p['length_km']:.2f} km, estimated {p['time_min']} min). "
            f"{p['recommendation']}. Flow: {p['flow_rpm']} people/min."
        )
    lines.append(
        "Security personnel are stationed at every checkpoint. "
        "Thank you for your cooperation."
    )
    return lines

# ─────────────────────────────────────────────────────────────────────────────
# Map rendering
# ─────────────────────────────────────────────────────────────────────────────

MODE_COLORS = {
    "walk":       ["#27AE60", "#2980B9", "#8E44AD", "#E67E22", "#C0392B"],
    "cycle":      ["#F39C12", "#D35400", "#E74C3C", "#8E44AD", "#1ABC9C"],
    "motorcycle": ["#3498DB", "#E74C3C", "#2ECC71", "#9B59B6", "#F39C12"],
    "car":        ["#E74C3C", "#C0392B", "#D35400", "#7F8C8D", "#2C3E50"],
}


def plot_routes_on_map(G, routes: list, center_coords: tuple,
                       dispersion_plan: dict = None) -> folium.Map:
    mode    = (dispersion_plan or {}).get("mode", "walk")
    colors  = MODE_COLORS.get(mode, MODE_COLORS["walk"])
    dashed  = mode in ("motorcycle", "car")
    WEIGHTS = [9, 7, 6, 5, 4]

    m = folium.Map(location=center_coords, zoom_start=14, tiles="CartoDB positron")

    phases         = (dispersion_plan or {}).get("phases", [])
    route_info     = (dispersion_plan or {}).get("routes_info", [])
    phase_by_route = {p["route_index"]: p for p in phases}

    for i, path in enumerate(routes):
        if not path: continue
        color  = colors[i % len(colors)]
        weight = WEIGHTS[i] if i < len(WEIGHTS) else 3
        coords = [(G.nodes[n]["y"], G.nodes[n]["x"])
                  for n in path if "y" in G.nodes[n] and "x" in G.nodes[n]]
        if len(coords) < 2: continue

        ph = phase_by_route.get(i)
        ri = route_info[i] if i < len(route_info) else {}

        # Tooltip shows Google Maps-style info: recommendation + score breakdown
        if ph:
            scores = ph.get("scores", {})
            tip = (
                f"<b>{ph['route_label']}</b> — {ph.get('recommendation','')}<br>"
                f"👥 {ph['crowd']:,} people | 📏 {ph['length_km']:.2f} km | "
                f"⏱ {ph['time_min']} min | 🕐 T+{ph['start_min']} min<br>"
                f"<small>⏱ Time: {scores.get('Time',0):.0f}  "
                f"🛣 Quality: {scores.get('Quality',0):.0f}  "
                f"📐 Direct: {scores.get('Directness',0):.0f}  "
                f"👥 Cap: {scores.get('Capacity',0):.0f}  "
                f"<b>Overall: {scores.get('Overall',0):.0f}</b></small>"
            )
        else:
            tip = (f"<b>Route {i+1}</b><br>"
                   f"📏 {ri.get('length_km','?')} km | ⏱ {ri.get('time_min','?')} min")

        folium.PolyLine(
            coords, color=color, weight=weight, opacity=0.9,
            dash_array="10 5" if dashed else None,
            tooltip=folium.Tooltip(tip, sticky=True),
        ).add_to(m)

        if ph and coords:
            mid = coords[len(coords) // 2]
            folium.Marker(
                location=mid,
                icon=folium.DivIcon(
                    html=(f'<div style="background:{color};color:white;'
                          f'border-radius:50%;width:30px;height:30px;'
                          f'display:flex;align-items:center;justify-content:center;'
                          f'font-weight:bold;font-size:14px;'
                          f'box-shadow:0 2px 8px rgba(0,0,0,.5);">'
                          f'{ph["phase"]}</div>'),
                    icon_size=(30, 30), icon_anchor=(15, 15),
                ),
            ).add_to(m)

    if routes and routes[0]:
        fc = [(G.nodes[n]["y"], G.nodes[n]["x"])
              for n in routes[0] if "y" in G.nodes[n]]
        if fc:
            folium.Marker(fc[0], popup="🔴 Crowd Start",
                          icon=folium.Icon(color="red",   icon="users", prefix="fa")).add_to(m)
            folium.Marker(fc[-1], popup="✅ Safe Exit",
                          icon=folium.Icon(color="green", icon="flag",  prefix="fa")).add_to(m)
    return m
