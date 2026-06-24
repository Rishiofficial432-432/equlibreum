"""
equilibrium.py — Route Resilience: Occlusion-Robust Road Extraction
     & Graph-Theoretic Criticality Analysis  (Phase IV Dashboard)
======================================================================
Orchestrates the 4-phase pipeline:

  Phase I  : Occlusion-aware road mask extraction (DL or classical CV)
             → segmentation.py
  Phase II : Topological healing — MST + Disjoint Set gap bridging
             → healing.py
  Phase III: Criticality analysis — betweenness, GNE, Resilience Index
             → criticality.py
  Phase IV : Interactive Streamlit dashboard
             • 4-stage pipeline visualisation
             • Healing before/after comparison
             • Betweenness centrality heatmap (Folium/Leaflet)
             • Resilience Index gauge
             • Node ablation table (stress test results)
             • Interactive collapse simulation with rerouting impact

Rendering entry point (called by app.py):  render()
"""

from __future__ import annotations
import io
import math
import json
from typing import Optional

import numpy as np
import streamlit as st
import pandas as pd
from PIL import Image
import cv2
import networkx as nx
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

# ── Internal modules ──────────────────────────────────────────────────────────
import segmentation as seg
import healing     as heal
import criticality as crit

# ── sknw (skeleton → graph) ───────────────────────────────────────────────────
try:
    import sknw
    SKNW_OK = True
except ImportError:
    SKNW_OK = False

# ── streamlit-folium (interactive map) ────────────────────────────────────────
try:
    import folium
    from streamlit_folium import st_folium
    FOLIUM_OK = True
except ImportError:
    FOLIUM_OK = False


# ─────────────────────────────────────────────────────────────────────────────
# SKELETON → GRAPH  (thin wrapper around sknw)
# ─────────────────────────────────────────────────────────────────────────────

def skeleton_to_graph(skeleton: np.ndarray) -> nx.Graph:
    """Convert a skeleton image to a NetworkX graph via sknw."""
    if not SKNW_OK:
        raise RuntimeError("sknw not installed. Run: pip install sknw")
    binary = (skeleton > 0).astype(np.uint16)
    G = sknw.build_sknw(binary, multi=False)
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
# VISUALISATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _bgr_to_pil(img: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))


def _mask_to_pil(mask: np.ndarray) -> Image.Image:
    if len(mask.shape) == 2:
        return Image.fromarray(mask)
    return Image.fromarray(mask)


def _node_pos(G: nx.Graph, n) -> tuple:
    data = G.nodes[n]
    o = data.get("o")
    if o is None:
        o = data.get("pos", (0, 0))
    return float(o[0]), float(o[1])


def draw_graph_overlay(
    img_bgr: np.ndarray,
    G: nx.Graph,
    node_centrality: dict,
    critical_nodes: list,
    critical_edges: list,
    gatekeeper_nodes: list,
) -> np.ndarray:
    """
    Draw colour-coded graph on top of the satellite image.

    Colour coding:
      🔴 Red  (thick)  : top critical edges (highest betweenness)
      🟡 Yellow (thick): gatekeeper nodes (high centrality AND articulation)
      🟠 Orange        : high-centrality articulation points
      🔵 Cyan          : regular road segments
      ⚪ White dots    : regular junctions
    """
    overlay = img_bgr.copy()
    crit_edge_set   = {(u, v) for u, v in critical_edges}
    crit_edge_set  |= {(v, u) for u, v in critical_edges}
    gk_set          = {g["node"] if isinstance(g, dict) else g
                       for g in gatekeeper_nodes}
    artic_set       = set(critical_nodes[:len(critical_nodes)//2])  # top articulation

    # Draw edges
    for u, v, data in G.edges(data=True):
        pts = data.get("pts")
        is_crit = (u, v) in crit_edge_set
        synthetic = data.get("synthetic", False)

        if synthetic:
            color, thick = (0, 220, 80), 2   # green: healed bridge
        elif is_crit:
            color, thick = (0, 0, 255), 3    # red: critical
        else:
            color, thick = (0, 220, 220), 1  # cyan: normal

        if pts is not None and len(pts) > 1:
            for i in range(len(pts) - 1):
                p1 = (int(pts[i][1]), int(pts[i][0]))
                p2 = (int(pts[i+1][1]), int(pts[i+1][0]))
                cv2.line(overlay, p1, p2, color, thick)
        elif synthetic:
            ya, xa = _node_pos(G, u)
            yb, xb = _node_pos(G, v)
            cv2.line(overlay, (int(xa), int(ya)), (int(xb), int(yb)), color, thick)

    # Draw nodes (sorted so most important renders last / on top)
    for n, data in G.nodes(data=True):
        y, x = _node_pos(G, n)
        if n in gk_set:
            color, radius = (0, 255, 255), 8   # yellow: gatekeeper
        elif n in artic_set:
            color, radius = (0, 128, 255), 6   # orange: articulation
        else:
            score = node_centrality.get(n, 0)
            intensity = int(min(255, score * 5000))
            color  = (0, max(30, 200 - intensity), 255)
            radius = 3
        cv2.circle(overlay, (int(x), int(y)), radius, color, -1)

    return overlay


def _make_centrality_colormap_img(
    img_bgr: np.ndarray, G: nx.Graph, node_centrality: dict
) -> np.ndarray:
    """
    Generate a heatmap overlay where each pixel is coloured by the
    betweenness centrality of the nearest road node.  Warm = high risk.
    """
    h, w = img_bgr.shape[:2]
    heat = np.zeros((h, w), dtype=np.float32)

    if not node_centrality:
        return img_bgr.copy()

    max_score = max(node_centrality.values()) or 1.0

    # Paint heat on skeleton pixels using node scores
    for n, score in node_centrality.items():
        y, x = _node_pos(G, n)
        y, x = int(y), int(x)
        if 0 <= y < h and 0 <= x < w:
            norm_score = score / max_score
            cv2.circle(heat, (x, y), 12, norm_score, -1)

    # Gaussian blur for smooth heatmap
    heat = cv2.GaussianBlur(heat, (31, 31), 0)

    # Map to RGB using jet colourmap
    cmap  = plt.get_cmap("plasma")
    rgb   = (cmap(heat)[:, :, :3] * 255).astype(np.uint8)
    bgr   = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    # Blend with original image
    mask_nonzero = (heat > 0.02).astype(np.float32)[:, :, np.newaxis]
    alpha = np.clip(heat[:, :, np.newaxis] * 0.7, 0, 0.7)
    blended = (img_bgr.astype(np.float32) * (1 - alpha) +
               bgr.astype(np.float32) * alpha).astype(np.uint8)
    return blended


def _resilience_gauge(R: float):
    """Render a colour-coded Resilience Index gauge in Streamlit."""
    if R >= 0.75:
        colour = "#22c55e"   # green
        label  = "🟢 Resilient"
        desc   = "Network handles this failure well — alternate routes exist."
    elif R >= 0.45:
        colour = "#f59e0b"   # amber
        label  = "🟡 Vulnerable"
        desc   = "Noticeable impact — rerouting increases travel time significantly."
    else:
        colour = "#ef4444"   # red
        label  = "🔴 Critical Failure"
        desc   = "Severe fragmentation — large parts of the network isolated."

    st.markdown(
        f"""
        <div style="
            background: linear-gradient(135deg, {colour}22, {colour}44);
            border: 2px solid {colour};
            border-radius: 12px;
            padding: 16px 24px;
            text-align: center;
            margin: 8px 0;
        ">
            <div style="font-size: 2.2rem; font-weight: 800;
                        color: {colour}; letter-spacing: 2px;">
                R = {R:.3f}
            </div>
            <div style="font-size: 1.1rem; font-weight: 600;
                        color: {colour}; margin-top: 4px;">
                {label}
            </div>
            <div style="font-size: 0.85rem; color: #aaa; margin-top: 6px;">
                {desc}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.progress(min(1.0, max(0.0, R)))


def _folium_heatmap(G: nx.Graph, node_centrality: dict,
                    img_shape: tuple) -> Optional[object]:
    """
    Build a Folium map centred at (0.5, 0.5) in normalised image space
    (since we don't have real geo-coordinates for a generic satellite tile).
    Nodes are coloured by betweenness centrality using a plasma palette.
    """
    if not FOLIUM_OK or not node_centrality:
        return None

    h, w = img_shape[:2]
    max_score = max(node_centrality.values()) or 1e-9
    cmap = plt.get_cmap("plasma")

    # Use normalised image coords as fake lat/lon (good enough for demo)
    m = folium.Map(location=[0.5, 0.5],
                   zoom_start=11,
                   tiles="CartoDB dark_matter",
                   min_zoom=5, max_zoom=18)

    for n, score in node_centrality.items():
        y, x = _node_pos(G, n)
        # Normalise to [0,1]
        lat = 1.0 - (y / max(h, 1))
        lon = x / max(w, 1)
        norm = score / max_score
        r, g, b, _ = cmap(norm)
        hex_colour = "#{:02x}{:02x}{:02x}".format(
            int(r*255), int(g*255), int(b*255))

        folium.CircleMarker(
            location=[lat, lon],
            radius=max(3, norm * 14),
            color=hex_colour,
            fill=True,
            fill_color=hex_colour,
            fill_opacity=0.85,
            weight=1,
            popup=folium.Popup(
                f"Node {n}<br>Centrality: {score:.5f}<br>"
                f"Criticality rank: top {int((1-norm)*100)}%",
                max_width=200
            ),
        ).add_to(m)

    # Draw synthetic (healed) edges
    for u, v, data in G.edges(data=True):
        if data.get("synthetic", False):
            ya, xa = _node_pos(G, u)
            yb, xb = _node_pos(G, v)
            lat_a, lon_a = 1.0 - ya/max(h,1), xa/max(w,1)
            lat_b, lon_b = 1.0 - yb/max(h,1), xb/max(w,1)
            folium.PolyLine(
                [[lat_a, lon_a], [lat_b, lon_b]],
                color="#22c55e", weight=2, opacity=0.8,
                tooltip="Healed (synthetic) bridge"
            ).add_to(m)

    return m


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE RUNNER  (cached per image)
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False, max_entries=3)
def _run_pipeline(
    file_bytes: bytes,
    extraction_mode: str,
    occlusion_comp: bool,
    clahe_clip: float,
    max_gap_px: int,
    angle_tolerance: float,
) -> dict:
    """
    Run the full 4-phase pipeline on the uploaded image.
    Cached so switching between UI tabs doesn't re-run computation.
    """
    pil_img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    img_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    # Phase I — Road mask extraction
    mask = seg.extract_road_mask(
        img_bgr,
        mode=extraction_mode,
        occlusion_compensation=occlusion_comp,
        clahe_clip=clahe_clip,
    )

    # Phase I.b — Skeletonize
    skeleton = seg.skeletonize_mask(mask)

    # Phase I.c — Build raw graph
    if not SKNW_OK:
        raise RuntimeError("sknw not installed. Run: pip install sknw")
    G_raw = skeleton_to_graph(skeleton)

    # Phase II — Topological healing
    G_healed, heal_meta = heal.heal_skeleton_mst(
        G_raw,
        max_gap_px=max_gap_px,
        angle_tolerance=angle_tolerance,
    )

    # Phase III — Criticality analysis
    bc_result  = crit.compute_betweenness(G_healed)
    gk_nodes   = crit.identify_gatekeeper_nodes(bc_result)
    gne_base   = crit.compute_global_efficiency(
        bc_result.get("main_graph", G_healed))

    # Node ablation series (top 10)
    ablation_series = crit.node_ablation_series(
        G_healed, bc_result, top_n=10)

    return {
        "img_bgr":         img_bgr,
        "mask":            mask,
        "skeleton":        skeleton,
        "G_raw":           G_raw,
        "G_healed":        G_healed,
        "heal_meta":       heal_meta,
        "bc_result":       bc_result,
        "gk_nodes":        gk_nodes,
        "gne_baseline":    gne_base,
        "ablation_series": ablation_series,
    }


# ─────────────────────────────────────────────────────────────────────────────
# STREAMLIT PAGE SECTIONS
# ─────────────────────────────────────────────────────────────────────────────

def _section_pipeline_stages(res: dict):
    """4-stage pipeline visualisation."""
    st.markdown("### 🔬 Phase I — Pipeline Stages")
    c1, c2, c3, c4 = st.columns(4)

    img_bgr   = res["img_bgr"]
    mask      = res["mask"]
    skeleton  = res["skeleton"]
    G_healed  = res["G_healed"]
    bc        = res["bc_result"]
    gk_nodes  = res["gk_nodes"]

    node_bc      = bc.get("node_centrality", {})
    artic        = bc.get("articulation_points", [])
    top_crit_n   = sorted(node_bc, key=node_bc.get, reverse=True)[:8]
    top_crit_e   = sorted(bc.get("edge_centrality", {}),
                          key=bc["edge_centrality"].get, reverse=True)[:12]

    overlay = draw_graph_overlay(img_bgr, G_healed, node_bc,
                                 artic, top_crit_e, gk_nodes)

    with c1:
        st.image(_bgr_to_pil(img_bgr), caption="① Original satellite tile",
                 use_container_width=True)
    with c2:
        mask_colour = cv2.applyColorMap(mask, cv2.COLORMAP_BONE)
        st.image(_bgr_to_pil(mask_colour), caption="② Road mask (segmentation)",
                 use_container_width=True)
    with c3:
        skel_colour = cv2.applyColorMap(skeleton, cv2.COLORMAP_COOL)
        st.image(_bgr_to_pil(skel_colour), caption="③ Skeleton centrelines",
                 use_container_width=True)
    with c4:
        st.image(_bgr_to_pil(overlay), caption="④ Graph + criticality overlay",
                 use_container_width=True)

    st.caption(
        "🔴 **Red** = critical bottleneck roads  ·  "
        "🟡 **Yellow** = Gatekeeper Nodes (fail → split network)  ·  "
        "🟢 **Green dashed** = healed synthetic bridges  ·  "
        "🔵 **Cyan** = normal road segments"
    )


def _section_healing(res: dict):
    """Phase II — Healing comparison."""
    st.markdown("### 🔗 Phase II — Topological Healing (MST + Disjoint Set)")

    heal_meta = res["heal_meta"]
    G_raw     = res["G_raw"]
    G_healed  = res["G_healed"]
    img_bgr   = res["img_bgr"]

    cr = heal_meta.get("connectivity_ratio", 1.0)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Fragments before healing", heal_meta["components_before"])
    m2.metric("Fragments after healing",  heal_meta["components_after"],
              delta=-(heal_meta["components_before"] - heal_meta["components_after"]),
              delta_color="inverse")
    m3.metric("Bridges inserted",         heal_meta["bridges_added"])
    m4.metric("Connectivity Ratio",       f"{cr:.3f}",
              delta=f"+{cr-1:.3f}" if cr > 1 else "0.000",
              delta_color="normal" if cr > 1 else "off")

    if heal_meta["components_before"] > 1 and heal_meta["bridges_added"] > 0:
        col_b, col_a = st.columns(2)

        before_overlay = heal.draw_healed_overlay(
            img_bgr, G_raw, G_raw,
            {"healed_edges": []})  # no bridges = raw state

        after_overlay = heal.draw_healed_overlay(
            img_bgr, G_raw, G_healed, heal_meta)

        with col_b:
            st.image(_bgr_to_pil(before_overlay),
                     caption=f"Before healing — {heal_meta['components_before']} fragments",
                     use_container_width=True)
        with col_a:
            st.image(_bgr_to_pil(after_overlay),
                     caption=f"After healing — {heal_meta['components_after']} fragments  "
                             f"(🟢 green = synthetic bridges)",
                     use_container_width=True)

        if heal_meta.get("healed_edges"):
            with st.expander("📋 Healing bridge details", expanded=False):
                df_bridges = pd.DataFrame(heal_meta["healed_edges"])
                df_bridges = df_bridges.rename(columns={
                    "node_a": "From Node", "node_b": "To Node",
                    "distance_px": "Gap (px)", "angle_deviation_deg": "Angle Dev (°)",
                    "cost": "MST Cost"})
                st.dataframe(df_bridges, hide_index=True, use_container_width=True)
    elif heal_meta["bridges_added"] == 0 and heal_meta["components_before"] > 1:
        st.warning(
            "⚠️ No bridges inserted — all gap candidates exceeded the "
            f"max gap ({res.get('max_gap_px', 50)} px) or angle tolerance. "
            "Try increasing **Max gap** in the settings panel."
        )
    else:
        st.success("✅ Network was already fully connected — no healing needed.")


def _section_criticality(res: dict):
    """Phase III — Criticality metrics."""
    st.markdown("### 📊 Phase III — Structural Intelligence")

    bc      = res["bc_result"]
    gk_list = res["gk_nodes"]
    gne_b   = res["gne_baseline"]

    # ── Summary cards ─────────────────────────────────────────────────────
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Total junctions",  bc["total_nodes"])
    s2.metric("Road segments",    bc["total_edges"])
    s3.metric("Network fragments",bc["num_components"],
              help="After healing. 1 = fully connected.")
    s4.metric("Global Efficiency",f"{gne_b:.5f}",
              help="GNE: 1.0 = perfect connectivity, 0.0 = totally disconnected")

    st.divider()

    # ── Gatekeeper nodes ──────────────────────────────────────────────────
    col_gk, col_edge = st.columns(2)

    with col_gk:
        st.markdown("#### 🔑 Gatekeeper Nodes")
        st.caption("High betweenness + articulation point = true single point of failure")
        if gk_list:
            df_gk = pd.DataFrame([
                {
                    "Node":         g["node"],
                    "Centrality":   g["centrality"],
                    "Is Artic.Pt":  "⚠️ YES" if g["is_artic"] else "No",
                }
                for g in gk_list[:10]
            ])
            st.dataframe(df_gk, hide_index=True, use_container_width=True)
        else:
            st.info("No gatekeeper nodes found — the network is well-redundant.")

    with col_edge:
        st.markdown("#### 🔴 Top Bottleneck Roads")
        st.caption("Road segments carrying the most shortest-paths")
        edge_bc = bc.get("edge_centrality", {})
        if edge_bc:
            top_edges = sorted(edge_bc.items(), key=lambda x: x[1], reverse=True)[:10]
            df_e = pd.DataFrame([
                {"Segment": f"Node {u} ↔ Node {v}", "Criticality": round(s, 5)}
                for (u, v), s in top_edges
            ])
            st.dataframe(df_e, hide_index=True, use_container_width=True)
        else:
            st.info("No edge centrality data.")

    st.divider()

    # ── Node ablation table ───────────────────────────────────────────────
    st.markdown("#### 🧪 Network Stress Test (Node Ablation Series)")
    st.caption(
        "Cumulative removal of top-betweenness nodes — simulates cascading "
        "infrastructure failures (floods, accidents, construction)"
    )
    ablation = res.get("ablation_series", [])
    if ablation:
        df_abl = pd.DataFrame([
            {
                "Rank":          a["rank"],
                "Node":          a["node"],
                "Centrality":    a["centrality"],
                "Gatekeeper?":   "⚠️" if a["is_articulation"] else "—",
                "R (index)":     a["R"],
                "% Isolated":    a["pct_isolated"],
                "Fragments":     a["components_after"],
            }
            for a in ablation
        ])
        # Colour R column
        st.dataframe(
            df_abl.style
            .background_gradient(subset=["R (index)"],
                                 cmap="RdYlGn", vmin=0, vmax=1)
            .background_gradient(subset=["% Isolated"],
                                 cmap="YlOrRd", vmin=0, vmax=100),
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("Ablation series unavailable — run pipeline on a larger image.")


def _section_heatmap(res: dict):
    """Phase IV.a — Centrality heatmap."""
    st.markdown("### 🗺️ Phase IV — Criticality Heatmap")

    img_bgr    = res["img_bgr"]
    G_healed   = res["G_healed"]
    node_bc    = res["bc_result"].get("node_centrality", {})

    # OpenCV heatmap overlay (always works)
    heat_bgr = _make_centrality_colormap_img(img_bgr, G_healed, node_bc)
    st.image(_bgr_to_pil(heat_bgr),
             caption="Betweenness centrality heatmap (plasma: 🔴 high → 🔵 low risk)",
             use_container_width=True)

    # Folium interactive map (if available)
    if FOLIUM_OK and node_bc:
        with st.expander("🌐 Open Interactive Leaflet Map", expanded=False):
            m = _folium_heatmap(G_healed, node_bc, img_bgr.shape)
            if m:
                st_folium(m, width=None, height=400,
                          returned_objects=[], use_container_width=True)
                st.caption(
                    "**How to read this map:** Node size and colour encode "
                    "betweenness centrality. 🟡 Large = major bottleneck. "
                    "🟢 Dashed edges = MST healing bridges."
                )
    elif not FOLIUM_OK:
        st.info("Install `streamlit-folium` for an interactive Leaflet map.")


def _section_simulation(res: dict):
    """Phase IV.b — Interactive collapse simulation."""
    st.markdown("### 💥 Phase IV — Simulate Urban Collapse")
    st.caption(
        "Select a junction → click **Simulate** → see network impact, "
        "Resilience Index, and travel time increase instantly."
    )

    bc       = res["bc_result"]
    G_main   = bc.get("main_graph", res["G_healed"])
    node_bc  = bc.get("node_centrality", {})
    artic    = set(bc.get("articulation_points", []))
    gk_nodes = {g["node"] if isinstance(g, dict) else g
                for g in res.get("gk_nodes", [])}

    if G_main.number_of_nodes() == 0:
        st.info("Run the pipeline above to enable simulation.")
        return

    top_nodes = sorted(node_bc, key=node_bc.get, reverse=True)[:20]
    if not top_nodes:
        st.info("No nodes available for simulation.")
        return

    def _node_label(n):
        score = node_bc.get(n, 0)
        tags  = []
        if n in gk_nodes: tags.append("🔑 GATEKEEPER")
        elif n in artic:  tags.append("⚠️ ARTIC.PT")
        return f"Node {n}  (centrality: {score:.5f})  {' '.join(tags)}"

    col_sel, col_btn = st.columns([3, 1])
    with col_sel:
        sel_node = st.selectbox(
            "Select junction to disable:",
            top_nodes,
            format_func=_node_label,
            key="eq_collapse_node",
        )
    with col_btn:
        st.markdown("<br>", unsafe_allow_html=True)
        do_sim = st.button("💥 Simulate Collapse",
                           type="primary", key="eq_simulate",
                           use_container_width=True)

    if do_sim or st.session_state.get("eq_sim_result"):
        if do_sim:
            with st.spinner("Running impact assessment…"):
                sim = crit.simulate_single_collapse(G_main, sel_node)
            st.session_state["eq_sim_result"] = sim
            st.session_state["eq_sim_node"]   = sel_node
        else:
            sim      = st.session_state["eq_sim_result"]
            sel_node = st.session_state.get("eq_sim_node", sel_node)

        if "error" in sim:
            st.error(sim["error"])
            return

        st.divider()
        # ── Metrics row ────────────────────────────────────────────────────
        _resilience_gauge(sim["resilience_index"])

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Fragments before", sim["before_components"])
        col2.metric("Fragments after",  sim["after_components"],
                    delta=sim["after_components"] - sim["before_components"],
                    delta_color="inverse")
        col3.metric("Nodes isolated",   sim["nodes_isolated"],
                    delta=f"-{sim['pct_isolated']}%", delta_color="inverse")

        apl_d = sim.get("apl_delta")
        if apl_d is not None and sim.get("apl_before"):
            pct_increase = round(100 * apl_d / max(sim["apl_before"], 1e-9), 1)
            col4.metric("Avg travel time",
                        f"+{pct_increase:.1f}%",
                        delta=f"APL Δ {apl_d:+.3f}",
                        delta_color="inverse")
        else:
            col4.metric("GNE after", f"{sim['gne_after']:.5f}")

        # ── Narrative verdict ──────────────────────────────────────────────
        R = sim["resilience_index"]
        if sim["nodes_isolated"] > 0 and R < 0.6:
            st.error(
                f"🚨 **Catastrophic impact!** Disabling Node {sel_node} "
                f"isolates **{sim['nodes_isolated']} junctions** "
                f"({sim['pct_isolated']}% of the network) and drops the "
                f"Resilience Index to **R = {R:.3f}**. "
                f"This is a critical single point of failure — "
                f"planners should prioritise redundant routing here."
            )
        elif sim["nodes_isolated"] > 0:
            st.warning(
                f"⚠️ **Moderate impact.** Removing Node {sel_node} "
                f"disconnects {sim['nodes_isolated']} junctions "
                f"(R = {R:.3f}).  Alternate routes exist but "
                f"are longer — expect congestion during a real closure."
            )
        else:
            st.success(
                f"✅ Network **absorbs this failure gracefully** (R = {R:.3f}). "
                f"Removing Node {sel_node} does not disconnect any part of "
                f"the road network — the city has good redundancy here."
            )

        # ── Rerouting visualisation ────────────────────────────────────────
        with st.expander("🗺️ Show rerouting impact on graph", expanded=False):
            img_bgr = res["img_bgr"].copy()
            G_after = G_main.copy()
            G_after.remove_node(sel_node)

            # Mark original position of removed node
            if sel_node in G_main.nodes:
                y, x = _node_pos(G_main, sel_node)
                cv2.drawMarker(img_bgr, (int(x), int(y)),
                               (0, 0, 255), cv2.MARKER_CROSS, 18, 3)
                cv2.circle(img_bgr, (int(x), int(y)), 12, (0, 0, 255), 2)

            overlay_after = draw_graph_overlay(
                img_bgr, G_after,
                node_bc, list(artic), [], [])
            st.image(_bgr_to_pil(overlay_after),
                     caption=f"Network after removing Node {sel_node}  "
                             f"(❌ red cross = disabled junction)",
                     use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN RENDER  (called from app.py)
# ─────────────────────────────────────────────────────────────────────────────

def render():
    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown(
        """
        <style>
        .eq-header {
            background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 50%, #0f172a 100%);
            border: 1px solid #334155;
            border-radius: 16px;
            padding: 24px 32px;
            margin-bottom: 24px;
        }
        .eq-header h1 {
            font-size: 1.8rem;
            font-weight: 800;
            background: linear-gradient(90deg, #60a5fa, #a78bfa, #f472b6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin: 0;
        }
        .eq-badge {
            display: inline-block;
            background: #1e40af22;
            border: 1px solid #3b82f6;
            border-radius: 6px;
            padding: 3px 10px;
            font-size: 0.75rem;
            color: #93c5fd;
            margin: 4px 4px 0 0;
        }
        </style>
        <div class="eq-header">
            <h1>⚖️ Equilibrium — Route Resilience</h1>
            <p style="color:#94a3b8; margin: 8px 0 12px 0; font-size:0.95rem;">
                Occlusion-Robust Road Extraction &amp; Graph-Theoretic Criticality Analysis
            </p>
            <span class="eq-badge">🛰️ Cartosat / Resourcesat LISS-IV</span>
            <span class="eq-badge">🧠 Attention U-Net</span>
            <span class="eq-badge">🔗 MST Healing</span>
            <span class="eq-badge">📊 Betweenness Centrality</span>
            <span class="eq-badge">💥 Resilience Index</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Dependency check ──────────────────────────────────────────────────────
    missing = []
    if not seg.SKIMAGE_OK: missing.append("scikit-image")
    if not SKNW_OK:        missing.append("sknw")
    if missing:
        st.error(
            f"Missing packages: **{', '.join(missing)}**\n\n"
            f"Run: `pip install {' '.join(missing)}`"
        )
        return

    # ── Settings panel ────────────────────────────────────────────────────────
    with st.expander("⚙️ Pipeline Settings", expanded=False):
        col_s1, col_s2, col_s3 = st.columns(3)
        with col_s1:
            extraction_mode = st.radio(
                "Extraction engine",
                ["classical", "dl"],
                format_func=lambda x: ("🧮 Classical CV (fast, no GPU)"
                                       if x == "classical"
                                       else "🧠 Attention U-Net (DL, ~2s/tile)"),
                index=0,
                key="eq_mode",
                help="Classical CV = always works. DL = requires PyTorch (already installed)"
            )
            occlusion_comp = st.checkbox(
                "CLAHE occlusion compensation",
                value=True, key="eq_clahe",
                help="Boosts local contrast to recover roads under shadows/canopy"
            )
            clahe_clip = st.slider("CLAHE clip limit", 1.0, 8.0, 3.0, 0.5,
                                   key="eq_clahe_clip")

        with col_s2:
            max_gap_px = st.slider(
                "Max healing gap (pixels)", 10, 120, 50, 5,
                key="eq_gap",
                help="Skeleton fragments within this many pixels are bridge candidates"
            )
            angle_tol = st.slider(
                "Angle tolerance (°)", 15.0, 90.0, 45.0, 5.0,
                key="eq_angle",
                help="Max direction deviation allowed for a healing bridge"
            )

        with col_s3:
            st.markdown("**Legend**")
            st.markdown(
                "🔴 Critical bottleneck roads  \n"
                "🟡 Gatekeeper Nodes  \n"
                "🟠 Articulation points  \n"
                "🟢 Healing bridges (synthetic)  \n"
                "🔵 Normal road segments"
            )

    # ── Upload ────────────────────────────────────────────────────────────────
    uploaded = st.file_uploader(
        "Upload a satellite / aerial image (GeoTIFF, JPEG, PNG)",
        type=["jpg", "jpeg", "png", "tif", "tiff"],
        key="eq_uploader",
    )

    if uploaded is None:
        st.markdown(
            """
            <div style="
                border: 2px dashed #334155;
                border-radius: 12px;
                padding: 40px;
                text-align: center;
                color: #64748b;
                margin: 24px 0;
            ">
                <div style="font-size: 3rem;">🛰️</div>
                <div style="font-size: 1.1rem; margin-top: 8px;">
                    Upload a satellite tile to begin the pipeline
                </div>
                <div style="font-size: 0.85rem; margin-top: 8px; color: #475569;">
                    Works best on imagery showing visible road networks —
                    a city block, neighbourhood, or small district.<br>
                    Try: Sentinel-2, Resourcesat LISS-IV, Cartosat-3,
                    or any Google Maps satellite screenshot.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    file_bytes = uploaded.getvalue()

    # Quick preview before running
    if "eq_result" not in st.session_state or \
       st.session_state.get("eq_last_file") != uploaded.name:
        pil_preview = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        col_prev, col_info = st.columns([1, 1])
        with col_prev:
            st.image(pil_preview, caption="Uploaded image", use_container_width=True)
        with col_info:
            w, h = pil_preview.size
            st.markdown(f"**File:** `{uploaded.name}`")
            st.markdown(f"**Dimensions:** {w} × {h} px")
            st.markdown(f"**Size:** {len(file_bytes)/1024:.1f} KB")

    # ── Run button ────────────────────────────────────────────────────────────
    col_run, col_rst = st.columns([3, 1])
    with col_run:
        run_btn = st.button(
            "🚀 Extract Road Network & Analyze",
            type="primary", key="eq_run",
            use_container_width=True,
        )
    with col_rst:
        if st.button("🔄 Reset", key="eq_reset", use_container_width=True):
            for key in ["eq_result", "eq_last_file", "eq_sim_result", "eq_sim_node"]:
                st.session_state.pop(key, None)
            st.rerun()

    if run_btn:
        steps = [
            "Phase I/A — Extracting road mask…",
            "Phase I/B — Skeletonizing centrelines…",
            "Phase I/C — Building raw graph…",
            "Phase II  — MST topological healing…",
            "Phase III — Betweenness & criticality…",
        ]
        progress = st.progress(0.0, text=steps[0])

        try:
            progress.progress(0.1, text=steps[0])
            result = _run_pipeline(
                file_bytes,
                extraction_mode=extraction_mode,
                occlusion_comp=occlusion_comp,
                clahe_clip=clahe_clip,
                max_gap_px=max_gap_px,
                angle_tolerance=angle_tol,
            )
            progress.progress(1.0, text="✅ Pipeline complete!")
        except Exception as e:
            progress.empty()
            st.error(f"Pipeline failed: {e}")
            raise

        st.session_state["eq_result"]    = result
        st.session_state["eq_last_file"] = uploaded.name
        st.session_state.pop("eq_sim_result", None)
        st.rerun()

    # ── Results rendering ─────────────────────────────────────────────────────
    if "eq_result" not in st.session_state:
        return

    res = st.session_state["eq_result"]
    bc  = res["bc_result"]

    # Summary banner
    heal_meta = res["heal_meta"]
    n_crit = len(res["gk_nodes"])
    st.success(
        f"✅ Extracted **{bc['total_nodes']} junctions** and "
        f"**{bc['total_edges']} road segments** · "
        f"Healed **{heal_meta['bridges_added']} gap(s)** · "
        f"**{n_crit} Gatekeeper Node(s)** identified · "
        f"GNE = **{res['gne_baseline']:.5f}**"
    )

    # ── Phase tabs ─────────────────────────────────────────────────────────────
    phase_tabs = st.tabs([
        "🔬 Phase I — Segmentation",
        "🔗 Phase II — Healing",
        "📊 Phase III — Criticality",
        "🗺️ Phase IV — Dashboard",
        "💥 Phase IV — Simulation",
    ])

    with phase_tabs[0]:
        _section_pipeline_stages(res)

    with phase_tabs[1]:
        _section_healing(res)

    with phase_tabs[2]:
        _section_criticality(res)

    with phase_tabs[3]:
        _section_heatmap(res)

    with phase_tabs[4]:
        _section_simulation(res)
