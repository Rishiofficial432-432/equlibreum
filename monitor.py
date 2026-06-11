"""
monitor.py — Monitor & Act tab
--------------------------------
Shows situation status immediately using the auto-filled crowd count.
No configuration required. Operator sees:
  1. Status (Safe / Warning / Critical) — big and colour-coded
  2. What to do right now — pre-written action checklist
  3. Who to call — alert contacts
  4. How the crowd has changed — trend graph (auto-populated)

Location type and capacity are set via a collapsible settings panel
so they don't clutter the main view.
"""

from __future__ import annotations
import streamlit as st
import pandas as pd
from datetime import datetime

# ── Data ──────────────────────────────────────────────────────────────────────

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
            "📢 Make PA announcement: 'Dear passengers, please move to all available platforms and waiting areas to avoid crowding.'",
            "🚔 Deploy RPF staff to manage the queue at entry stairs",
            "🎟️ Open additional ticket counters and slow down new entries",
            "📋 Check crowd count again in 5 minutes",
        ],
        "Critical": [
            "🚫 Close the main entry gate immediately — stop all new entries",
            "🚔 Call RPF In-charge and District Police right now",
            "📢 PA announcement: 'The platform has reached maximum capacity. Entry is temporarily closed. Please wait outside calmly.'",
            "🚉 Hold the next train departure until crowd on platform reduces",
            "🔄 Redirect all arriving passengers to alternate entrance",
        ],
    },
    "Temple / Religious Site": {
        "Warning": [
            "⏳ Switch to slow entry — allow maximum 50 people per minute through the main gate",
            "🚪 Open the alternate side gate or rear darshan entrance",
            "📢 PA: 'Devotees, please maintain the queue. Darshan is in progress. Please do not push.'",
            "🎫 Pause token distribution for the next 10 minutes",
        ],
        "Critical": [
            "🚫 Close the main gate — stop all new entries immediately",
            "🚨 Alert Temple Security Head, Police Liaison, and District Collector",
            "📢 PA: 'The temple premises have reached full capacity. Entry is temporarily closed for the safety of all devotees.'",
            "🔄 Stop all VIP darshan slots — convert to exit-only flow",
            "🏥 Move medical team to the main gate area now",
        ],
    },
    "Big Event / Rally / Concert": {
        "Warning": [
            "🚪 Open all entry gates at the same time to spread the crowd",
            "📢 PA: Guide the crowd toward sections C and D which still have space",
            "👮 Send the medical team to the area with the highest crowd density",
            "🎯 Limit entry to maximum 100 people per gate per minute",
        ],
        "Critical": [
            "🚫 Stop all new entries — switch to exit-only mode immediately",
            "📢 PA: 'The venue has reached its maximum safe capacity. Entry is now closed for everyone's safety.'",
            "🚨 Alert Event Director, Police Liaison, and Fire Marshal right now",
            "🆘 Open and secure all emergency exits immediately",
            "📋 Begin announcing section-by-section exit over PA",
        ],
    },
    "Market / Shopping Area": {
        "Warning": [
            "↔️ Enforce one-way pedestrian flow — all movement in one direction",
            "👮 Increase police patrol in the most crowded lane",
            "🚫 Block entry of two-wheelers and delivery vehicles",
        ],
        "Critical": [
            "🚫 Close the lane to new entries — deploy police barricades",
            "📢 PA: 'The market area is overcrowded. Please exit using the designated routes calmly.'",
            "🚨 Call Police Inspector and Fire Officer",
            "↔️ Allow movement in exit direction only",
        ],
    },
    "Stadium / Sports Venue": {
        "Warning": [
            "🆘 Walk to all emergency exits and physically check they are clear and unlocked",
            "📢 Prepare the section-wise exit announcement text",
            "👮 Place marshals at all corridor exits now",
        ],
        "Critical": [
            "🚫 Stop ticket scanning at all gates — no one else enters",
            "📢 PA: 'The venue is at full capacity. No further entry is permitted for safety reasons.'",
            "🚨 Alert Police SP, Fire Officer, and Event Organizer",
            "🆘 Open all emergency exits — position one marshal at each",
        ],
    },
    "Public Transport Hub": {
        "Warning": [
            "🚌 Call depot and request one extra bus or ferry to this stop immediately",
            "🪢 Set up rope barriers to create an orderly boarding queue",
            "📋 Update the display board: 'Extra vehicle arriving in approximately 8 minutes'",
        ],
        "Critical": [
            "🚫 Stop boarding the current vehicle — it is at full capacity",
            "📢 PA: 'Please move to the alternate stop. Extra vehicles are being sent urgently.'",
            "🚨 Alert Transport Authority and Local Police",
            "🚌 Request extra vehicles on this route for the rest of the day",
        ],
    },
    "Emergency / Disaster Site": {
        "Warning": [
            "🚨 Alert the rescue team and start counting and recording people in the building",
            "🔍 Check that all stairwells and exit routes are clear",
            "🏥 Brief fire and medical units on the current number of people",
        ],
        "Critical": [
            "🚫 Stop any new people from entering — create a clear rescue lane",
            "📋 Immediately share the current occupancy count with the Incident Commander",
            "🚨 Send Medical and Fire teams to the most crowded area now",
            "🔼 Start evacuating upper floors downward — alert fire officer",
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

STATUS_COLORS = {
    "Safe":     "#1e7e34",
    "Warning":  "#856404",
    "Critical": "#721c24",
}

STATUS_BG = {
    "Safe":     "#d4edda",
    "Warning":  "#fff3cd",
    "Critical": "#f8d7da",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _classify(count: int, capacity: int):
    r = count / capacity if capacity > 0 else 0.0
    if r >= 0.90: return "Critical", r
    if r >= 0.70: return "Warning",  r
    return "Safe", r


# ── Main render ───────────────────────────────────────────────────────────────

def render(crowd_count: int = 0, crowd_source: str = ""):
    st.markdown("## 🛡️ Monitor & Act")

    # ── Settings (collapsed by default so they don't clutter the view) ──
    with st.expander("⚙️ Venue settings — click to change location type or capacity",
                     expanded=(crowd_count == 0)):
        c1, c2 = st.columns([2, 1])
        with c1:
            location = st.selectbox(
                "What type of venue is this?",
                LOCATION_TYPES,
                key="mon_location",
            )
        with c2:
            capacity = st.number_input(
                "Maximum safe capacity",
                min_value=10,
                value=int(st.session_state.get("mon_capacity",
                           DEFAULT_CAPACITIES.get(
                               st.session_state.get("mon_location", LOCATION_TYPES[0]), 1000))),
                step=50,
                key="mon_capacity",
                help="The maximum number of people this venue can safely hold at one time",
            )
    location = st.session_state.get("mon_location", LOCATION_TYPES[0])
    capacity = int(st.session_state.get("mon_capacity",
                    DEFAULT_CAPACITIES.get(location, 1000)))

    # ── Count display ─────────────────────────────────────────────────
    st.divider()

    if crowd_count == 0:
        st.info(
            "👈 No crowd count yet. "
            "Go to the **Analyze Crowd** tab, upload a photo or video, "
            "and the count will appear here automatically."
        )
        count = st.number_input(
            "Or enter crowd count manually:",
            min_value=0, value=0, step=1, key="mon_manual_count",
        )
        crowd_count = count if count > 0 else 0

    if crowd_count == 0:
        return

    # Auto-add to trend
    snap_key = f"mon_snaps_{location}"
    if snap_key not in st.session_state:
        st.session_state[snap_key] = []
    st.session_state[snap_key].append({
        "Time": datetime.now().strftime("%H:%M:%S"),
        "Count": crowd_count,
    })
    st.session_state[snap_key] = st.session_state[snap_key][-120:]

    # ── Status banner — the most important thing ──────────────────────
    status, ratio = _classify(crowd_count, capacity)
    bg  = STATUS_BG[status]
    col = STATUS_COLORS[status]

    icons    = {"Safe": "✅", "Warning": "⚠️", "Critical": "🚨"}
    messages = {
        "Safe":     "All clear — crowd is within safe limits. Continue monitoring.",
        "Warning":  "Crowd is getting large. Start preparation steps below.",
        "Critical": "OVERCROWDED — Take action immediately using the checklist below.",
    }

    st.markdown(
        f"""
        <div style="
            background:{bg};
            border-left: 6px solid {col};
            border-radius: 8px;
            padding: 20px 24px;
            margin-bottom: 16px;
        ">
            <div style="font-size:28px;font-weight:700;color:{col};">
                {icons[status]}&nbsp; {status.upper()}
            </div>
            <div style="font-size:15px;color:{col};margin-top:4px;">
                {messages[status]}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Metrics row ───────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("👥 People at venue",  f"{crowd_count:,}")
    c2.metric("🏟️ Safe capacity",    f"{capacity:,}")
    c3.metric("📊 Venue is",          f"{ratio*100:.0f}% full")
    c4.metric("📍 Location type",     location.split("/")[0].strip())

    if crowd_source:
        st.caption(f"Count source: {crowd_source}")

    st.divider()

    # ── Action checklist (only shown when Warning or Critical) ────────
    if status in ("Warning", "Critical"):
        border_color = "#dc3545" if status == "Critical" else "#ffc107"
        st.markdown(
            f"""<div style="border-left:4px solid {border_color};
                            padding-left:16px;margin-bottom:8px;">
                <b style="font-size:18px;">{"🚨 Immediate Actions Required" if status == "Critical" else "⚠️ Preparation Steps"}</b><br>
                <span style="color:#666;font-size:13px;">
                Go through each item below in order.
                {"Do not delay — act now." if status == "Critical" else "Start before the situation gets worse."}
                </span>
            </div>""",
            unsafe_allow_html=True,
        )

        actions = ACTIONS.get(location, {}).get(status, [])
        for i, action in enumerate(actions, 1):
            checked = st.checkbox(action, key=f"action_{location}_{status}_{i}")

        st.divider()
        contacts = CONTACTS.get(location, "Security and local authorities")
        st.markdown(
            f"""<div style="background:#e8f4fd;border-radius:8px;padding:14px 18px;">
                📞 <b>Alert these people:</b><br>
                <span style="font-size:15px;">{contacts}</span>
            </div>""",
            unsafe_allow_html=True,
        )
        st.divider()

    # ── Crowd trend graph ─────────────────────────────────────────────
    snaps = st.session_state.get(snap_key, [])
    if len(snaps) > 1:
        st.markdown("#### 📈 Crowd trend this session")
        df = pd.DataFrame(snaps).set_index("Time")
        st.line_chart(df[["Count"]], height=200)

        # Capacity reference line annotation
        st.caption(
            f"Safe capacity: {capacity:,}  ·  "
            f"Warning threshold (70%): {int(capacity*0.70):,}  ·  "
            f"Critical threshold (90%): {int(capacity*0.90):,}"
        )
    else:
        st.markdown("#### 📈 Crowd trend")
        st.caption("The trend graph will appear once there are multiple readings.")
