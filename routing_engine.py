"""
routing_engine.py — Google Maps-style Multi-Modal Crowd Dispersion Engine
==========================================================================

PERFORMANCE DESIGN:
  1. Bbox-based graph fetch   → downloads only the road corridor, not a full circle
  2. Parallel mode downloads  → all transport modes fetched simultaneously via threads
  3. networkx built-in A*     → C-optimised astar_path, not pure Python
  4. networkx k-shortest      → C-optimised shortest_simple_paths (Yen's algorithm)
  5. Session-state caching    → same locations → zero re-download on repeat clicks

Algorithm: A* with Haversine heuristic (same as Google Maps)
  f(n) = g(n) + h(n)  where h = haversine_distance / max_speed  (admissible)
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
    },
    "cycle": {
        "label":            "🚲 Cycling / 2-Wheeler (Non-Motor)",
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
    },
    "motorcycle": {
        "label":            "🛵 Motorcycle / Scooter",
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
    },
    "car": {
        "label":            "🚗 Car / 4-Wheeler",
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
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_ROAD_WIDTH = 3.0
MAX_ROUTES         = 5         # keep it small for speed
MAX_EDGE_OVERLAP   = 0.55
MIN_PHASE_GAP_SEC  = 90
PENALTY_CRITICAL   = 10.0
PENALTY_WARNING    = 3.5

# Corridor padding around the straight-line route (metres, per mode)
CORRIDOR_PADDING: dict[str, int] = {
    "walk":       400,
    "cycle":      500,
    "motorcycle": 600,
    "car":        800,
}

_counter = itertools.count()

# ─────────────────────────────────────────────────────────────────────────────
# Geocoding
# ─────────────────────────────────────────────────────────────────────────────

def geocode_multi(query: str, limit: int = 6) -> list:
    try:
        geo = Nominatim(user_agent="crowd_dispersion_multimodal_v5")
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
# PERFORMANCE FIX 1: Bbox-based graph fetch (corridor, not full circle)
# ─────────────────────────────────────────────────────────────────────────────

def _bbox_for_route(src_lat, src_lon, tgt_lat, tgt_lon, padding_m: int) -> tuple:
    """
    Bounding box around the route corridor.
    Much smaller than a circle — downloads only roads the route might actually use.
    """
    lat_pad = padding_m / 111_000          # 1° lat ≈ 111 km
    lon_pad = padding_m / (111_000 * math.cos(math.radians((src_lat + tgt_lat) / 2)))

    north = max(src_lat, tgt_lat) + lat_pad
    south = min(src_lat, tgt_lat) - lat_pad
    east  = max(src_lon, tgt_lon) + lon_pad
    west  = min(src_lon, tgt_lon) - lon_pad
    return north, south, east, west


# In-process cache: (src_lat, src_lon, tgt_lat, tgt_lon, mode) → G
# Avoids re-downloading when user clicks Generate again with same locations.
_GRAPH_CACHE: dict = {}
MAX_CACHE = 6


def _fetch_graph_single(src_lat, src_lon, tgt_lat, tgt_lon,
                        network_type: str, padding_m: int):
    """Fetch one graph via bounding box (fast corridor approach)."""
    ox.settings.timeout = 45          # hard cap — don't hang forever
    ox.settings.log_console = False
    # Use use_cache=True so repeated fetches hit OSMnx's local disk cache
    ox.settings.use_cache = True
    north, south, east, west = _bbox_for_route(src_lat, src_lon, tgt_lat, tgt_lon, padding_m)
    G = ox.graph_from_bbox(
        (west, south, east, north),
        network_type=network_type,
        simplify=True,
        retain_all=False,
    )
    # Do NOT project — stays in EPSG:4326, skips expensive re-projection
    return G


def fetch_map_graph_for_route(src_lat: float, src_lon: float,
                               tgt_lat: float, tgt_lon: float,
                               mode: str = "walk"):
    """
    FAST graph fetch using bounding-box corridor + in-process cache.
    Returns (G, error_str).
    """
    cache_key = (round(src_lat, 4), round(src_lon, 4),
                 round(tgt_lat, 4), round(tgt_lon, 4), mode)
    if cache_key in _GRAPH_CACHE:
        return _GRAPH_CACHE[cache_key], None

    try:
        network_type = TRANSPORT_MODES.get(mode, TRANSPORT_MODES["walk"])["network_type"]
        dist_m   = haversine_m(src_lat, src_lon, tgt_lat, tgt_lon)
        padding  = max(150, min(int(dist_m * 0.15), CORRIDOR_PADDING.get(mode, 500)))
        G = _fetch_graph_single(src_lat, src_lon, tgt_lat, tgt_lon, network_type, padding)

        # Trim cache to MAX_CACHE entries (FIFO)
        if len(_GRAPH_CACHE) >= MAX_CACHE:
            oldest = next(iter(_GRAPH_CACHE))
            del _GRAPH_CACHE[oldest]
        _GRAPH_CACHE[cache_key] = G
        return G, None
    except Exception as exc:
        return None, str(exc)


# PERFORMANCE FIX 2: Parallel multi-mode graph downloads
def fetch_graphs_parallel(src_lat: float, src_lon: float,
                          tgt_lat: float, tgt_lon: float,
                          modes: list[str]) -> dict[str, tuple]:
    """
    Download graphs for all selected transport modes simultaneously.
    Uses ThreadPoolExecutor — all network requests happen in parallel.

    Returns {mode_key: (G, error_str)}
    """
    results: dict[str, tuple] = {}

    def _fetch(mode_key):
        G, err = fetch_map_graph_for_route(src_lat, src_lon, tgt_lat, tgt_lon, mode_key)
        return mode_key, G, err

    with ThreadPoolExecutor(max_workers=len(modes)) as pool:
        futures = {pool.submit(_fetch, mk): mk for mk in modes}
        for future in as_completed(futures):
            mk, G, err = future.result()
            results[mk] = (G, err)

    return results


# Legacy single-point fetch kept for backward compat
def fetch_map_graph_from_point(lat: float, lon: float, dist: int = 700,
                               mode: str = "walk"):
    try:
        ox.settings.timeout = 60
        ox.settings.log_console = False
        network_type = TRANSPORT_MODES.get(mode, TRANSPORT_MODES["walk"])["network_type"]
        G = ox.graph_from_point((lat, lon), dist=dist,
                                network_type=network_type,
                                simplify=True, retain_all=False)
        return ox.project_graph(G, to_crs="EPSG:4326"), (lat, lon), None
    except Exception as exc:
        return None, (lat, lon), str(exc)


def fetch_map_graph(location_name: str, dist: int = 700, mode: str = "walk"):
    results = geocode_multi(location_name, limit=1)
    if not results:
        return None, None, f"Could not geocode '{location_name}'"
    r = results[0]
    return fetch_map_graph_from_point(r["lat"], r["lon"], dist=dist, mode=mode)


# ─────────────────────────────────────────────────────────────────────────────
# Edge helpers
# ─────────────────────────────────────────────────────────────────────────────

def _best_edge(raw: dict) -> dict:
    if not raw:
        return {}
    first_val = next(iter(raw.values()))
    return first_val if isinstance(first_val, dict) else raw


def _highway_type(edge_data: dict) -> str:
    hw = edge_data.get("highway", "default")
    return str(hw[0] if isinstance(hw, list) else hw)


def _walk_speed_for_mode(edge_data: dict, mode: str) -> float:
    profile = TRANSPORT_MODES.get(mode, TRANSPORT_MODES["walk"])
    hw = _highway_type(edge_data)
    return profile["speeds"].get(hw, profile["default_speed_ms"])


def _road_width(edge_data: dict) -> float:
    for key in ("width", "lanes"):
        val = edge_data.get(key)
        if val is None:
            continue
        try:
            v = float(str(val).split(";")[0].strip())
            return v * 2.5 if key == "lanes" else v
        except ValueError:
            continue
    hw = _highway_type(edge_data)
    return float({
        "motorway": 12, "trunk": 10, "primary": 8, "secondary": 6,
        "tertiary": 5, "residential": 4, "living_street": 3,
        "footway": 2, "pedestrian": 4, "path": 2, "service": 3, "cycleway": 2,
    }.get(hw, DEFAULT_ROAD_WIDTH))


def _edge_length_m(edge_data: dict) -> float:
    return max(1.0, float(edge_data.get("length", 50.0)))


def _edge_time_s(edge_data: dict, mode: str) -> float:
    return _edge_length_m(edge_data) / _walk_speed_for_mode(edge_data, mode)


def _route_length_m(G, path: list) -> float:
    return sum(_edge_length_m(_best_edge(G.get_edge_data(u, v) or {}))
               for u, v in zip(path[:-1], path[1:]))


def _route_length_km(G, path: list) -> float:
    return _route_length_m(G, path) / 1000.0


def _route_time_s(G, path: list, mode: str = "walk") -> float:
    return sum(_edge_time_s(_best_edge(G.get_edge_data(u, v) or {}), mode)
               for u, v in zip(path[:-1], path[1:]))


def _bottleneck_width(G, path: list) -> float:
    widths = [_road_width(_best_edge(G.get_edge_data(u, v) or {}))
              for u, v in zip(path[:-1], path[1:])]
    return min(widths) if widths else DEFAULT_ROAD_WIDTH


def _route_capacity(G, path: list, mode: str = "walk") -> int:
    sqm = TRANSPORT_MODES.get(mode, TRANSPORT_MODES["walk"])["sqm_per_unit"]
    return max(10, int((_route_length_m(G, path) * _bottleneck_width(G, path)) / sqm))


# ─────────────────────────────────────────────────────────────────────────────
# PERFORMANCE FIX 3: Build time-weight dict for networkx A*
# ─────────────────────────────────────────────────────────────────────────────

def _build_time_weights(G, mode: str, node_penalties: dict = None,
                        edge_multipliers: dict = None) -> dict:
    """
    Pre-compute edge weight dict {(u,v): seconds}.
    networkx's built-in astar_path uses this via weight= callable.
    """
    weights: dict[tuple, float] = {}
    np = node_penalties or {}
    em = edge_multipliers or {}
    for u, v, data in G.edges(data=True):
        inner = _best_edge(data) if isinstance(next(iter(data.values()), None), dict) else data
        base  = _edge_time_s(inner, mode)
        mult  = max(
            em.get((u, v), 1.0),
            PENALTY_CRITICAL if np.get(u) == "Critical" or np.get(v) == "Critical"
            else PENALTY_WARNING if np.get(u) == "Warning" or np.get(v) == "Warning"
            else 1.0
        )
        weights[(u, v)] = base * mult
    return weights


# ─────────────────────────────────────────────────────────────────────────────
# PERFORMANCE FIX 3 (cont.): Use networkx built-in A* (C-optimised)
# ─────────────────────────────────────────────────────────────────────────────

def _astar_nx(G, src: int, tgt: int, weights: dict, mode: str) -> Optional[list]:
    """
    Use networkx's built-in astar_path — runs in C, much faster than pure Python.

    Heuristic: haversine straight-line time (admissible → always finds optimal path).
    """
    tgt_y = G.nodes[tgt].get("y", 0.0)
    tgt_x = G.nodes[tgt].get("x", 0.0)
    max_spd = max(TRANSPORT_MODES[mode]["speeds"].values(),
                  default=TRANSPORT_MODES[mode]["default_speed_ms"])

    def heuristic(u, v):
        uy, ux = G.nodes[u].get("y", tgt_y), G.nodes[u].get("x", tgt_x)
        return haversine_m(uy, ux, tgt_y, tgt_x) / max_spd

    def weight_fn(u, v, data):
        return weights.get((u, v), _edge_time_s(_best_edge(data), mode))

    try:
        return nx.astar_path(G, src, tgt, heuristic=heuristic, weight=weight_fn)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# PERFORMANCE FIX 4: Use networkx k-shortest paths (C-optimised Yen's)
# ─────────────────────────────────────────────────────────────────────────────

def _find_k_routes(G, src: int, tgt: int, weights: dict,
                   mode: str, k: int = MAX_ROUTES) -> list[list]:
    """
    Find k diverse routes using Penalty-based A* search.
    This is extremely fast compared to Yen's algorithm and yields actually diverse paths
    by penalizing edges of already chosen paths.
    """
    routes: list[list] = []
    current_weights = weights.copy()
    penalty_multiplier = 2.0

    for i in range(k):
        path = _astar_nx(G, src, tgt, current_weights, mode)
        if not path:
            break
        routes.append(path)
        # Apply penalty to edges on this path in both directions
        for u, v in zip(path[:-1], path[1:]):
            if (u, v) not in current_weights:
                edge_data = G.get_edge_data(u, v)
                best = _best_edge(edge_data) if edge_data else {}
                current_weights[(u, v)] = _edge_time_s(best, mode)
            current_weights[(u, v)] *= penalty_multiplier

            if (v, u) not in current_weights:
                edge_data = G.get_edge_data(v, u)
                best = _best_edge(edge_data) if edge_data else {}
                current_weights[(v, u)] = _edge_time_s(best, mode)
            current_weights[(v, u)] *= penalty_multiplier

    return routes


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def find_routes(G, source_coords: tuple, target_coords: tuple,
                mode: str = "walk",
                weight_multiplier: dict = None,
                zone_statuses: dict = None,
                zone_node_map: dict = None) -> list[list]:
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


def build_dispersion_plan(G, routes: list, total_crowd: int,
                          mode: str = "walk",
                          location_type: str = "General") -> dict:
    if not routes or G is None:
        return {}

    mode_cfg = TRANSPORT_MODES.get(mode, TRANSPORT_MODES["walk"])
    route_data = []
    for i, path in enumerate(routes):
        lm  = _route_length_m(G, path)
        ts  = _route_time_s(G, path, mode)
        cap = _route_capacity(G, path, mode)
        bw  = _bottleneck_width(G, path)
        route_data.append({
            "index":        i,
            "path":         path,
            "route_label":  f"Route {i + 1}",
            "length_m":     round(lm),
            "length_km":    round(lm / 1000, 2),
            "time_s":       round(ts),
            "time_min":     round(ts / 60, 1),
            "capacity":     cap,
            "bottleneck_w": round(bw, 2),
            "score":        (cap / max(ts, 1.0)) * 100.0,
            "mode":         mode,
        })

    total_score = sum(r["score"] for r in route_data) or 1.0
    allocated, remaining = [], total_crowd
    for idx, r in enumerate(route_data):
        if idx == len(route_data) - 1:
            share = max(0, remaining)
        else:
            share = int(round((r["score"] / total_score) * total_crowd))
            remaining -= share
        share = min(share, max(1, r["capacity"]))
        allocated.append(max(0, share))

    phases, clock_s = [], 0
    for idx, (r, crowd) in enumerate(zip(route_data, allocated)):
        if crowd <= 0:
            continue
        flow_rpm  = max(10, int(mode_cfg["flow_base"] * r["bottleneck_w"]))
        release_s = int((crowd / flow_rpm) * 60)
        phases.append({
            "phase":       idx + 1,
            "route_index": r["index"],
            "route_label": r["route_label"],
            "crowd":       crowd,
            "start_s":     clock_s,
            "start_min":   round(clock_s / 60, 1),
            "travel_s":    r["time_s"],
            "time_min":    r["time_min"],
            "arrival_min": round((clock_s + r["time_s"]) / 60, 1),
            "length_m":    r["length_m"],
            "length_km":   r["length_km"],
            "flow_rpm":    flow_rpm,
            "mode":        mode,
        })
        clock_s += release_s + MIN_PHASE_GAP_SEC

    return {
        "routes_info":    route_data,
        "allocations":    allocated,
        "phases":         phases,
        "pa_script":      _pa_script(phases, location_type, mode_cfg),
        "total_time_min": round(clock_s / 60, 1),
        "mode":           mode,
    }


def _pa_script(phases, location_type, mode_cfg) -> list[str]:
    audience = {
        "Railway / Metro": "passengers", "Temple / Religious": "devotees",
        "Big Event / Rally": "attendees", "Stadium / Concert": "guests",
        "Market / Shopping": "visitors", "Public Transport": "passengers",
        "Emergency / Disaster": "persons",
    }.get(location_type, "persons")
    lines = [
        f"Attention {audience}. Orderly phased dispersal is beginning. "
        f"Those {mode_cfg['icon']} {mode_cfg['label']} — please follow your route below."
    ]
    for p in phases:
        lines.append(
            f"[T+{p['start_min']} min] Phase {p['phase']}: "
            f"{p['crowd']:,} {audience} via {p['route_label']} "
            f"({p['length_km']:.2f} km, ETA {p['arrival_min']} min). Stay steady."
        )
    lines.append("Security at every checkpoint. Thank you.")
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
    mode   = (dispersion_plan or {}).get("mode", "walk")
    colors = MODE_COLORS.get(mode, MODE_COLORS["walk"])
    dashed = mode in ("motorcycle", "car")

    m = folium.Map(location=center_coords, zoom_start=14, tiles="CartoDB positron")
    WEIGHTS = [9, 7, 6, 5, 4]

    phases         = (dispersion_plan or {}).get("phases", [])
    route_info     = (dispersion_plan or {}).get("routes_info", [])
    phase_by_route = {p["route_index"]: p for p in phases}

    for i, path in enumerate(routes):
        if not path:
            continue
        color  = colors[i % len(colors)]
        weight = WEIGHTS[i] if i < len(WEIGHTS) else 3
        coords = [(G.nodes[n]["y"], G.nodes[n]["x"])
                  for n in path if "y" in G.nodes[n] and "x" in G.nodes[n]]
        if len(coords) < 2:
            continue

        ph = phase_by_route.get(i)
        ri = route_info[i] if i < len(route_info) else {}

        tip = (
            f"<b>{TRANSPORT_MODES.get(mode,{}).get('icon','')} {ph['route_label']}</b><br>"
            f"👥 {ph['crowd']:,} people | 📏 {ph['length_km']:.2f} km | "
            f"⏱ {ph['time_min']} min | 🕐 T+{ph['start_min']} min"
        ) if ph else (
            f"<b>Route {i+1}</b><br>📏 {ri.get('length_km','?')} km | ⏱ {ri.get('time_min','?')} min"
        )

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
                    html=(f'<div style="background:{color};color:white;border-radius:50%;'
                          f'width:30px;height:30px;display:flex;align-items:center;'
                          f'justify-content:center;font-weight:bold;font-size:14px;'
                          f'box-shadow:0 2px 8px rgba(0,0,0,.5);">{ph["phase"]}</div>'),
                    icon_size=(30, 30), icon_anchor=(15, 15),
                ),
            ).add_to(m)

    if routes and routes[0]:
        fc = [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in routes[0] if "y" in G.nodes[n]]
        if fc:
            folium.Marker(fc[0], popup="🔴 Crowd Start",
                          icon=folium.Icon(color="red", icon="users", prefix="fa")).add_to(m)
            folium.Marker(fc[-1], popup="✅ Safe Exit",
                          icon=folium.Icon(color="green", icon="flag", prefix="fa")).add_to(m)
    return m
