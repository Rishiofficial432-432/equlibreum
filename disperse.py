"""
disperse.py — Plan Exit Routes tab
─────────────────────────────────────────────────────────────────────────────
Three things in one clean flow:

  1. DRAW YOUR VENUE on the map → area in m² → crowd capacity at every density
  2. SEE IF YOUR CROWD FITS — AI count vs drawn area
  3. PLAN EXIT ROUTES — choose transport mode, set start + exit,
     get multiple A*-optimal routes with phased dispersal plan + PA script

Transport modes: 🚶 Walking · 🚲 Cycling · 🛵 Motorcycle · 🚗 Car
Distances shown: km (not metres)
Algorithm       : A* with Haversine heuristic (Google Maps approach)
"""

from __future__ import annotations
import math
import streamlit as st
import pandas as pd
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium
import routing_engine as routing

# ─────────────────────────────────────────────────────────────────────────────
# Density presets (mapchecking.com method)
# ─────────────────────────────────────────────────────────────────────────────

DENSITY_LEVELS = [
    ("🟢  1 per 10 m²  (Very sparse — open field)",       0.10),
    ("🟢  1 per 4 m²   (Sparse — loose crowd)",           0.25),
    ("🟡  1 per 2.5 m² (Comfortable standing)",           0.40),
    ("🟡  1 per 1.4 m² (Typical event crowd)",            0.71),
    ("🟠  1 per 1 m²   (Dense crowd)",                    1.00),
    ("🔴  1 per 0.5 m² (Very dense — packed)",            2.00),
    ("🔴  1 per 0.27 m² (⚠️ Dangerous — crush risk)",    3.70),
]

# Transport mode definitions for UI (mirrors routing_engine.TRANSPORT_MODES)
MODES = {
    "walk":       {"label": "🚶 Walking",               "color": "#27AE60"},
    "cycle":      {"label": "🚲 Cycling / 2-Wheeler",   "color": "#F39C12"},
    "motorcycle": {"label": "🛵 Motorcycle / Scooter",  "color": "#3498DB"},
    "car":        {"label": "🚗 Car / 4-Wheeler",       "color": "#E74C3C"},
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _poly_area_sqm(geometry: dict) -> float:
    try:
        from pyproj import Geod
        from shapely.geometry import shape
        geod = Geod(ellps="WGS84")
        area, _ = geod.geometry_area_perimeter(shape(geometry))
        return abs(area)
    except ImportError:
        pass
    try:
        coords = geometry.get("coordinates", [[]])[0]
        if len(coords) < 3:
            return 0.0
        R, n, total = 6_371_000, len(coords), 0.0
        for i in range(n):
            lon1, lat1 = coords[i]
            lon2, lat2 = coords[(i + 1) % n]
            total += math.radians(lon2 - lon1) * (
                2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2))
            )
        return abs(total * R * R / 2)
    except Exception:
        return 0.0


def _capacity_at_density(area_sqm: float, density: float) -> int:
    return max(0, int(area_sqm * density))


def _fmt_km(metres: float) -> str:
    """Format distance — always in km with 2 decimal places."""
    return f"{metres / 1000:.2f} km"


def _mode_graph_radius(dist_m: float) -> int:
    """How large a graph to fetch based on straight-line distance."""
    return int(dist_m / 2 * 1.4) + 400


# ─────────────────────────────────────────────────────────────────────────────
# Main render
# ─────────────────────────────────────────────────────────────────────────────

def render(crowd_count: int = 0):
    st.markdown("## 🗺️ Plan Exit Routes")
    st.markdown(
        "**Part 1** — Draw your venue on the map to calculate crowd capacity.  \n"
        "**Part 2** — Choose a transport mode, set start & exit, "
        "get A\\*-optimised routes with full dispersal plan."
    )
    st.divider()

    # ═════════════════════════════════════════════════════════════════════
    # PART 1 — VENUE AREA & CAPACITY
    # ═════════════════════════════════════════════════════════════════════
    st.markdown("### Part 1 — How many people fit in your venue?")
    st.markdown(
        "Draw a shape around your venue. The system calculates area and "
        "shows capacity at every crowd density level."
    )

    mc1, mc2 = st.columns([3, 1])
    with mc1:
        map_search = st.text_input(
            "venue_search",
            placeholder="e.g. Lal Darwaja, Vadodara  or  Parul University",
            key="map_search_q",
            label_visibility="collapsed",
        )
    with mc2:
        if st.button("🔍 Find on map", key="map_search_btn", width="stretch"):
            if map_search.strip():
                with st.spinner("Locating…"):
                    results = routing.geocode_multi(map_search, limit=4)
                st.session_state["map_search_results"] = results
                if results:
                    st.session_state["map_center"] = [results[0]["lat"], results[0]["lon"]]
                else:
                    st.error("Could not find this location. Try adding the city name.")

    map_results = st.session_state.get("map_search_results", [])
    if map_results and len(map_results) > 1:
        sel = st.selectbox("Select the correct location:", map_results,
                           format_func=lambda x: x["address"], key="map_search_sel")
        if st.button("📍 Go to this location", key="map_goto"):
            st.session_state["map_center"] = [sel["lat"], sel["lon"]]
            st.rerun()

    center = st.session_state.get("map_center", [22.3072, 73.1812])
    m = folium.Map(location=center, zoom_start=16, tiles="CartoDB positron")
    Draw(
        export=False,
        draw_options={
            "polygon": True, "rectangle": True,
            "polyline": False, "circle": False,
            "marker": False, "circlemarker": False,
        },
        edit_options={"edit": True, "remove": True},
    ).add_to(m)

    draw_result = st_folium(m, width="100%", height=420,
                            key="venue_draw_map", returned_objects=["all_drawings"])
    st.caption(
        "👆 Use the toolbar on the left to draw a polygon or rectangle around your venue."
    )

    drawings = draw_result.get("all_drawings") or []
    polygons = [d for d in drawings
                if d.get("geometry", {}).get("type") in ("Polygon", "MultiPolygon")]

    if polygons:
        area_sqm = _poly_area_sqm(polygons[-1]["geometry"])
        if area_sqm > 0:
            st.divider()
            st.markdown("#### 📐 Results for your drawn area")
            ca1, ca2 = st.columns(2)
            ca1.metric("Area",
                       f"{area_sqm:,.0f} m²" if area_sqm < 10_000
                       else f"{area_sqm / 10_000:.2f} hectares")
            ca2.metric("Area (approx.)",
                       f"{area_sqm / 1_000_000:.4f} km²" if area_sqm > 100_000
                       else f"{area_sqm:.0f} sqm")

            st.markdown("#### 👥 Crowd capacity at different density levels")
            rows = []
            for label, density in DENSITY_LEVELS:
                cap = _capacity_at_density(area_sqm, density)
                rows.append({
                    "Crowd density":   label,
                    "Max people":      f"{cap:,}",
                    "sqm per person":  f"{1 / density:.1f} m²",
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

            if crowd_count > 0:
                st.markdown("#### Your current crowd vs this area")
                cur_density = crowd_count / area_sqm
                cur_sqm_pp  = area_sqm / crowd_count

                if cur_sqm_pp <= 0.27:
                    lbl, col = "🔴 DANGEROUS — crush risk", "#dc3545"
                elif cur_sqm_pp <= 0.5:
                    lbl, col = "🔴 Very dense — packed",   "#dc3545"
                elif cur_sqm_pp <= 1.0:
                    lbl, col = "🟠 Dense crowd",            "#fd7e14"
                elif cur_sqm_pp <= 1.4:
                    lbl, col = "🟡 Typical event crowd",    "#ffc107"
                elif cur_sqm_pp <= 2.5:
                    lbl, col = "🟡 Comfortable standing",   "#ffc107"
                else:
                    lbl, col = "🟢 Sparse — safe",          "#28a745"

                st.markdown(
                    f'<div style="background:#f8f9fa;border-radius:8px;'
                    f'padding:16px 20px;border-left:5px solid {col};">'
                    f'<b>Current crowd: {crowd_count:,} people in {area_sqm:,.0f} m²</b><br>'
                    f'<span style="font-size:15px;">'
                    f'That is <b>{cur_sqm_pp:.1f} m² per person</b> — '
                    f'<span style="color:{col}"><b>{lbl}</b></span></span></div>',
                    unsafe_allow_html=True,
                )
        else:
            st.warning("Could not calculate area. Try redrawing the shape.")
    else:
        st.info("👆 Draw a shape on the map above to see crowd capacity estimates.")

    st.divider()

    # ═════════════════════════════════════════════════════════════════════
    # PART 2 — EXIT ROUTE PLANNING (multi-modal)
    # ═════════════════════════════════════════════════════════════════════
    st.markdown("### Part 2 — Plan the exit routes")
    st.markdown(
        "Choose how people will travel, set the start and exit locations, "
        "then generate A\\*-optimised routes with a full dispersal plan."
    )

    if crowd_count > 0:
        st.success(f"👥 Using current crowd count: **{crowd_count:,} people**")
    else:
        st.info("Analyze a crowd first (Analyze Crowd tab) to auto-fill the count.")

    st.divider()

    # ── Transport mode selector ───────────────────────────────────────────
    st.markdown("#### 🚦 How will people travel?")
    st.markdown(
        "Select **all modes** that apply. The system will plan separate "
        "routes and timings for each mode of transport."
    )

    col_w, col_c, col_m, col_car = st.columns(4)
    with col_w:
        use_walk  = st.checkbox("🚶 Walking",             value=True,  key="mode_walk")
    with col_c:
        use_cycle = st.checkbox("🚲 Cycling",             value=False, key="mode_cycle")
    with col_m:
        use_moto  = st.checkbox("🛵 Motorcycle",          value=False, key="mode_moto")
    with col_car:
        use_car   = st.checkbox("🚗 Car / 4-Wheeler",     value=False, key="mode_car")

    selected_modes = []
    if use_walk:  selected_modes.append("walk")
    if use_cycle: selected_modes.append("cycle")
    if use_moto:  selected_modes.append("motorcycle")
    if use_car:   selected_modes.append("car")

    if not selected_modes:
        st.warning("Please select at least one transport mode.")
        return

    # Mode info cards
    mode_info_cols = st.columns(len(selected_modes))
    speed_map = {"walk": "~4.3 km/h", "cycle": "~15 km/h",
                 "motorcycle": "~25 km/h", "car": "~30 km/h"}
    road_map  = {"walk": "Footways, paths, streets",
                 "cycle": "Cycle lanes, roads",
                 "motorcycle": "All motorable roads",
                 "car": "All motorable roads"}
    for col, mode_key in zip(mode_info_cols, selected_modes):
        cfg = MODES[mode_key]
        with col:
            st.markdown(
                f'<div style="background:{cfg["color"]}18;border:1px solid {cfg["color"]}44;'
                f'border-radius:8px;padding:10px 14px;">'
                f'<b style="color:{cfg["color"]}">{cfg["label"]}</b><br>'
                f'<small>🏃 Speed: {speed_map[mode_key]}<br>'
                f'🛣️ {road_map[mode_key]}</small></div>',
                unsafe_allow_html=True,
            )

    st.markdown("")

    # ── Location search — Start ───────────────────────────────────────────
    st.markdown("#### 📍 Where is the crowd right now?")
    r1a, r1b = st.columns([3, 1])
    with r1a:
        start_q = st.text_input(
            "start_q",
            placeholder="e.g. Parul University, Waghodiya Road, Vadodara",
            key="dis_start_q",
            label_visibility="collapsed",
        )
    with r1b:
        if st.button("🔍 Search", key="dis_start_btn", width="stretch"):
            if start_q.strip():
                with st.spinner("Searching…"):
                    res = routing.geocode_multi(start_q, limit=5)
                st.session_state["dis_start_results"] = res
                if not res:
                    st.error("Not found. Try adding city name.")
            else:
                st.warning("Type a location name first.")

    sr = st.session_state.get("dis_start_results", [])
    if sr:
        sel_s = st.selectbox("Select start location:", sr,
                             format_func=lambda x: x["address"], key="dis_start_sel")
        if st.button("✅ Set as Start", key="dis_start_confirm", type="primary"):
            st.session_state["dis_start"] = sel_s
            for k in ("dis_graph_cache", "dis_routes_cache", "dis_plan_cache"):
                st.session_state.pop(k, None)
            st.rerun()

    start_pt = st.session_state.get("dis_start")
    if start_pt:
        st.success(f"📍 Start: {start_pt['address']}")

    st.markdown("")

    # ── Location search — Exit ────────────────────────────────────────────
    st.markdown("#### 🏁 Where should the crowd go? (Exit / Safe zone)")
    r2a, r2b = st.columns([3, 1])
    with r2a:
        end_q = st.text_input(
            "end_q",
            placeholder="e.g. Waghodiya Crossway, Vadodara",
            key="dis_end_q",
            label_visibility="collapsed",
        )
    with r2b:
        if st.button("🔍 Search", key="dis_end_btn", width="stretch"):
            if end_q.strip():
                with st.spinner("Searching…"):
                    res = routing.geocode_multi(end_q, limit=5)
                st.session_state["dis_end_results"] = res
                if not res:
                    st.error("Not found. Try adding city name.")
            else:
                st.warning("Type a destination first.")

    er = st.session_state.get("dis_end_results", [])
    if er:
        sel_e = st.selectbox("Select exit location:", er,
                             format_func=lambda x: x["address"], key="dis_end_sel")
        if st.button("✅ Set as Exit", key="dis_end_confirm", type="primary"):
            st.session_state["dis_end"] = sel_e
            for k in ("dis_graph_cache", "dis_routes_cache", "dis_plan_cache"):
                st.session_state.pop(k, None)
            st.rerun()

    end_pt = st.session_state.get("dis_end")
    if end_pt:
        st.success(f"🏁 Exit: {end_pt['address']}")

    st.markdown("")

    # ── Generate routes ───────────────────────────────────────────────────
    if start_pt and end_pt:
        dist_m   = routing.haversine_m(start_pt["lat"], start_pt["lon"],
                                        end_pt["lat"],   end_pt["lon"])
        dist_km  = dist_m / 1000.0
        mid_lat  = (start_pt["lat"] + end_pt["lat"]) / 2
        mid_lon  = (start_pt["lon"] + end_pt["lon"]) / 2
        radius   = _mode_graph_radius(dist_m)

        st.markdown("#### 🚀 Generate the exit plan")

        # Show distance info with mode-specific context
        dist_km_str = f"{dist_km:.2f} km"
        speed_map_disp = {"walk": "~4.3 km/h", "cycle": "~15 km/h",
                          "motorcycle": "~25 km/h", "car": "~30 km/h"}
        eta_strs = []
        for mk in selected_modes:
            spd = {"walk": 1.2, "cycle": 4.2, "motorcycle": 7.0, "car": 8.0}[mk]
            eta_min = round(dist_m / spd / 60, 1)
            eta_strs.append(f"{MODES[mk]['label']}: ~{eta_min} min")

        st.info(
            f"📏 Straight-line distance: **{dist_km_str}**  \n"
            + "  \n".join(f"⏱ Estimated travel time — {e}" for e in eta_strs)
        )

        disperse_n = st.number_input(
            "Number of people to disperse:",
            min_value=1,
            value=max(1, crowd_count),
            step=10,
            key="dis_crowd",
        )

        if st.button("🗺️ Find All Exit Routes",
                     type="primary", width="stretch", key="dis_generate"):

            # Build cache key — same locations + modes = skip re-download
            cache_key = (
                round(start_pt["lat"], 5), round(start_pt["lon"], 5),
                round(end_pt["lat"], 5),   round(end_pt["lon"], 5),
                tuple(sorted(selected_modes)),
            )
            cached_key = st.session_state.get("dis_cache_key")

            if cache_key == cached_key and st.session_state.get("dis_graph_cache"):
                st.info("⚡ Using cached street map — no re-download needed.")
                st.rerun()
            else:
                # PARALLEL download: all modes fetched simultaneously
                with st.spinner(
                    f"Downloading street maps for {len(selected_modes)} mode(s) — "
                    "running in parallel… (usually 10-20 s)"
                ):
                    fetch_results = routing.fetch_graphs_parallel(
                        start_pt["lat"], start_pt["lon"],
                        end_pt["lat"],   end_pt["lon"],
                        selected_modes,
                    )

                graphs: dict[str, object] = {}
                errors: list[str] = []
                for mode_key, (G, err) in fetch_results.items():
                    if G:
                        graphs[mode_key] = G
                    else:
                        errors.append(f"{MODES[mode_key]['label']}: {err or 'network error'}")

                if not graphs:
                    st.error(
                        "❌ Could not load any street maps.  \n"
                        "**Tips:**  \n"
                        "• Check your internet connection  \n"
                        "• Try locations that are closer together  \n"
                        "• Make sure the location names are correct"
                    )
                else:
                    all_routes: dict[str, list] = {}
                    all_plans:  dict[str, dict] = {}

                    with st.spinner("Computing A* routes (C-optimised)..."):
                        for mode_key, G in graphs.items():
                            routes = routing.find_routes(
                                G,
                                (start_pt["lat"], start_pt["lon"]),
                                (end_pt["lat"],   end_pt["lon"]),
                                mode=mode_key,
                            )
                            if routes:
                                all_routes[mode_key] = routes
                                all_plans[mode_key]  = routing.build_dispersion_plan(
                                    G, routes,
                                    total_crowd=int(disperse_n),
                                    mode=mode_key,
                                )

                    st.session_state["dis_graph_cache"]  = graphs
                    st.session_state["dis_center"]       = (mid_lat, mid_lon)
                    st.session_state["dis_routes_cache"] = all_routes
                    st.session_state["dis_plan_cache"]   = all_plans
                    st.session_state["dis_cache_key"]    = cache_key
                    st.rerun()

        # ── Show results ──────────────────────────────────────────────────
        graphs     = st.session_state.get("dis_graph_cache",  {})
        all_routes = st.session_state.get("dis_routes_cache", {})
        all_plans  = st.session_state.get("dis_plan_cache",   {})
        center     = st.session_state.get("dis_center", (mid_lat, mid_lon))

        if all_routes:
            total_modes = len(all_routes)
            total_routes = sum(len(r) for r in all_routes.values())
            st.success(
                f"✅ **{total_routes} exit routes found** across "
                f"**{total_modes} transport mode(s)**."
            )
            st.divider()

            # ── Tab per transport mode ────────────────────────────────────
            mode_tab_labels = [
                f"{MODES[mk]['label']} ({len(all_routes[mk])} routes)"
                for mk in all_routes
            ]
            mode_tabs = st.tabs(mode_tab_labels)

            for tab, mode_key in zip(mode_tabs, all_routes):
                G      = graphs[mode_key]
                routes = all_routes[mode_key]
                plan   = all_plans[mode_key]
                cfg    = MODES[mode_key]

                with tab:
                    phases = plan.get("phases", [])

                    # Summary metrics
                    mc1, mc2, mc3 = st.columns(3)
                    mc1.metric("Routes Found",    len(routes))
                    mc2.metric("Total Dispersal Time", f"{plan.get('total_time_min', 0)} min")
                    mc3.metric("Straight-line Dist",   f"{dist_km:.2f} km")

                    # Route map
                    st.markdown(f"##### 🗺️ {cfg['label']} — Route Map")
                    st.caption(
                        "Each coloured line is a different route.  "
                        "Phase number shown at midpoint.  "
                        "🔴 = start · 🟢 = exit."
                    )
                    m_map = routing.plot_routes_on_map(
                        G, routes, center, dispersion_plan=plan
                    )
                    folium.Marker(
                        [start_pt["lat"], start_pt["lon"]],
                        icon=folium.Icon(color="red", icon="users", prefix="fa"),
                        tooltip="🔴 Crowd is here",
                    ).add_to(m_map)
                    folium.Marker(
                        [end_pt["lat"], end_pt["lon"]],
                        icon=folium.Icon(color="green", icon="flag", prefix="fa"),
                        tooltip="🏁 Safe exit destination",
                    ).add_to(m_map)
                    st_folium(m_map, width="100%", height=480,
                              key=f"dis_map_{mode_key}", returned_objects=[])

                    st.divider()

                    # Route summary table
                    st.markdown("##### 📊 Route Summary")
                    ri_list = plan.get("routes_info", [])
                    if ri_list:
                        table_rows = []
                        for ri in ri_list:
                            ph = next((p for p in phases
                                       if p["route_index"] == ri["index"]), None)
                            table_rows.append({
                                "Route":         ri["route_label"],
                                "Distance":      f"{ri['length_km']:.2f} km",
                                "Travel Time":   f"{ri['time_min']} min",
                                "Road Width":    f"{ri['bottleneck_w']} m",
                                "Capacity":      f"{ri['capacity']:,}",
                                "Crowd Assigned": f"{ph['crowd']:,}" if ph else "—",
                                "Release At":    f"T+{ph['start_min']} min" if ph else "—",
                                "ETA":           f"{ph['arrival_min']} min" if ph else "—",
                            })
                        st.dataframe(pd.DataFrame(table_rows),
                                     width="stretch", hide_index=True)

                    st.divider()

                    # Phased dispersal plan
                    st.markdown("##### 📋 Phased Dispersal Plan")
                    st.markdown(
                        "Release each group in order. "
                        "**Wait for each group to start moving before releasing the next.**"
                    )
                    for p in phases:
                        with st.container(border=True):
                            h1, h2 = st.columns([1, 3])
                            with h1:
                                st.markdown(f"### Phase {p['phase']}")
                                st.caption(f"Release at T+{p['start_min']} min")
                            with h2:
                                p1, p2, p3, p4 = st.columns(4)
                                p1.metric("People",   f"{p['crowd']:,}")
                                p2.metric("Distance", f"{p['length_km']:.2f} km")
                                p3.metric("Travel",   f"{p['time_min']} min")
                                p4.metric("ETA",      f"{p['arrival_min']} min")

                    st.divider()

                    # PA script
                    st.markdown("##### 📢 PA Announcement Script")
                    st.markdown(
                        "Read these announcements over the PA system in order."
                    )
                    for i, line in enumerate(plan.get("pa_script", []), 1):
                        st.info(f"**Announcement {i}:** {line}")

            st.divider()
            if st.button("🔄 Search new locations", key="dis_reset"):
                for k in ("dis_start", "dis_end", "dis_start_results",
                          "dis_end_results", "dis_graph_cache",
                          "dis_routes_cache", "dis_plan_cache", "dis_center"):
                    st.session_state.pop(k, None)
                st.rerun()

    elif start_pt and not end_pt:
        st.info("👆 Now search for the exit destination above.")
    elif not start_pt:
        st.info("👆 Start by typing your current location above.")
