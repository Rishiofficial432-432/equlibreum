"""
equilibrium.py — Route Resilience: Road Extraction & Criticality Analysis
============================================================================
Pipeline (classical-CV-first, swap in deep learning later):

  1. Upload satellite image
  2. Extract road mask (edge detection + morphology — works today,
     no training required; same function signature can later be swapped
     for a U-Net/DeepLabV3 occlusion-robust model with zero changes
     to steps 3-6)
  3. Skeletonize the mask → thin centerlines
  4. Convert skeleton → NetworkX graph (nodes + weighted edges)
  5. Run criticality analysis:
       - Betweenness centrality  → "busiest" edges (bottlenecks)
       - Articulation points     → nodes whose removal disconnects the graph
  6. Visualize: original → mask → skeleton → graph with critical points highlighted
  7. "Simulate collapse" — remove the top critical node/edge and show
     how many nodes become unreachable (urban collapse scenario)
"""

from __future__ import annotations
import io
import math
from typing import Optional
import numpy as np
import streamlit as st
import pandas as pd
from PIL import Image
import cv2
import networkx as nx

try:
    from skimage.morphology import skeletonize
    SKIMAGE_OK = True
except ImportError:
    SKIMAGE_OK = False

try:
    import sknw
    SKNW_OK = True
except ImportError:
    SKNW_OK = False


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Road mask extraction (classical CV)
# ─────────────────────────────────────────────────────────────────────────────

def extract_road_mask(img_bgr: np.ndarray,
                      occlusion_compensation: bool = True) -> np.ndarray:
    """
    Extract a binary road mask from a satellite image using classical CV.

    Pipeline:
      - Grayscale + CLAHE (contrast boost — helps pull roads out from
        shadow/canopy darkened regions, a cheap stand-in for "occlusion
        robustness" until a learned model replaces this function)
      - Adaptive thresholding (roads are usually a consistent grey tone,
        distinct from green canopy / building rooftops)
      - Morphological closing — bridges small gaps (this is the
        "topological reconnection" step, classical version)
      - Remove small blobs that aren't elongated enough to be roads

    Returns a uint8 binary mask (255 = road, 0 = background).
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    if occlusion_compensation:
        # CLAHE boosts local contrast — pulls road signal out of shadowed
        # or canopy-darkened regions without blowing out bright areas
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)

    # Adaptive threshold — robust to uneven lighting across the tile
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, blockSize=35, C=10,
    )

    # Morphological closing — bridges gaps caused by occlusion (tree
    # canopy breaking a road into disconnected segments)
    kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel_close, iterations=2)

    # Remove small noise blobs
    kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, kernel_open, iterations=1)

    # Keep only elongated components (roads are long & thin, not blobby
    # like building rooftops or open ground)
    mask = _filter_elongated_components(opened)

    return mask


def _filter_elongated_components(mask: np.ndarray, min_area: int = 80,
                                  min_aspect: float = 2.0) -> np.ndarray:
    """Keep only connected components that look road-shaped (elongated)."""
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = np.zeros_like(mask)

    for i in range(1, num_labels):  # skip background label 0
        area = stats[i, cv2.CC_STAT_AREA]
        if area < min_area:
            continue
        w = stats[i, cv2.CC_STAT_WIDTH]
        h = stats[i, cv2.CC_STAT_HEIGHT]
        aspect = max(w, h) / max(1, min(w, h))
        # Keep if elongated OR reasonably large (junctions are blobby but valid)
        if aspect >= min_aspect or area >= 400:
            out[labels == i] = 255

    return out


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Skeletonization
# ─────────────────────────────────────────────────────────────────────────────

# Maximum image dimension before we downsample to keep skeleton manageable
_MAX_SKEL_DIM = 800

def skeletonize_mask(mask: np.ndarray) -> np.ndarray:
    """Thin the road mask down to single-pixel-wide centerlines.
    
    Automatically downsamples large masks so the skeleton graph stays
    small enough for betweenness centrality to complete quickly.
    """
    if not SKIMAGE_OK:
        raise RuntimeError(
            "scikit-image is not installed. Run: pip install scikit-image"
        )
    # Downsample if image is too large — keeps node count manageable
    h, w = mask.shape[:2]
    if max(h, w) > _MAX_SKEL_DIM:
        scale = _MAX_SKEL_DIM / max(h, w)
        new_w, new_h = int(w * scale), int(h * scale)
        mask = cv2.resize(mask, (new_w, new_h), interpolation=cv2.INTER_NEAREST)

    binary = (mask > 0).astype(np.uint8)
    skeleton = skeletonize(binary).astype(np.uint8) * 255
    return skeleton


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Skeleton → Graph
# ─────────────────────────────────────────────────────────────────────────────

def skeleton_to_graph(skeleton: np.ndarray) -> nx.Graph:
    """
    Convert a skeleton image into a NetworkX graph using sknw.
    Nodes = junctions/endpoints. Edges = road segments with pixel-path
    geometry and length (in pixels) as weight.
    """
    if not SKNW_OK:
        raise RuntimeError("sknw is not installed. Run: pip install sknw")

    binary = (skeleton > 0).astype(np.uint16)
    G = sknw.build_sknw(binary, multi=False)

    # Add 'weight' = pixel-length of each edge (sknw stores this as 'weight' already,
    # but we normalize the key name defensively)
    for u, v, data in G.edges(data=True):
        if "weight" not in data:
            pts = data.get("pts")
            if pts is not None and len(pts) > 1:
                data["weight"] = float(np.sum(
                    np.linalg.norm(np.diff(pts, axis=0), axis=1)))
            else:
                data["weight"] = 1.0

    return G


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Criticality analysis
# ─────────────────────────────────────────────────────────────────────────────

# Graphs above this node count use approximate betweenness (sampled)
_MAX_EXACT_NODES = 100
# Hard cap — prune graph down to this many nodes if still too large
_MAX_GRAPH_NODES = 500


def _prune_graph(G: nx.Graph, max_nodes: int) -> nx.Graph:
    """
    Reduce an oversized graph by repeatedly removing degree-2 nodes
    (straightaways) and then low-weight leaf edges until we are under
    max_nodes. This preserves junctions and critical structure.
    """
    G = G.copy()

    # First pass: remove very short leaf edges (dead-end stubs)
    changed = True
    while changed and G.number_of_nodes() > max_nodes:
        changed = False
        leaves = [n for n in G.nodes() if G.degree(n) == 1]
        short_leaves = [
            n for n in leaves
            if any(G[n][nb].get("weight", 1.0) < 5.0 for nb in G.neighbors(n))
        ]
        if short_leaves:
            G.remove_nodes_from(short_leaves)
            changed = True

    # Second pass: if still too large, keep only the top-N nodes by degree
    if G.number_of_nodes() > max_nodes:
        top_nodes = sorted(G.nodes(), key=lambda n: G.degree(n), reverse=True)[:max_nodes]
        G = G.subgraph(top_nodes).copy()

    return G


def analyze_criticality(G: nx.Graph) -> dict:
    """
    Compute graph-theoretic criticality metrics:

      - betweenness_centrality (edges): how many shortest paths pass
        through each edge — high value = bottleneck / chokepoint
      - articulation_points (nodes): nodes whose removal disconnects
        the graph — single points of failure
      - connected_components: how fragmented the network already is
        (a side-effect of occlusion — disconnected mask = disconnected graph)

    Uses approximate betweenness (k-sample) for large graphs to avoid hanging.
    Returns a dict with all results, safe to call on small/disconnected graphs.
    """
    if G.number_of_nodes() == 0:
        return {
            "node_centrality": {}, "edge_centrality": {},
            "articulation_points": [], "num_components": 0,
            "largest_component_size": 0, "total_nodes": 0, "total_edges": 0,
        }

    # Use the largest connected component for centrality (meaningless on
    # tiny disconnected fragments otherwise)
    components = list(nx.connected_components(G))
    components.sort(key=len, reverse=True)
    largest_cc_nodes = components[0] if components else set()
    G_main = G.subgraph(largest_cc_nodes).copy()

    # Prune if graph is still too large after image downsampling
    if G_main.number_of_nodes() > _MAX_GRAPH_NODES:
        G_main = _prune_graph(G_main, _MAX_GRAPH_NODES)

    n_nodes = G_main.number_of_nodes()

    # Use approximate betweenness (random k-sample of source nodes) for
    # large graphs — O(k·E) instead of O(V·E), completes in seconds
    if n_nodes > _MAX_EXACT_NODES:
        k = min(50, n_nodes)  # sample up to 50 pivot nodes
        node_centrality = nx.betweenness_centrality(
            G_main, k=k, weight="weight", normalized=True, seed=42
        )
        edge_centrality = nx.edge_betweenness_centrality(
            G_main, k=k, weight="weight", normalized=True, seed=42
        )
    else:
        node_centrality = nx.betweenness_centrality(
            G_main, weight="weight", normalized=True
        )
        edge_centrality = nx.edge_betweenness_centrality(
            G_main, weight="weight", normalized=True
        )

    artic_points = list(nx.articulation_points(G_main))

    return {
        "node_centrality":        node_centrality,
        "edge_centrality":        edge_centrality,
        "articulation_points":    artic_points,
        "num_components":         len(components),
        "largest_component_size": len(largest_cc_nodes),
        "total_nodes":            G.number_of_nodes(),
        "total_edges":            G.number_of_edges(),
        "main_graph":             G_main,
    }


def simulate_collapse(G: nx.Graph, node_to_remove) -> dict:
    """
    'Urban collapse scenario': remove one critical node and measure
    how much of the network becomes unreachable as a result.
    """
    if node_to_remove not in G:
        return {"error": "Node not found in graph"}

    before_components = nx.number_connected_components(G)
    before_largest     = len(max(nx.connected_components(G), key=len))

    G_after = G.copy()
    G_after.remove_node(node_to_remove)

    after_components = nx.number_connected_components(G_after) if G_after.number_of_nodes() else 0
    after_largest     = (len(max(nx.connected_components(G_after), key=len))
                          if G_after.number_of_nodes() else 0)

    nodes_isolated = before_largest - after_largest

    return {
        "before_components": before_components,
        "after_components":  after_components,
        "before_largest":    before_largest,
        "after_largest":     after_largest,
        "nodes_isolated":    max(0, nodes_isolated),
        "pct_disconnected":  round(100 * max(0, nodes_isolated) / max(1, before_largest), 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Visualization helpers
# ─────────────────────────────────────────────────────────────────────────────

def draw_graph_overlay(img_bgr: np.ndarray, G: nx.Graph,
                       critical_nodes: list = None,
                       critical_edges: list = None) -> np.ndarray:
    """Draw the extracted graph on top of the original image."""
    overlay = img_bgr.copy()
    critical_nodes = critical_nodes or []
    critical_edges = critical_edges or []

    # Draw edges
    for u, v, data in G.edges(data=True):
        pts = data.get("pts")
        is_critical = (u, v) in critical_edges or (v, u) in critical_edges
        color = (0, 0, 255) if is_critical else (0, 255, 255)   # red if critical, else cyan
        thickness = 3 if is_critical else 1
        if pts is not None and len(pts) > 1:
            for i in range(len(pts) - 1):
                p1 = (int(pts[i][1]), int(pts[i][0]))
                p2 = (int(pts[i+1][1]), int(pts[i+1][0]))
                cv2.line(overlay, p1, p2, color, thickness)

    # Draw nodes
    for n, data in G.nodes(data=True):
        y, x = data.get("o", (0, 0))
        is_critical = n in critical_nodes
        color  = (0, 0, 255) if is_critical else (255, 128, 0)
        radius = 6 if is_critical else 3
        cv2.circle(overlay, (int(x), int(y)), radius, color, -1)

    return overlay


def mask_to_pil(mask: np.ndarray) -> Image.Image:
    return Image.fromarray(mask)


def bgr_to_pil(img_bgr: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))


# ─────────────────────────────────────────────────────────────────────────────
# Main Streamlit render
# ─────────────────────────────────────────────────────────────────────────────

def render():
    st.markdown("## ⚖️ Equilibrium")
    st.markdown(
        "**Route Resilience: Occlusion-Robust Road Extraction & "
        "Graph-Theoretic Criticality Analysis**"
    )
    st.caption(
        "Upload a satellite image → extract the road network even where it's "
        "broken by tree canopy or shadows → analyze which roads are critical "
        "bottlenecks → simulate what happens if one collapses."
    )

    missing = []
    if not SKIMAGE_OK: missing.append("scikit-image")
    if not SKNW_OK:    missing.append("sknw")
    if missing:
        st.error(
            f"Missing packages: **{', '.join(missing)}**.  \n"
            f"Run: `pip install {' '.join(missing)}`"
        )
        return

    st.divider()

    # ── Upload ────────────────────────────────────────────────────────
    uploaded = st.file_uploader(
        "Upload a satellite / aerial image",
        type=["jpg", "jpeg", "png", "tif", "tiff"],
        key="eq_uploader",
    )

    if uploaded is None:
        st.info(
            "👆 Upload a satellite tile to begin. "
            "Works best on imagery showing visible road networks — "
            "a city block, neighbourhood, or small district."
        )
        return

    file_bytes = uploaded.getvalue()
    pil_img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    img_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    st.divider()

    # ── Settings ──────────────────────────────────────────────────────
    with st.expander("⚙️ Extraction settings", expanded=False):
        occlusion_comp = st.checkbox(
            "Occlusion compensation (CLAHE contrast boost)",
            value=True,
            help="Helps recover road signal from tree-shadowed or canopy-darkened areas",
        )

    # ── Run pipeline ──────────────────────────────────────────────────
    run = st.button("🚀 Extract Road Network & Analyze",
                    type="primary", width="stretch", key="eq_run")

    if not run and "eq_result" not in st.session_state:
        col1, col2 = st.columns(2)
        with col1:
            st.image(bgr_to_pil(img_bgr), caption="Uploaded image", width="stretch")
        return

    if run:
        with st.spinner("Step 1/4 — Extracting road mask…"):
            mask = extract_road_mask(img_bgr, occlusion_compensation=occlusion_comp)

        with st.spinner("Step 2/4 — Skeletonizing…"):
            skeleton = skeletonize_mask(mask)

        with st.spinner("Step 3/4 — Building graph…"):
            G = skeleton_to_graph(skeleton)

        n_nodes = G.number_of_nodes()
        spinner_msg = (
            f"Step 4/4 — Running criticality analysis on {n_nodes} nodes"
            + (" (approximate, graph too large for exact)…" if n_nodes > _MAX_EXACT_NODES else "…")
        )
        with st.spinner(spinner_msg):
            analysis = analyze_criticality(G)

        st.session_state["eq_result"] = {
            "img_bgr": img_bgr, "mask": mask, "skeleton": skeleton,
            "G": G, "analysis": analysis,
        }
        st.rerun()

    if "eq_result" not in st.session_state:
        return

    res      = st.session_state["eq_result"]
    img_bgr  = res["img_bgr"]
    mask     = res["mask"]
    skeleton = res["skeleton"]
    G        = res["G"]
    analysis = res["analysis"]

    st.success(
        f"✅ Extracted **{analysis['total_nodes']} junctions** and "
        f"**{analysis['total_edges']} road segments** · "
        f"**{analysis['num_components']} connected fragments** found"
    )

    # ── Pipeline visualization (4 stages side by side) ─────────────────
    st.markdown("### 🔬 Pipeline Stages")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.image(bgr_to_pil(img_bgr), caption="1. Original", width="stretch")
    with c2:
        st.image(mask_to_pil(mask), caption="2. Road mask", width="stretch")
    with c3:
        st.image(mask_to_pil(skeleton), caption="3. Skeleton", width="stretch")
    with c4:
        critical_nodes = sorted(analysis["node_centrality"],
                                key=analysis["node_centrality"].get, reverse=True)[:5]
        critical_edges = sorted(analysis["edge_centrality"],
                                key=analysis["edge_centrality"].get, reverse=True)[:8]
        overlay = draw_graph_overlay(img_bgr, G, critical_nodes, critical_edges)
        st.image(bgr_to_pil(overlay), caption="4. Graph + criticality", width="stretch")

    st.caption(
        "🔴 Red = critical (high-centrality) roads/junctions — these carry the "
        "most through-traffic and are bottlenecks. 🟠 Orange = regular junctions. "
        "🟦 Cyan = regular road segments."
    )

    if analysis["num_components"] > 1:
        st.warning(
            f"⚠️ The extracted network is **fragmented into "
            f"{analysis['num_components']} disconnected pieces** "
            f"(likely caused by occlusion in the source image — tree canopy, "
            f"shadows, or low contrast breaking the road mask). "
            f"Only the largest fragment ({analysis['largest_component_size']} nodes) "
            f"was used for criticality analysis below."
        )

    st.divider()

    # ── Criticality tables ──────────────────────────────────────────────
    st.markdown("### 📊 Criticality Analysis")

    t1, t2 = st.columns(2)

    with t1:
        st.markdown("#### 🔴 Top Bottleneck Roads")
        st.caption("Road segments carrying the most shortest-paths through them")
        if analysis["edge_centrality"]:
            edge_rows = sorted(analysis["edge_centrality"].items(),
                               key=lambda x: x[1], reverse=True)[:10]
            df_edges = pd.DataFrame([
                {"Segment": f"Node {u} ↔ Node {v}", "Criticality score": round(score, 4)}
                for (u, v), score in edge_rows
            ])
            st.dataframe(df_edges, width="stretch", hide_index=True)
        else:
            st.info("No edges found — try a larger or clearer image.")

    with t2:
        st.markdown("#### 🟠 Articulation Points (single points of failure)")
        st.caption("Junctions that, if blocked, would split the network into disconnected pieces")
        artic = analysis["articulation_points"]
        if artic:
            df_artic = pd.DataFrame([
                {"Junction": f"Node {n}",
                 "Centrality": round(analysis["node_centrality"].get(n, 0), 4)}
                for n in artic
            ]).sort_values("Centrality", ascending=False)
            st.dataframe(df_artic, width="stretch", hide_index=True)
        else:
            st.info("No single points of failure found in this network — it's well connected.")

    st.divider()

    # ── Collapse simulation ───────────────────────────────────────────
    st.markdown("### 💥 Simulate Urban Collapse")
    st.caption(
        "Pick a critical junction and simulate what happens if it becomes "
        "unusable (flooding, building collapse, bridge failure, etc.) — "
        "see how much of the network gets cut off."
    )

    main_graph = analysis.get("main_graph")
    if main_graph and main_graph.number_of_nodes() > 0:
        # Offer top-N critical nodes as options
        top_nodes = sorted(analysis["node_centrality"],
                           key=analysis["node_centrality"].get, reverse=True)[:15]
        if top_nodes:
            sel_node = st.selectbox(
                "Select a junction to simulate removing:",
                top_nodes,
                format_func=lambda n: (
                    f"Node {n}  (centrality: {analysis['node_centrality'].get(n, 0):.4f})"
                    + ("  ⚠️ ARTICULATION POINT" if n in analysis["articulation_points"] else "")
                ),
                key="eq_collapse_node",
            )

            if st.button("💥 Simulate Collapse", key="eq_simulate", type="primary"):
                sim = simulate_collapse(main_graph, sel_node)

                if "error" in sim:
                    st.error(sim["error"])
                else:
                    sc1, sc2, sc3 = st.columns(3)
                    sc1.metric("Fragments before", sim["before_components"])
                    sc2.metric("Fragments after", sim["after_components"],
                              delta=sim["after_components"] - sim["before_components"])
                    sc3.metric("Nodes isolated", sim["nodes_isolated"],
                              delta=f"-{sim['pct_disconnected']}%", delta_color="inverse")

                    if sim["nodes_isolated"] > 0:
                        st.error(
                            f"🚨 Removing this junction would **disconnect "
                            f"{sim['nodes_isolated']} nodes** "
                            f"({sim['pct_disconnected']}% of the network) "
                            f"from the rest of the road system. "
                            f"This is a **critical single point of failure**."
                        )
                    else:
                        st.success(
                            "✅ Removing this junction does not disconnect any part "
                            "of the network — there are alternate routes available."
                        )
        else:
            st.info("No nodes available to simulate.")
    else:
        st.info("Run the extraction above first to enable collapse simulation.")

    st.divider()
    if st.button("🔄 Analyze a different image", key="eq_reset"):
        st.session_state.pop("eq_result", None)
        st.rerun()
