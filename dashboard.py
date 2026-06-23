"""
dashboard.py — Unified Crowd Safety Dashboard
==============================================
Everything in one scrollable page:

  1. Status banner        — big, colour-coded, immediate
  2. Live metrics         — count, capacity, % full
  3. Action checklist     — pre-written, tickable
  4. Alert contacts       — who to call right now
  5. Crowd trend graph    — persisted in PostgreSQL
  6. Venue capacity tool  — draw on map, get area + density table
  7. Exit route planning  — search start + exit, get all routes + PA script
  8. Incident history     — past Warning/Critical events

Count flows in automatically from Analyze tab. Zero re-entry.
"""

from __future__ import annotations
import math
import streamlit as st
import pandas as pd
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium
import db
import routing_engine as routing

# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────

LOCATION_TYPES = [
    "Railway / Metro Station",
    "Temple / Religious Site",
    "Big Event / Rally / Concert",
    "Market / Shopping Area",
    "Stadium / Sports Venue",
    "Public Transport Hub",
    "Emergency / Disaster Site",
]

DEFAULT_CAPACITIES = {
    "Railway / Metro Station":     2000,
    "Temple / Religious Site":     1500,
    "Big Event / Rally / Concert": 5000,
    "Market / Shopping Area":      1200,
    "Stadium / Sports Venue":      8000,
    "Public Transport Hub":        800,
    "Emergency / Disaster Site":   500,
}

ACTIONS = {
    "Railway / Metro Station": {
        "Warning": [
            "📢 PA: 'Dear passengers, please move to all available platforms and waiting areas.'",
            "🚔 Deploy RPF staff to manage queue at entry stairs",
            "🎟️ Open extra ticket counters — slow down new entries",
            "📋 Re-check crowd count in 5 minutes",
        ],
        "Critical": [
            "🚫 Close main entry gate immediately — stop all new entries",
            "🚔 Call RPF In-charge and District Police NOW",
            "📢 PA: 'Platform at maximum capacity. Entry temporarily closed. Wait outside calmly.'",
            "🚉 Hold next train departure until platform crowd reduces",
            "🔄 Redirect all arriving passengers to alternate entrance",
        ],
    },
    "Temple / Religious Site": {
        "Warning": [
            "⏳ Switch to slow entry — max 50 people/min through main gate",
            "🚪 Open alternate side gate or rear darshan entrance",
            "📢 PA: 'Devotees, please maintain the queue. Do not push.'",
            "🎫 Pause token distribution for 10 minutes",
        ],
        "Critical": [
            "🚫 Close main gate — stop all entries immediately",
            "🚨 Alert Temple Security Head, Police Liaison, District Collector",
            "📢 PA: 'Temple at full capacity. Entry temporarily closed for safety.'",
            "🔄 Stop VIP darshan — convert to exit-only flow",
            "🏥 Move medical team to main gate now",
        ],
    },
    "Big Event / Rally / Concert": {
        "Warning": [
            "🚪 Open all entry gates simultaneously to spread crowd",
            "📢 PA: Guide crowd toward sections C and D which have space",
            "👮 Deploy medical team to highest-density zone",
            "🎯 Limit entry to max 100 people per gate per minute",
        ],
        "Critical": [
            "🚫 Stop all entries — switch to exit-only mode immediately",
            "📢 PA: 'Venue at maximum safe capacity. Entry now closed.'",
            "🚨 Alert Event Director, Police Liaison, Fire Marshal NOW",
            "🆘 Open and secure ALL emergency exits immediately",
            "📋 Begin section-by-section exit announcements over PA",
        ],
    },
    "Market / Shopping Area": {
        "Warning": [
            "↔️ Enforce one-way pedestrian flow",
            "👮 Increase police patrol in most crowded lane",
            "🚫 Block two-wheeler and delivery vehicle entry",
        ],
        "Critical": [
            "🚫 Close lane to new entries — deploy barricades",
            "📢 PA: 'Market overcrowded. Exit via designated routes calmly.'",
            "🚨 Call Police Inspector and Fire Officer",
            "↔️ Exit direction only — no incoming movement",
        ],
    },
    "Stadium / Sports Venue": {
        "Warning": [
            "🆘 Physically check all emergency exits are clear and unlocked",
            "📢 Prepare section-wise exit announcement",
            "👮 Place marshals at all corridor exits",
        ],
        "Critical": [
            "🚫 Stop ticket scanning — no further entry at any gate",
            "📢 PA: 'Venue at full capacity. No further entry for safety.'",
            "🚨 Alert Police SP, Fire Officer, Event Organizer",
            "🆘 Open all emergency exits — marshal at each",
        ],
    },
    "Public Transport Hub": {
        "Warning": [
            "🚌 Request extra bus/ferry to this stop immediately",
            "🪢 Set up rope barriers for orderly boarding queue",
            "📋 Update display board: 'Extra vehicle arriving ~8 min'",
        ],
        "Critical": [
            "🚫 Stop boarding current vehicle — at capacity",
            "📢 PA: 'Move to alternate stop. Extra vehicles being dispatched.'",
            "🚨 Alert Transport Authority and Local Police",
            "🚌 Deploy extra vehicles for rest of the day",
        ],
    },
    "Emergency / Disaster Site": {
        "Warning": [
            "🚨 Alert rescue team — begin occupancy logging",
            "🔍 Check all stairwells and exit routes are clear",
            "🏥 Brief fire and medical units on current count",
        ],
        "Critical": [
            "🚫 Stop new entries — create clear rescue lane",
            "📋 Share current occupancy with Incident Commander immediately",
            "🚨 Deploy Medical and Fire teams to most crowded area now",
            "🔼 Evacuate upper floors downward — alert fire officer",
        ],
    },
}

CONTACTS = {
    "Railway / Metro Station":     "RPF In-charge · Station Director · District Police",
    "Temple / Religious Site":     "Temple Security Head · Police Liaison · District Collector",
    "Big Event / Rally / Concert": "Event Director · Police Liaison · Fire Marshal",
    "Market / Shopping Area":      "Beat Constable · Police Inspector · Municipal Officer",
    "Stadium / Sports Venue":      "Organizer · Fire Safety Officer · Police SP",
    "Public Transport Hub":        "Depot Manager · Route Controller · Local Police",
    "Emergency / Disaster Site":   "Incident Commander · Fire & Rescue · Medical Control",
}

DENSITY_LEVELS = [
    ("🟢  1 per 10 m²  (Very sparse — open field)",     0.10),
    ("🟢  1 per 4 m²   (Sparse — loose crowd)",         0.25),
    ("🟡  1 per 2.5 m² (Comfortable standing)",         0.40),
    ("🟡  1 per 1.4 m² (Typical event crowd)",          0.71),
    ("🟠  1 per 1 m²   (Dense crowd)",                  1.00),
    ("🔴  1 per 0.5 m² (Very dense — packed)",          2.00),
    ("🔴  1 per 0.27 m² (⚠️ Dangerous — crush risk)", 3.70),
]

STATUS_COLORS = {"Safe": "#1e7e34", "Warning": "#856404", "Critical": "#721c24"}
STATUS_BG     = {"Safe": "#d4edda", "Warning": "#fff3cd", "Critical": "#f8d7da"}
STATUS_BORDER = {"Safe": "#28a745", "Warning": "#ffc107", "Critical": "#dc3545"}
STATUS_ICONS  = {"Safe": "✅", "Warning": "⚠️", "Critical": "🚨"}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _classify(count, capacity):
    r = count / capacity if capacity > 0 else 0.0
    if r >= 0.90: return "Critical", r
    if r >= 0.70: return "Warning",  r
    return "Safe", r


def _poly_area_sqm(geometry):
    try:
        from pyproj import Geod
        from shapely.geometry import shape
        geod = Geod(ellps="WGS84")
        area, _ = geod.geometry_area_perimeter(shape(geometry))
        return abs(area)
    except Exception:
        pass
    try:
        coords = geometry.get("coordinates", [[]])[0]
        if len(coords) < 3: return 0.0
        R, n, total = 6_371_000, len(coords), 0.0
        for i in range(n):
            lon1, lat1 = coords[i]
            lon2, lat2 = coords[(i + 1) % n]
            total += math.radians(lon2 - lon1) * (
                2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2)))
        return abs(total * R * R / 2)
    except Exception:
        return 0.0


def _section(title, subtitle=""):
    """Consistent section header styling."""
    st.markdown(f"### {title}")
    if subtitle:
        st.caption(subtitle)


# ─────────────────────────────────────────────────────────────────────────────
# Main render
# ─────────────────────────────────────────────────────────────────────────────

def render(crowd_count: int = 0, crowd_source: str = ""):

    # ══════════════════════════════════════════════════════════════════
    # TOP — venue setup (collapsed by default, open if no count yet)
    # ══════════════════════════════════════════════════════════════════
    with st.expander("⚙️ Venue Setup — location type & safe capacity",
                     expanded=(crowd_count == 0)):
        s1, s2 = st.columns([2, 1])
        with s1:
            st.selectbox("Venue type", LOCATION_TYPES, key="db_location")
        with s2:
            loc = st.session_state.get("db_location", LOCATION_TYPES[0])
            st.number_input(
                "Safe capacity (max people)",
                min_value=10,
                value=int(st.session_state.get("db_capacity",
                           DEFAULT_CAPACITIES.get(loc, 1000))),
                step=50, key="db_capacity",
            )

    location = st.session_state.get("db_location", LOCATION_TYPES[0])
    capacity = int(st.session_state.get("db_capacity",
                    DEFAULT_CAPACITIES.get(location, 1000)))

    # DB zone
    zone_id, db_ok = None, False
    try:
        zone_id = db.get_or_create_zone(location, location, capacity)
        db_ok = True
    except Exception as e:
        st.toast(f"DB unavailable: {e}", icon="⚠️")

    # ══════════════════════════════════════════════════════════════════
    # SECTION 1 — LIVE STATUS (most important, always visible)
    # ══════════════════════════════════════════════════════════════════
    st.markdown("---")

    # Manual count input if nothing from analyze tab
    if crowd_count == 0:
        st.info(
            "👈 No crowd count yet — go to **Analyze Crowd** tab, upload a photo "
            "or video, and the count will appear here automatically. "
            "Or enter it manually below."
        )
        manual = st.number_input("Enter crowd count manually:",
                                  min_value=0, value=0, step=1, key="db_manual")
        crowd_count = manual if manual > 0 else 0

    if crowd_count == 0:
        st.caption("Waiting for crowd count…")
        return

    status, ratio = _classify(crowd_count, capacity)

    # Persist to DB (skip duplicate reruns)
    if db_ok:
        lk = (zone_id, crowd_count, crowd_source, status)
        if st.session_state.get("db_last_logged") != lk:
            try:
                db.log_reading(zone_id, crowd_count, status, source=crowd_source)
                inc = db.get_open_incident(zone_id)
                if status in ("Warning", "Critical"):
                    if inc is None: db.open_incident(zone_id, status, crowd_count)
                    else: db.update_incident_peak(inc["id"], crowd_count)
                else:
                    if inc is not None: db.close_incident(inc["id"])
                st.session_state["db_last_logged"] = lk
            except Exception as e:
                st.toast(f"Could not save reading: {e}", icon="⚠️")

    # Big status banner
    st.markdown(
        f"""<div style="background:{STATUS_BG[status]};border-left:8px solid
        {STATUS_BORDER[status]};border-radius:10px;padding:22px 28px;margin-bottom:8px;">
        <div style="font-size:32px;font-weight:800;color:{STATUS_COLORS[status]};">
        {STATUS_ICONS[status]}&nbsp;{status.upper()}</div>
        <div style="font-size:16px;color:{STATUS_COLORS[status]};margin-top:6px;">
        {"All clear — crowd is within safe limits. Continue monitoring."
          if status == "Safe" else
         "Crowd is getting large. Start preparation steps below."
          if status == "Warning" else
         "OVERCROWDED — Take action immediately using the checklist below."}
        </div></div>""",
        unsafe_allow_html=True,
    )

    # Metrics
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("👥 People at venue",  f"{crowd_count:,}")
    m2.metric("🏟️ Safe capacity",    f"{capacity:,}")
    m3.metric("📊 Venue is",          f"{ratio * 100:.0f}% full")
    m4.metric("📍 Venue type",        location.split("/")[0].strip())
    if crowd_source:
        st.caption(f"Count source: {crowd_source}")

    # ══════════════════════════════════════════════════════════════════
    # SECTION 2 — ACTION CHECKLIST (only when Warning / Critical)
    # ══════════════════════════════════════════════════════════════════
    if status in ("Warning", "Critical"):
        st.markdown("---")
        _section(
            f"{'🚨 Immediate Actions Required' if status == 'Critical' else '⚠️ Preparation Steps'}",
            f"{'Act NOW — do not delay.' if status == 'Critical' else 'Start before it gets worse.'}"
        )
        actions = ACTIONS.get(location, {}).get(status, [])
        for i, action in enumerate(actions, 1):
            st.checkbox(action, key=f"chk_{location}_{status}_{i}")

        contacts = CONTACTS.get(location, "Security and local authorities")
        st.markdown(
            f'<div style="background:#e8f4fd;border-radius:8px;'
            f'padding:14px 18px;margin-top:12px;">'
            f'📞 <b>Alert these people:</b><br>'
            f'<span style="font-size:15px;">{contacts}</span></div>',
            unsafe_allow_html=True,
        )

    # ══════════════════════════════════════════════════════════════════
    # SECTION 3 — CROWD TREND GRAPH
    # ══════════════════════════════════════════════════════════════════
    st.markdown("---")
    _section("📈 Crowd Trend",
             "How the crowd count has changed over time — saved across sessions.")

    history = []
    if db_ok:
        try: history = db.get_recent_readings(zone_id, limit=100)
        except Exception: pass

    if len(history) > 1:
        df = pd.DataFrame(history)
        df["recorded_at"] = pd.to_datetime(df["recorded_at"])
        st.line_chart(df.set_index("recorded_at")[["count"]], height=200)
        st.caption(
            f"Warning threshold (70%): {int(capacity * 0.70):,}  ·  "
            f"Critical threshold (90%): {int(capacity * 0.90):,}  ·  "
            f"Safe capacity: {capacity:,}"
        )
    else:
        st.caption("Trend graph appears after multiple readings are saved.")

    # ══════════════════════════════════════════════════════════════════
    # SECTION 4 — VENUE CAPACITY ESTIMATION (MapChecking method)
    # ══════════════════════════════════════════════════════════════════
    st.markdown("---")
    _section("🗺️ Venue Area & Crowd Capacity",
             "Draw your venue on the map to calculate how many people it can safely hold.")

    vc1, vc2 = st.columns([3, 1])
    with vc1:
        venue_search = st.text_input(
            "venue_search_input",
            placeholder="Search for your venue — e.g. Parul University, Vadodara",
            key="db_venue_search",
            label_visibility="collapsed",
        )
    with vc2:
        if st.button("🔍 Find on map", key="db_venue_search_btn", use_container_width=True):
            if venue_search.strip():
                with st.spinner("Locating…"):
                    res = routing.geocode_multi(venue_search, limit=4)
                st.session_state["db_venue_results"] = res
                if res:
                    st.session_state["db_map_center"] = [res[0]["lat"], res[0]["lon"]]
                else:
                    st.error("Not found — try adding city name.")

    vr = st.session_state.get("db_venue_results", [])
    if vr and len(vr) > 1:
        vsel = st.selectbox("Select location:", vr,
                            format_func=lambda x: x["address"], key="db_venue_sel")
        if st.button("📍 Go here", key="db_venue_goto", use_container_width=True):
            st.session_state["db_map_center"] = [vsel["lat"], vsel["lon"]]
            st.rerun()

    map_center = st.session_state.get("db_map_center", [22.3072, 73.1812])
    m_draw = folium.Map(location=map_center, zoom_start=16, tiles="CartoDB positron")
    Draw(
        export=False,
        draw_options={"polygon": True, "rectangle": True, "polyline": False,
                      "circle": False, "marker": False, "circlemarker": False},
        edit_options={"edit": True, "remove": True},
    ).add_to(m_draw)

    draw_result = st_folium(m_draw, width="100%", height=400,
                            key="db_venue_draw", returned_objects=["all_drawings"])
    st.caption("👆 Draw a polygon or rectangle around your venue using the toolbar on the left.")

    drawings = draw_result.get("all_drawings") or []
    polygons = [d for d in drawings
                if d.get("geometry", {}).get("type") in ("Polygon", "MultiPolygon")]

    if polygons:
        area_sqm = _poly_area_sqm(polygons[-1]["geometry"])
        if area_sqm > 0:
            dc1, dc2 = st.columns(2)
            dc1.metric("Area", f"{area_sqm:,.0f} m²" if area_sqm < 10_000
                       else f"{area_sqm / 10_000:.2f} hectares")
            dc2.metric("Current crowd density",
                       f"{crowd_count / area_sqm:.2f} p/m²" if area_sqm > 0 else "—")

            cap_rows = [{"Crowd Density": lbl,
                         "Max People": f"{max(0, int(area_sqm * d)):,}",
                         "sqm/person": f"{1/d:.1f}"}
                        for lbl, d in DENSITY_LEVELS]
            st.dataframe(pd.DataFrame(cap_rows), use_container_width=True, hide_index=True)

            # Show current crowd vs area
            if crowd_count > 0:
                sqm_pp = area_sqm / crowd_count
                if sqm_pp <= 0.27:   lbl, col2 = "🔴 DANGEROUS — crush risk", "#dc3545"
                elif sqm_pp <= 0.5:  lbl, col2 = "🔴 Very dense — packed",    "#dc3545"
                elif sqm_pp <= 1.0:  lbl, col2 = "🟠 Dense crowd",             "#fd7e14"
                elif sqm_pp <= 1.4:  lbl, col2 = "🟡 Typical event crowd",     "#ffc107"
                elif sqm_pp <= 2.5:  lbl, col2 = "🟡 Comfortable standing",    "#ffc107"
                else:                lbl, col2 = "🟢 Sparse — safe",           "#28a745"
                st.markdown(
                    f'<div style="background:#f8f9fa;border-radius:8px;'
                    f'padding:14px 18px;border-left:5px solid {col2};margin-top:8px;">'
                    f'<b>{crowd_count:,} people in {area_sqm:,.0f} m² = '
                    f'{sqm_pp:.1f} m²/person</b><br>'
                    f'<span style="color:{col2};font-size:15px;"><b>{lbl}</b></span>'
                    f'</div>', unsafe_allow_html=True)
        else:
            st.warning("Could not calculate area — try redrawing the shape.")
    else:
        st.info("👆 Draw a shape above to see capacity estimates.")

    # ══════════════════════════════════════════════════════════════════
    # SECTION 5 — EXIT ROUTE PLANNING
    # ══════════════════════════════════════════════════════════════════
    st.markdown("---")
    _section("🛣️ Crowd Dispersal — Plan Exit Routes",
             "Search start + exit by name. The system finds all walking routes "
             "and creates a phased dispersal plan automatically.")

    if crowd_count > 0:
        st.success(f"👥 Crowd count for dispersal: **{crowd_count:,} people**")

    # Start location
    st.markdown("#### 📍 Where is the crowd now?")
    rs1, rs2 = st.columns([3, 1])
    with rs1:
        start_q = st.text_input("start_loc", placeholder="e.g. Parul University, Vadodara",
                                key="db_start_q", label_visibility="collapsed")
    with rs2:
        if st.button("🔍 Search", key="db_start_btn", use_container_width=True):
            if start_q.strip():
                with st.spinner("Searching…"):
                    res = routing.geocode_multi(start_q, limit=5)
                st.session_state["db_start_results"] = res
                if not res: st.error("Not found. Add city name.")

    sr = st.session_state.get("db_start_results", [])
    if sr:
        sels = st.selectbox("Select start:", sr,
                            format_func=lambda x: x["address"], key="db_start_sel")
        if st.button("✅ Confirm Start", key="db_start_confirm", type="primary"):
            st.session_state["db_start"] = sels
            for k in ("db_graph", "db_routes", "db_plan"): st.session_state.pop(k, None)
            st.rerun()

    start_pt = st.session_state.get("db_start")
    if start_pt:
        st.success(f"📍 **Start:** {start_pt['address']}")

    # Exit location
    st.markdown("#### 🏁 Where should people go?")
    re1, re2 = st.columns([3, 1])
    with re1:
        end_q = st.text_input("end_loc", placeholder="e.g. Waghodiya Crossway, Vadodara",
                              key="db_end_q", label_visibility="collapsed")
    with re2:
        if st.button("🔍 Search", key="db_end_btn", use_container_width=True):
            if end_q.strip():
                with st.spinner("Searching…"):
                    res = routing.geocode_multi(end_q, limit=5)
                st.session_state["db_end_results"] = res
                if not res: st.error("Not found. Add city name.")

    er = st.session_state.get("db_end_results", [])
    if er:
        sele = st.selectbox("Select exit:", er,
                            format_func=lambda x: x["address"], key="db_end_sel")
        if st.button("✅ Confirm Exit", key="db_end_confirm", type="primary"):
            st.session_state["db_end"] = sele
            for k in ("db_graph", "db_routes", "db_plan"): st.session_state.pop(k, None)
            st.rerun()

    end_pt = st.session_state.get("db_end")
    if end_pt:
        st.success(f"🏁 **Exit:** {end_pt['address']}")

    # Generate button
    if start_pt and end_pt:
        dist_m   = routing.haversine_m(start_pt["lat"], start_pt["lon"],
                                        end_pt["lat"],   end_pt["lon"])
        mid_lat  = (start_pt["lat"] + end_pt["lat"]) / 2
        mid_lon  = (start_pt["lon"] + end_pt["lon"]) / 2
        radius   = int(dist_m / 2 * 1.35) + 300

        st.info(f"📏 Distance: **{dist_m/1000:.2f} km** · Graph radius: **{radius:,} m**")

        disperse_n = st.number_input("People to disperse:", min_value=1,
                                      value=max(1, crowd_count), step=10, key="db_crowd")

        if st.button("🚀 Find All Routes & Generate Dispersal Plan",
                     type="primary", use_container_width=True, key="db_gen"):
            with st.spinner(f"Loading street map: {start_pt['address'].split(',')[0]}"
                            f" → {end_pt['address'].split(',')[0]}…"):
                G, _, err = routing.fetch_map_graph_from_point(mid_lat, mid_lon, dist=radius)

            if not G:
                st.error(f"Could not load street map. {err or ''}")
            else:
                with st.spinner("Calculating all walking routes…"):
                    routes = routing.find_routes(
                        G,
                        (start_pt["lat"], start_pt["lon"]),
                        (end_pt["lat"],   end_pt["lon"]),
                    )
                if not routes:
                    st.error("No routes found — try a different destination.")
                else:
                    plan = routing.build_dispersion_plan(
                        G, routes, total_crowd=int(disperse_n))
                    st.session_state["db_graph"]  = G
                    st.session_state["db_center"] = (mid_lat, mid_lon)
                    st.session_state["db_routes"] = routes
                    st.session_state["db_plan"]   = plan
                    # Save to DB
                    if db_ok:
                        try:
                            db.save_dispersion_plan(
                                zone_id,
                                start_pt["address"], end_pt["address"],
                                int(disperse_n), plan,
                            )
                        except Exception:
                            pass
                    st.rerun()

        # Results
        G      = st.session_state.get("db_graph")
        routes = st.session_state.get("db_routes", [])
        plan   = st.session_state.get("db_plan", {})
        center = st.session_state.get("db_center", (mid_lat, mid_lon))

        if G and routes and plan:
            st.success(
                f"✅ **{len(routes)} routes found** · "
                f"Full dispersal in **{plan.get('total_time_min', 0)} min**"
            )

            # Route map
            st.markdown("#### 🗺️ Route Map")
            st.caption(
                "Each colour = different route. Numbers = phase order. "
                "🔴 Start · 🟢 Exit."
            )
            m_routes = routing.plot_routes_on_map(G, routes, center, dispersion_plan=plan)
            folium.Marker([start_pt["lat"], start_pt["lon"]],
                          icon=folium.Icon(color="red",   icon="users", prefix="fa"),
                          tooltip="🔴 Crowd here").add_to(m_routes)
            folium.Marker([end_pt["lat"],   end_pt["lon"]],
                          icon=folium.Icon(color="green", icon="flag",  prefix="fa"),
                          tooltip="🏁 Safe exit").add_to(m_routes)
            st_folium(m_routes, width="100%", height=480,
                      key="db_route_map", returned_objects=[])

            # Phase plan
            st.markdown("#### 📋 Phased Dispersal Plan")
            st.caption("Release each group in order. Wait for each group to start moving before releasing the next.")
            for p in plan.get("phases", []):
                with st.container(border=True):
                    pc1, pc2 = st.columns([1, 3])
                    with pc1:
                        st.markdown(f"### Phase {p['phase']}")
                        st.caption(f"Release at T+{p['start_min']} min")
                    with pc2:
                        pp1, pp2, pp3 = st.columns(3)
                        pp1.metric("People",    f"{p['crowd']:,}")
                        pp2.metric("Distance",  f"{p['length_m']} m")
                        pp3.metric("Walk time", f"{p['arrival_min']} min")

            # PA Script
            st.markdown("#### 📢 PA Announcement Script")
            for i, line in enumerate(plan.get("pa_script", []), 1):
                st.info(f"**{i}.** {line}")

            if st.button("🔄 Plan new route", key="db_reset", use_container_width=True):
                for k in ("db_start", "db_end", "db_start_results", "db_end_results",
                          "db_graph", "db_routes", "db_plan", "db_center"):
                    st.session_state.pop(k, None)
                st.rerun()

    elif start_pt and not end_pt:
        st.info("👆 Now set the exit destination above.")
    elif not start_pt:
        st.info("👆 Set the starting location above to begin.")

    # ══════════════════════════════════════════════════════════════════
    # SECTION 6 — INCIDENT HISTORY
    # ══════════════════════════════════════════════════════════════════
    if db_ok:
        try:
            incidents = db.get_incident_history(zone_id, limit=5)
        except Exception:
            incidents = []

        if incidents:
            st.markdown("---")
            _section("🗂️ Past Incidents at this Venue",
                     "Warning and Critical events saved automatically.")
            for inc in incidents:
                state = "🟢 Resolved" if inc["ended_at"] else "🔴 Ongoing"
                started = inc["started_at"]
                started_str = (started.strftime("%d %b %H:%M")
                               if hasattr(started, "strftime") else str(started)[:16])
                st.markdown(
                    f"**{inc['status']}** — peak **{inc['peak_count']:,}** people · "
                    f"started {started_str} · {state}"
                )
