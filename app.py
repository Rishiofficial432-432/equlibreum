"""
app.py — Crowd Safety Platform
================================
Three tabs:

  🔬 Analyze      →  upload image or video, AI counts the crowd
  🛡️ Dashboard    →  status + actions + capacity map + routing — ALL IN ONE
  ⚖️ Equilibrium  →  satellite road extraction & graph criticality analysis

Count flows from Analyze into Dashboard automatically.
"""

from __future__ import annotations
import streamlit as st
import requests
import io
import base64
import tempfile
import threading
import queue
import time
import os

import cv2
import pandas as pd
from PIL import Image

import dashboard
import equilibrium

st.set_page_config(
    page_title="Crowd Safety Platform",
    page_icon="🛡️",
    layout="wide",
)

API_BASE_URL = "http://localhost:8000"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _api_ok():
    try:
        return requests.get(f"{API_BASE_URL}/health", timeout=4).status_code == 200
    except Exception:
        return False


def _predict_bytes(file_bytes, filename="upload.jpg"):
    try:
        r = requests.post(
            f"{API_BASE_URL}/predict",
            files={"file": (filename, file_bytes, "image/jpeg")},
            timeout=45,
        )
        if r.status_code == 200:
            return r.json()
        st.toast(f"Backend returned HTTP {r.status_code}", icon="⚠️")
    except requests.exceptions.ConnectionError:
        st.toast("Cannot reach AI engine. Is backend.py running?", icon="🔴")
    except Exception as e:
        st.toast(f"Upload error: {e}", icon="⚠️")
    return None


def _worker(task_q, result_q):
    while True:
        item = task_q.get()
        if item is None:
            break
        ts, img_bytes = item
        try:
            r = requests.post(
                f"{API_BASE_URL}/predict",
                files={"file": ("frame.png", img_bytes, "image/png")},
                timeout=45,
            )
            if r.status_code == 200:
                data = r.json()
                result_q.put({"ok": True, "time": ts,
                              "count": float(data.get("estimated_count", 0))})
            else:
                result_q.put({"ok": False, "time": ts, "error": f"HTTP {r.status_code}"})
        except Exception as e:
            result_q.put({"ok": False, "time": ts, "error": str(e)})
        finally:
            task_q.task_done()


def _push_count(count, source):
    st.session_state["crowd_count"]  = count
    st.session_state["crowd_source"] = source


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 🛡️ Crowd Safety")
    st.divider()

    api_ok = _api_ok()
    if api_ok:
        st.success("🟢  AI Engine: Online")
    else:
        st.error("🔴  AI Engine: Offline\n\nRun `python backend.py` to start it.")

    st.divider()

    crowd  = int(st.session_state.get("crowd_count", 0))
    source = st.session_state.get("crowd_source", "—")
    st.metric("Current crowd count", f"{crowd:,}",
              help="Auto-updated every time you run an analysis")
    if source != "—":
        st.caption(f"Source: {source}")

    st.divider()
    st.caption(
        "**Quick guide**\n\n"
        "1. **Analyze** tab → upload photo or video\n"
        "2. **Dashboard** tab → everything else in one place:\n"
        "   - Status + actions\n"
        "   - Capacity estimation\n"
        "   - Exit route planning\n"
        "   - Incident history\n"
        "3. **Equilibrium** tab → satellite road analysis"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────

tab_analyze, tab_dashboard, tab_equilibrium = st.tabs([
    "🔬  Analyze Crowd",
    "🛡️  Dashboard",
    "⚖️  Equilibrium",
])


# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 — ANALYZE
# ═════════════════════════════════════════════════════════════════════════════

with tab_analyze:
    st.markdown("## 🔬 Analyze Crowd")
    st.markdown(
        "Upload a **photo** or **video** of the crowd. "
        "The AI counts the people automatically and sends the result to the Dashboard."
    )
    st.divider()

    upload_mode = st.radio(
        "upload_mode",
        ["📷 Upload Photo", "🎥 Upload Video"],
        horizontal=True,
        label_visibility="collapsed",
    )

    # ── Photo ─────────────────────────────────────────────────────────
    if upload_mode == "📷 Upload Photo":

        uploaded = st.file_uploader(
            "Drag and drop a crowd photo, or click Browse",
            type=["jpg", "jpeg", "png"],
            key="photo_uploader",
        )

        if uploaded is not None:
            file_bytes = uploaded.getvalue()
            img = Image.open(io.BytesIO(file_bytes))
            w, h = img.size

            col_img, col_result = st.columns([1, 1], gap="large")

            with col_img:
                st.image(img, use_container_width=True)
                st.caption(f"📄 {uploaded.name}  ·  {w} × {h} px")

            with col_result:
                already_done = (
                    st.session_state.get("crowd_source") == uploaded.name
                    and st.session_state.get("crowd_count", 0) > 0
                )

                if already_done:
                    c = st.session_state["crowd_count"]
                    st.markdown(f"## 👥 {c:,} people")
                    st.success("✅ Already analyzed. Switch to **Dashboard** tab.")

                    if st.session_state.get("last_density_map"):
                        try:
                            raw = base64.b64decode(
                                st.session_state["last_density_map"].split(",")[1])
                            st.image(Image.open(io.BytesIO(raw)),
                                     caption="Heat map — brighter = more people",
                                     use_container_width=True)
                        except Exception:
                            pass

                    if st.button("🔄 Re-analyze", key="photo_reanalyze",
                                 use_container_width=True):
                        st.session_state.pop("crowd_count", None)
                        st.rerun()
                else:
                    st.markdown("### Ready to analyze")
                    st.markdown("Click below — AI will count all visible people.")

                    if st.button("🚀 Count People", type="primary",
                                 use_container_width=True, key="photo_analyze"):
                        if not api_ok:
                            st.error("AI Engine offline. Run `python backend.py`.")
                        else:
                            with st.spinner("AI is counting people…"):
                                result = _predict_bytes(file_bytes, uploaded.name)

                            if result and result.get("success"):
                                count = int(float(result["estimated_count"]))
                                _push_count(count, uploaded.name)

                                if result.get("density_map"):
                                    st.session_state["last_density_map"] = result["density_map"]

                                st.markdown(f"## 👥 {count:,} people")
                                st.success("✅ Done! Switch to **Dashboard** tab.")

                                if result.get("density_map"):
                                    try:
                                        raw = base64.b64decode(
                                            result["density_map"].split(",")[1])
                                        st.image(Image.open(io.BytesIO(raw)),
                                                 caption="Heat map",
                                                 use_container_width=True)
                                    except Exception:
                                        pass
                                st.rerun()
                            else:
                                st.error("Analysis failed. Check `python backend.py`.")

    # ── Video ─────────────────────────────────────────────────────────
    else:
        uploaded_vid = st.file_uploader(
            "Drag and drop a crowd video, or click Browse",
            type=["mp4", "mov", "avi"],
            key="video_uploader",
        )

        if uploaded_vid is not None:
            vid_bytes = uploaded_vid.getvalue()

            cache_key = f"vid_tmppath_{uploaded_vid.name}_{len(vid_bytes)}"
            if cache_key not in st.session_state:
                tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                tfile.write(vid_bytes)
                tfile.flush()
                tfile.close()
                st.session_state[cache_key] = tfile.name

            tmp_path = st.session_state[cache_key]

            cap = cv2.VideoCapture(tmp_path)
            fps         = cap.get(cv2.CAP_PROP_FPS) or 25
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration    = frame_count / fps if fps > 0 else 0
            cap.release()

            st.video(vid_bytes)
            st.info(f"📹 {duration:.0f}s · {frame_count:,} frames · {fps:.0f} fps")

            interval = st.slider(
                "Analyze one frame every ___ seconds",
                min_value=1, max_value=30, value=5, key="vid_interval",
            )

            col_btn, col_info = st.columns([1, 2])
            with col_btn:
                run_analysis = st.button(
                    "🚀 Start Analysis", type="primary",
                    use_container_width=True, key="vid_analyze",
                    disabled=not api_ok,
                )
            with col_info:
                if not api_ok:
                    st.warning("AI Engine offline. Start `python backend.py`.")
                else:
                    n = max(1, int(duration / interval))
                    st.caption(f"~{n} frames · est. {n*3}–{n*8}s")

            if run_analysis:
                frames_step   = max(1, int(fps * interval))
                task_q        = queue.Queue()
                result_q      = queue.Queue()
                results       = []

                wt = threading.Thread(target=_worker, args=(task_q, result_q), daemon=True)
                wt.start()

                prog     = st.progress(0.0, text="Starting…")
                chart_ph = st.empty()
                frame_ph = st.empty()

                cap     = cv2.VideoCapture(tmp_path)
                current = 0

                while cap.isOpened():
                    ret, frame = cap.read()
                    if not ret: break

                    if current % frames_step == 0:
                        ts  = current / fps
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        pil = Image.fromarray(rgb)
                        buf = io.BytesIO()
                        pil.save(buf, format="PNG")
                        frame_ph.image(pil, caption=f"Frame at {ts:.0f}s",
                                       use_container_width=True)
                        task_q.put((ts, buf.getvalue()))

                    while not result_q.empty():
                        res = result_q.get()
                        if res["ok"]:
                            results.append({"Time (s)": round(res["time"], 1),
                                            "Count": int(res["count"])})
                            chart_ph.line_chart(pd.DataFrame(results).set_index("Time (s)"))

                    prog.progress(min(current / max(frame_count, 1), 1.0),
                                  text=f"Analyzing… {current/max(frame_count,1)*100:.0f}%")
                    current += 1

                cap.release()
                task_q.put(None)

                while wt.is_alive():
                    while not result_q.empty():
                        res = result_q.get()
                        if res["ok"]:
                            results.append({"Time (s)": round(res["time"], 1),
                                            "Count": int(res["count"])})
                    time.sleep(0.15)

                while not result_q.empty():
                    res = result_q.get()
                    if res["ok"]:
                        results.append({"Time (s)": round(res["time"], 1),
                                        "Count": int(res["count"])})

                prog.progress(1.0, text="Done!")
                frame_ph.empty()

                if results:
                    df    = pd.DataFrame(results).set_index("Time (s)")
                    chart_ph.line_chart(df)
                    peak  = int(df["Count"].max())
                    avg   = int(df["Count"].mean())
                    final = int(df["Count"].iloc[-1])
                    _push_count(peak, f"{uploaded_vid.name} (peak)")

                    c1, c2, c3 = st.columns(3)
                    c1.metric("🔺 Peak",   f"{peak:,}")
                    c2.metric("📊 Average", f"{avg:,}")
                    c3.metric("⏱ Final",   f"{final:,}")
                    st.success(
                        f"✅ Done. Peak **{peak:,}** sent to **Dashboard** automatically.")
                else:
                    st.error("No results. Check AI engine is running.")


# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 — DASHBOARD (everything in one page)
# ═════════════════════════════════════════════════════════════════════════════

with tab_dashboard:
    dashboard.render(
        crowd_count=int(st.session_state.get("crowd_count", 0)),
        crowd_source=str(st.session_state.get("crowd_source", "")),
    )


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 — EQUILIBRIUM
# ═════════════════════════════════════════════════════════════════════════════

with tab_equilibrium:
    equilibrium.render()


if __name__ == "__main__":
    pass
