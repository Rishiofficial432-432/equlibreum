"""
healing.py — Phase II: Topological Reconstruction
====================================================
Converts a fragmented skeleton (broken by occlusion) into a unified,
routable vector graph using graph-theoretic "healing":

  heal_skeleton_mst(G, max_gap_px, angle_tolerance)
    1. Find all connected components (fragments caused by occlusion)
    2. Collect endpoint nodes of each fragment
    3. Build a candidate bridge set: all endpoint pairs across components,
       weighted by Euclidean distance + angular deviation penalty
    4. Run Kruskal's MST on the candidate graph → minimum-cost bridges
    5. Insert healing edges (marked synthetic=True) into G
    6. Return healed graph + healing metadata

  connectivity_ratio(G_before, G_after)
    = len(largest CC after) / len(largest CC before)
    A value > 1.0 means healing increased network size — target metric
    described in the problem statement.

  disjoint_set_components(G)
    Pure Python Union-Find implementation for component detection —
    no external deps beyond NetworkX.
"""

from __future__ import annotations
import math
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import networkx as nx


# ─────────────────────────────────────────────────────────────────────────────
# UNION-FIND  (Disjoint Set)
# ─────────────────────────────────────────────────────────────────────────────

class UnionFind:
    """
    Path-compressed, rank-unioned Disjoint Set data structure.
    Used to track which skeleton fragments have been merged during healing.
    """
    def __init__(self, elements):
        self._parent = {e: e for e in elements}
        self._rank   = {e: 0 for e in elements}

    def find(self, x):
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # path compression
            x = self._parent[x]
        return x

    def union(self, a, b) -> bool:
        """Returns True if a and b were in different sets (bridge was useful)."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1
        return True


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _node_position(G: nx.Graph, node) -> Tuple[float, float]:
    """Return (y, x) pixel position of a node using the 'o' attribute
    set by sknw.  Falls back to (0,0) if missing."""
    data = G.nodes[node]
    o = data.get("o")
    if o is None:
        o = data.get("pos", (0, 0))
    return float(o[0]), float(o[1])


def _euclidean(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    return math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)


def _endpoint_direction(G: nx.Graph, endpoint) -> Optional[Tuple[float, float]]:
    """
    Estimate the road direction at an endpoint by looking at the nearest
    neighbour node.  Returns a unit vector (dy, dx) or None if isolated.
    Used for angular alignment check — we don't want to create bridge edges
    that make sharp 90° turns (unnatural for roads).
    """
    neighbours = list(G.neighbors(endpoint))
    if not neighbours:
        return None
    ey, ex = _node_position(G, endpoint)
    ny, nx_ = _node_position(G, neighbours[0])
    dy, dx = ey - ny, ex - nx_
    mag = math.sqrt(dy**2 + dx**2)
    if mag < 1e-6:
        return None
    return (dy / mag, dx / mag)


def _angle_deviation(
    dir1: Optional[Tuple[float, float]],
    dir2: Optional[Tuple[float, float]],
) -> float:
    """
    Angular deviation (degrees) between two road directions.
    A bridge connecting two endpoints that face roughly the same direction
    is geometrically plausible (road continues straight through an occlusion).
    A bridge that causes a sharp turn is suspicious.
    Returns 0 if either direction is unknown (no penalty).
    """
    if dir1 is None or dir2 is None:
        return 0.0
    # dot product of the "incoming" direction of endpoint 1 with the
    # "outgoing" direction of endpoint 2
    dot = dir1[0] * (-dir2[0]) + dir1[1] * (-dir2[1])
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot))


def _get_endpoints(G: nx.Graph) -> List:
    """Return all degree-1 nodes (road endpoints / dead ends)."""
    return [n for n in G.nodes() if G.degree(n) == 1]


def _get_junctions(G: nx.Graph) -> List:
    """Return all degree-3+ nodes (intersections)."""
    return [n for n in G.nodes() if G.degree(n) >= 3]


# ─────────────────────────────────────────────────────────────────────────────
# CORE HEALING ALGORITHM
# ─────────────────────────────────────────────────────────────────────────────

def heal_skeleton_mst(
    G: nx.Graph,
    max_gap_px: int = 50,
    angle_tolerance: float = 45.0,
    angle_penalty_weight: float = 0.3,
    min_component_size: int = 2,
) -> Tuple[nx.Graph, dict]:
    """
    MST-based topological healing of a fragmented skeleton graph.

    Algorithm:
      1. Identify disconnected components (caused by occlusion breaks)
      2. For every pair of endpoints across different components:
           a. Compute Euclidean distance (gap size in pixels)
           b. Compute angular deviation (does the bridge make sense
              geometrically? Roads don't usually make sharp turns)
           c. If distance ≤ max_gap_px AND angle ≤ angle_tolerance:
              add to candidate bridge list with composite weight
      3. Sort candidate bridges by composite weight (Kruskal's order)
      4. Use Union-Find to greedily accept bridges that connect new
         components (exactly Kruskal's MST — minimum cost to achieve
         full connectivity)
      5. Insert accepted bridges as edges with synthetic=True flag

    Parameters
    ----------
    G                 : Input fragmented graph (from sknw or manual build)
    max_gap_px        : Maximum pixel gap we're willing to bridge (default 50)
    angle_tolerance   : Max angular deviation (°) to accept a bridge
    angle_penalty_weight : How much to penalise angular deviation in cost
    min_component_size : Components smaller than this are ignored (noise)

    Returns
    -------
    G_healed : New graph with healing edges inserted
    metadata : dict with healing statistics
    """
    if G.number_of_nodes() == 0:
        return G.copy(), {"bridges_added": 0, "components_before": 0,
                          "components_after": 0, "connectivity_ratio": 1.0}

    G_healed = G.copy()

    # ── Step 1: Find connected components ─────────────────────────────────────
    components = [
        c for c in nx.connected_components(G_healed)
        if len(c) >= min_component_size
    ]
    components.sort(key=len, reverse=True)  # largest first
    n_components_before = len(components)

    if n_components_before <= 1:
        # Already connected — nothing to heal
        return G_healed, {
            "bridges_added": 0,
            "components_before": n_components_before,
            "components_after": n_components_before,
            "connectivity_ratio": 1.0,
            "healed_edges": [],
        }

    # ── Step 2: Collect endpoints per component ────────────────────────────────
    component_of = {}
    for idx, comp in enumerate(components):
        for node in comp:
            component_of[node] = idx

    endpoints_by_comp: Dict[int, List] = {i: [] for i in range(len(components))}
    for node in G_healed.nodes():
        if G_healed.degree(node) == 1:
            c_idx = component_of.get(node)
            if c_idx is not None:
                endpoints_by_comp[c_idx].append(node)

    # Fallback: if a component has no degree-1 endpoints (e.g., it's a loop),
    # use a sample of its nodes as potential bridge anchors
    for idx, comp in enumerate(components):
        if not endpoints_by_comp[idx]:
            sample = list(comp)[:min(5, len(comp))]
            endpoints_by_comp[idx].extend(sample)

    # ── Step 3: Build candidate bridge list ────────────────────────────────────
    candidates = []  # (cost, node_a, node_b, distance_px, angle_dev)

    for i in range(len(components)):
        for j in range(i + 1, len(components)):
            for na in endpoints_by_comp[i]:
                for nb in endpoints_by_comp[j]:
                    pa = _node_position(G_healed, na)
                    pb = _node_position(G_healed, nb)
                    dist = _euclidean(pa, pb)
                    if dist > max_gap_px:
                        continue  # too far — unlikely to be a real road gap

                    dir_a = _endpoint_direction(G_healed, na)
                    dir_b = _endpoint_direction(G_healed, nb)
                    angle_dev = _angle_deviation(dir_a, dir_b)
                    if angle_dev > angle_tolerance:
                        continue  # unnatural geometry — skip

                    # Composite cost: distance + angle penalty
                    cost = dist + angle_penalty_weight * angle_dev * (max_gap_px / 45.0)
                    candidates.append((cost, na, nb, dist, angle_dev))

    # Sort by composite cost (Kruskal's MST ordering)
    candidates.sort(key=lambda x: x[0])

    # ── Step 4: Union-Find Kruskal's MST acceptance ────────────────────────────
    uf = UnionFind(range(len(components)))
    healed_edges = []

    for cost, na, nb, dist_px, angle_dev in candidates:
        ci = component_of.get(na)
        cj = component_of.get(nb)
        if ci is None or cj is None:
            continue
        if uf.union(ci, cj):
            # This bridge connects two previously disconnected components
            healed_edges.append({
                "node_a": na,
                "node_b": nb,
                "distance_px": round(dist_px, 2),
                "angle_deviation_deg": round(angle_dev, 2),
                "cost": round(cost, 2),
            })
            # Insert the healing edge
            G_healed.add_edge(na, nb,
                              weight=dist_px,
                              synthetic=True,
                              gap_px=dist_px,
                              angle_dev=angle_dev)
            # Update component_of so future iterations are aware
            root = uf.find(ci)
            for node in list(components[cj]) + list(components[ci]):
                component_of[node] = root

    # ── Step 5: Compute post-healing stats ────────────────────────────────────
    components_after = list(nx.connected_components(G_healed))
    n_components_after = len([c for c in components_after
                               if len(c) >= min_component_size])
    largest_before = len(components[0]) if components else 0
    largest_after  = (max(len(c) for c in components_after)
                      if components_after else 0)
    connectivity_ratio = (largest_after / largest_before
                          if largest_before > 0 else 1.0)

    metadata = {
        "bridges_added":       len(healed_edges),
        "components_before":   n_components_before,
        "components_after":    n_components_after,
        "largest_before":      largest_before,
        "largest_after":       largest_after,
        "connectivity_ratio":  round(connectivity_ratio, 4),
        "healed_edges":        healed_edges,
    }

    return G_healed, metadata


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTIVITY RATIO  (standalone metric)
# ─────────────────────────────────────────────────────────────────────────────

def connectivity_ratio(G_before: nx.Graph, G_after: nx.Graph) -> float:
    """
    Connectivity Ratio = (largest CC size after healing) /
                         (largest CC size before healing)

    A value > 1.0 confirms the healing phase increased network connectivity.
    Per the problem statement, this is a core evaluation metric alongside
    IoU and Dice scores.
    """
    def _largest(G):
        if G.number_of_nodes() == 0:
            return 0
        ccs = list(nx.connected_components(G))
        return max(len(c) for c in ccs) if ccs else 0

    before = _largest(G_before)
    after  = _largest(G_after)
    if before == 0:
        return 1.0
    return round(after / before, 4)


# ─────────────────────────────────────────────────────────────────────────────
# VISUALISATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def draw_healed_overlay(
    img_bgr: np.ndarray,
    G_before: nx.Graph,
    G_after: nx.Graph,
    metadata: dict,
) -> np.ndarray:
    """
    Draw a colour-coded overlay showing:
      - Cyan lines  : original skeleton edges (pre-healing)
      - Green lines : healing bridge edges (synthetic connections)
      - Orange dots : original junctions
      - Yellow dots : newly bridged endpoints
    """
    import cv2 as _cv2  # local import to avoid circular deps

    overlay = img_bgr.copy()
    healed_edge_pairs = {
        (e["node_a"], e["node_b"]) for e in metadata.get("healed_edges", [])
    }
    healed_edge_pairs |= {(b, a) for a, b in healed_edge_pairs}  # both directions

    # Draw original edges
    for u, v, data in G_after.edges(data=True):
        is_healed = (u, v) in healed_edge_pairs or data.get("synthetic", False)
        pts = data.get("pts")
        color = (0, 220, 100) if is_healed else (0, 220, 220)  # green vs cyan
        thickness = 3 if is_healed else 1
        if pts is not None and len(pts) > 1:
            for i in range(len(pts) - 1):
                p1 = (int(pts[i][1]), int(pts[i][0]))
                p2 = (int(pts[i+1][1]), int(pts[i+1][0]))
                _cv2.line(overlay, p1, p2, color, thickness)
        elif is_healed:
            # Synthetic edge — draw a dashed straight line between nodes
            ya, xa = _node_position(G_after, u)
            yb, xb = _node_position(G_after, v)
            _cv2.line(overlay, (int(xa), int(ya)), (int(xb), int(yb)),
                      (0, 255, 80), 2)

    # Draw nodes
    for n, data in G_after.nodes(data=True):
        y, x = _node_position(G_after, n)
        color = (0, 255, 80) if n in {e["node_a"] for e in metadata.get("healed_edges", [])} \
                             or n in {e["node_b"] for e in metadata.get("healed_edges", [])} \
                else (255, 140, 0)
        _cv2.circle(overlay, (int(x), int(y)), 4, color, -1)

    return overlay
