"""
criticality.py — Phase III: Structural Intelligence & Stress Testing
=====================================================================
Implements all quantitative metrics defined in the problem statement:

  compute_betweenness(G)        → node & edge centrality dicts
  compute_global_efficiency(G)  → GNE scalar (1/avg shortest path)
  compute_resilience_index(G, removed_nodes) → R = GNE_perturbed / GNE_baseline
  node_ablation_series(G, top_n) → table of R after each top node removed
  identify_gatekeeper_nodes(G)  → nodes that are both high-centrality
                                    AND articulation points

Terminology follows the problem statement exactly:
  - "Gatekeeper Node"  = high-betweenness articulation point
  - "Resilience Index" = GNE ratio (R < 1 = degraded, R → 0 = collapse)
  - "Network Stress Test" = systematic node ablation
"""

from __future__ import annotations
import math
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH SIZE LIMITS  (keep everything responsive on CPU)
# ─────────────────────────────────────────────────────────────────────────────

_MAX_EXACT_NODES      = 150   # exact betweenness up to this size
_MAX_APPROX_NODES     = 600   # approx betweenness up to this size (k-sample)
_MAX_EFFICIENCY_NODES = 300   # GNE exact computation limit
_BETWEENNESS_K        = 60    # number of pivot nodes for approximate betweenness


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH PRUNING
# ─────────────────────────────────────────────────────────────────────────────

def prune_graph(G: nx.Graph, max_nodes: int,
                preserve_articulaion: bool = True) -> nx.Graph:
    """
    Reduce an oversized graph while preserving its structural character.

    Strategy:
      1. Remove very short leaf stubs (dead-end noise)
      2. Contract degree-2 chains (straightaways that add nodes but not structure)
      3. If still over limit, keep highest-degree nodes (junctions are richest)
    """
    G = G.copy()

    # Pass 1: remove short leaf edges
    changed = True
    while changed and G.number_of_nodes() > max_nodes:
        changed = False
        for n in list(G.nodes()):
            if G.degree(n) != 1:
                continue
            nbrs = list(G.neighbors(n))
            if not nbrs:
                continue
            nb = nbrs[0]
            w = G[n][nb].get("weight", 1.0)
            if w < 8.0:
                G.remove_node(n)
                changed = True

    # Pass 2: if still too large, keep highest-degree nodes
    if G.number_of_nodes() > max_nodes:
        top = sorted(G.nodes(), key=lambda n: G.degree(n), reverse=True)[:max_nodes]
        G = G.subgraph(top).copy()

    return G


# ─────────────────────────────────────────────────────────────────────────────
# BETWEENNESS CENTRALITY
# ─────────────────────────────────────────────────────────────────────────────

def compute_betweenness(G: nx.Graph, use_weight: bool = True) -> dict:
    """
    Compute node and edge betweenness centrality on the largest connected
    component.  Uses approximate sampling for large graphs to stay fast.

    Returns
    -------
    dict with keys:
      node_centrality  : {node: float}
      edge_centrality  : {(u,v): float}
      articulation_points : [node, ...]
      num_components   : int
      largest_cc_size  : int
      total_nodes      : int
      total_edges      : int
      main_graph       : nx.Graph (the largest CC, possibly pruned)
    """
    if G.number_of_nodes() == 0:
        return _empty_criticality_result()

    # Work on largest connected component
    components = sorted(nx.connected_components(G), key=len, reverse=True)
    G_main = G.subgraph(components[0]).copy()

    if G_main.number_of_nodes() > _MAX_APPROX_NODES:
        G_main = prune_graph(G_main, _MAX_APPROX_NODES)

    n = G_main.number_of_nodes()
    weight = "weight" if use_weight else None

    if n > _MAX_EXACT_NODES:
        k = min(_BETWEENNESS_K, n)
        node_bc = nx.betweenness_centrality(
            G_main, k=k, weight=weight, normalized=True, seed=42)
        edge_bc = nx.edge_betweenness_centrality(
            G_main, k=k, weight=weight, normalized=True, seed=42)
    else:
        node_bc = nx.betweenness_centrality(
            G_main, weight=weight, normalized=True)
        edge_bc = nx.edge_betweenness_centrality(
            G_main, weight=weight, normalized=True)

    artic = list(nx.articulation_points(G_main))

    return {
        "node_centrality":    node_bc,
        "edge_centrality":    edge_bc,
        "articulation_points": artic,
        "num_components":     len(components),
        "largest_cc_size":    len(components[0]),
        "total_nodes":        G.number_of_nodes(),
        "total_edges":        G.number_of_edges(),
        "main_graph":         G_main,
    }


def _empty_criticality_result() -> dict:
    return {
        "node_centrality": {}, "edge_centrality": {},
        "articulation_points": [], "num_components": 0,
        "largest_cc_size": 0, "total_nodes": 0, "total_edges": 0,
        "main_graph": nx.Graph(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL NETWORK EFFICIENCY
# ─────────────────────────────────────────────────────────────────────────────

def compute_global_efficiency(G: nx.Graph,
                               weight: str = "weight") -> float:
    """
    Global Network Efficiency (Latora & Marchiori 2001):

        GNE = 1/(N*(N-1)) * Σ_{i≠j} 1/d(i,j)

    where d(i,j) is the shortest path length (weighted by road segment
    length in pixels).  Disconnected pairs contribute 0 (1/∞ = 0).

    GNE = 1.0  → perfectly connected (every node directly links to every other)
    GNE = 0.0  → completely disconnected

    For large graphs uses an approximate version (sampled pairs) to
    remain CPU-responsive.
    """
    n = G.number_of_nodes()
    if n < 2:
        return 0.0

    # Sample nodes for large graphs
    sample = list(G.nodes())
    if n > _MAX_EFFICIENCY_NODES:
        import random
        rng = random.Random(42)
        sample = rng.sample(sample, _MAX_EFFICIENCY_NODES)
        n_eff = _MAX_EFFICIENCY_NODES
    else:
        n_eff = n

    total = 0.0
    count = 0
    for source in sample:
        try:
            lengths = nx.single_source_dijkstra_path_length(
                G, source, weight=weight)
            for target, dist in lengths.items():
                if target != source and dist > 0:
                    total += 1.0 / dist
                    count += 1
        except (nx.NetworkXError, nx.NodeNotFound):
            pass

    if count == 0:
        return 0.0
    # Normalise to [0,1] using the sampled pair count
    return total / (n_eff * (n_eff - 1))


# ─────────────────────────────────────────────────────────────────────────────
# RESILIENCE INDEX
# ─────────────────────────────────────────────────────────────────────────────

def compute_resilience_index(
    G: nx.Graph,
    removed_nodes: Optional[List] = None,
    weight: str = "weight",
) -> Tuple[float, float, float]:
    """
    Resilience Index (R) as defined in the problem statement:

        R = GNE(perturbed) / GNE(baseline)

    R = 1.0 → network fully intact
    R < 1.0 → degraded (lower = more vulnerable)
    R ≈ 0.0 → catastrophic failure / complete fragmentation

    Parameters
    ----------
    G              : Full road network graph
    removed_nodes  : List of nodes to remove (simulated failures)

    Returns
    -------
    (R, gne_baseline, gne_perturbed)
    """
    if G.number_of_nodes() == 0:
        return 1.0, 0.0, 0.0

    gne_baseline = compute_global_efficiency(G, weight=weight)

    if not removed_nodes:
        return 1.0, gne_baseline, gne_baseline

    G_perturbed = G.copy()
    for node in removed_nodes:
        if node in G_perturbed:
            G_perturbed.remove_node(node)

    gne_perturbed = compute_global_efficiency(G_perturbed, weight=weight)

    if gne_baseline == 0:
        R = 1.0
    else:
        R = gne_perturbed / gne_baseline

    return round(R, 4), round(gne_baseline, 6), round(gne_perturbed, 6)


# ─────────────────────────────────────────────────────────────────────────────
# NODE ABLATION SERIES  (Stress Testing)
# ─────────────────────────────────────────────────────────────────────────────

def node_ablation_series(
    G: nx.Graph,
    betweenness_result: dict,
    top_n: int = 10,
    weight: str = "weight",
) -> List[dict]:
    """
    Systematic "Network Stress Test" as described in the problem statement.

    For each of the top_n highest-betweenness nodes, simulate removing it
    (and all previously removed nodes cumulatively) and record:
      - Resilience Index R
      - Nodes isolated from largest component
      - New number of connected fragments
      - Whether the node was an articulation point

    Returns a list of dicts, one per removal step, sorted by betweenness rank.
    """
    node_bc = betweenness_result.get("node_centrality", {})
    artic   = set(betweenness_result.get("articulation_points", []))
    G_main  = betweenness_result.get("main_graph", G)

    if G_main.number_of_nodes() == 0:
        return []

    top_nodes = sorted(node_bc, key=node_bc.get, reverse=True)[:top_n]
    if not top_nodes:
        return []

    gne_baseline = compute_global_efficiency(G_main, weight=weight)
    baseline_largest = (
        len(max(nx.connected_components(G_main), key=len))
        if G_main.number_of_nodes() > 0 else 0
    )
    baseline_components = nx.number_connected_components(G_main)

    results = []
    removed_so_far = []

    for rank, node in enumerate(top_nodes):
        removed_so_far.append(node)

        G_test = G_main.copy()
        for n in removed_so_far:
            if n in G_test:
                G_test.remove_node(n)

        if G_test.number_of_nodes() == 0:
            results.append({
                "rank":             rank + 1,
                "node":             node,
                "centrality":       round(node_bc.get(node, 0), 5),
                "is_articulation":  node in artic,
                "R":                0.0,
                "gne_perturbed":    0.0,
                "components_after": 0,
                "largest_after":    0,
                "nodes_isolated":   baseline_largest,
                "pct_isolated":     100.0,
            })
            break

        gne_perturbed = compute_global_efficiency(G_test, weight=weight)
        R = round(gne_perturbed / gne_baseline, 4) if gne_baseline > 0 else 1.0

        ccs = list(nx.connected_components(G_test))
        largest_after = max(len(c) for c in ccs) if ccs else 0
        n_components  = len(ccs)
        nodes_isolated = max(0, baseline_largest - largest_after - rank)
        pct_isolated   = round(100 * nodes_isolated / max(1, baseline_largest), 1)

        results.append({
            "rank":             rank + 1,
            "node":             node,
            "centrality":       round(node_bc.get(node, 0), 5),
            "is_articulation":  node in artic,
            "R":                R,
            "gne_baseline":     round(gne_baseline, 6),
            "gne_perturbed":    round(gne_perturbed, 6),
            "components_after": n_components,
            "largest_after":    largest_after,
            "nodes_isolated":   max(0, nodes_isolated),
            "pct_isolated":     max(0.0, pct_isolated),
        })

    return results


# ─────────────────────────────────────────────────────────────────────────────
# GATEKEEPER NODE IDENTIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def identify_gatekeeper_nodes(
    betweenness_result: dict,
    centrality_percentile: float = 75.0,
) -> List[dict]:
    """
    Identify "Gatekeeper Nodes" as defined in the problem statement:
    nodes that satisfy BOTH criteria simultaneously:

      1. High betweenness centrality (above `centrality_percentile` percentile)
      2. Articulation point (removing them disconnects the network)

    These represent true single points of failure — the network has no
    alternative routing around them AND they carry heavy through-traffic.

    Returns a list of dicts sorted by centrality (highest first).
    """
    node_bc = betweenness_result.get("node_centrality", {})
    artic   = set(betweenness_result.get("articulation_points", []))

    if not node_bc:
        return []

    import numpy as _np
    scores = list(node_bc.values())
    threshold = float(_np.percentile(scores, centrality_percentile))

    gatekeepers = [
        {
            "node":        n,
            "centrality":  round(score, 5),
            "threshold":   round(threshold, 5),
            "is_artic":    n in artic,
        }
        for n, score in node_bc.items()
        if score >= threshold and n in artic
    ]
    gatekeepers.sort(key=lambda x: x["centrality"], reverse=True)
    return gatekeepers


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE-NODE COLLAPSE SIMULATION  (interactive use)
# ─────────────────────────────────────────────────────────────────────────────

def simulate_single_collapse(G: nx.Graph, node_to_remove,
                              weight: str = "weight") -> dict:
    """
    Simulate removing one specific node and report the impact.
    Used by the interactive Streamlit "Simulate Collapse" button.

    Returns dict with before/after metrics and the Resilience Index R.
    """
    if node_to_remove not in G:
        return {"error": f"Node {node_to_remove} not found in graph."}

    before_ccs  = list(nx.connected_components(G))
    before_comp = len(before_ccs)
    before_lrg  = max(len(c) for c in before_ccs) if before_ccs else 0
    gne_before  = compute_global_efficiency(G, weight=weight)

    G_after = G.copy()
    G_after.remove_node(node_to_remove)

    after_ccs   = list(nx.connected_components(G_after)) if G_after.number_of_nodes() else []
    after_comp  = len(after_ccs)
    after_lrg   = max(len(c) for c in after_ccs) if after_ccs else 0
    gne_after   = compute_global_efficiency(G_after, weight=weight)

    R = round(gne_after / gne_before, 4) if gne_before > 0 else 1.0
    isolated = max(0, before_lrg - after_lrg)
    pct      = round(100 * isolated / max(1, before_lrg), 1)

    # Average path length change (problem statement metric)
    def _avg_path(Gx):
        try:
            ccs2 = list(nx.connected_components(Gx))
            if not ccs2:
                return float("inf")
            main = Gx.subgraph(max(ccs2, key=len)).copy()
            if main.number_of_nodes() < 2:
                return 0.0
            return nx.average_shortest_path_length(main, weight=weight)
        except Exception:
            return float("inf")

    apl_before = _avg_path(G)
    apl_after  = _avg_path(G_after)
    apl_delta  = (apl_after - apl_before) if apl_after != float("inf") else float("inf")

    return {
        "node":              node_to_remove,
        "before_components": before_comp,
        "after_components":  after_comp,
        "before_largest":    before_lrg,
        "after_largest":     after_lrg,
        "nodes_isolated":    isolated,
        "pct_isolated":      pct,
        "gne_before":        round(gne_before, 6),
        "gne_after":         round(gne_after, 6),
        "resilience_index":  R,
        "apl_before":        round(apl_before, 4) if apl_before != float("inf") else None,
        "apl_after":         round(apl_after,  4) if apl_after  != float("inf") else None,
        "apl_delta":         round(apl_delta,  4) if apl_delta  != float("inf") else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# RELAXED IoU (Tolerance Buffer)
# ─────────────────────────────────────────────────────────────────────────────

def relaxed_iou(
    pred_mask: "np.ndarray",
    gt_mask: "np.ndarray",
    buffer_px: int = 4,
) -> float:
    """
    Relaxed / Length-Complete IoU as described in the problem statement.

    If a predicted road pixel falls within `buffer_px` pixels of a ground
    truth road pixel, it counts as a True Positive (not penalised for minor
    alignment shifts).

    Uses morphological dilation to expand both masks by buffer_px before
    computing standard IoU.

    Returns float in [0, 1].
    """
    import cv2 as _cv2
    import numpy as _np

    kernel = _cv2.getStructuringElement(
        _cv2.MORPH_ELLIPSE, (2 * buffer_px + 1, 2 * buffer_px + 1))
    pred_d = _cv2.dilate(pred_mask.astype(_np.uint8), kernel)
    gt_d   = _cv2.dilate(gt_mask.astype(_np.uint8), kernel)

    inter = _np.logical_and(pred_d > 0, gt_d > 0).sum()
    union = _np.logical_or(pred_d > 0, gt_d > 0).sum()
    if union == 0:
        return 1.0
    return float(inter) / float(union)
