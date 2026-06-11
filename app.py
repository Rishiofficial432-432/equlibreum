"""
app.py — Crowd Safety Platform
================================
Three tabs, one flow:

  🔬 Analyze   →  upload image or video, AI counts the crowd automatically
  🛡️ Monitor   →  status dashboard, auto-filled count, action checklist
  🗺️ Disperse  →  draw area on map, get capacity, plan exit routes

The crowd count flows automatically from Analyze into Monitor and Disperse.
The operator never types a number twice.
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

import monitor
import disperse

st.set_page_config(
    page_title="Crowd Safety Platform",
    page_icon="🛡️",
    layout="wide",
)

API_BASE_URL = "http://localhost:8000"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _api_ok() -> bool:
    try:
        return requests.get(f"{API_BASE_URL}/health", timeout=4).status_code == 200
    except Exception:
        return False


def _predict_bytes(file_bytes: bytes, filename: str = "upload.jpg") -> dict | None:
    """Send raw bytes to backend. Always works regardless of file pointer state."""
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


def _worker(task_q: queue.Queue, result_q: queue.Queue):
    """Background thread: send frames to backend API."""
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


def _push_count(count: int, source: str):
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
        "2. Count appears automatically in all tabs\n"
        "3. **Monitor** tab → situation status + actions\n"
        "4. **Disperse** tab → draw venue + plan exit routes"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────

tab_analyze, tab_monitor, tab_disperse = st.tabs([
    "🔬  Analyze Crowd",
    "🛡️  Monitor & Act",
    "🗺️  Plan Exit Routes",
])


# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 — ANALYZE
# ═════════════════════════════════════════════════════════════════════════════

with tab_analyze:
    st.markdown("## 🔬 Analyze Crowd")
    st.markdown(
        "Upload a **photo** or **video** of the crowd. "
        "The AI counts the people automatically and sends the result to all other tabs."
    )
    st.divider()

    upload_mode = st.radio(
        "upload_mode",
        ["📷 Upload Photo", "🎥 Upload Video"],
        horizontal=True,
        label_visibility="collapsed",
    )

    # ═══ PHOTO MODE ═══════════════════════════════════════════════════
    if upload_mode == "📷 Upload Photo":

        uploaded = st.file_uploader(
            "Drag and drop a crowd photo, or click Browse",
            type=["jpg", "jpeg", "png"],
            key="photo_uploader",
        )

        if uploaded is not None:
            # Read bytes immediately — before any rerun can invalidate the buffer
            file_bytes = uploaded.getvalue()
            img = Image.open(io.BytesIO(file_bytes))
            w, h = img.size

            col_img, col_result = st.columns([1, 1], gap="large")

            with col_img:
                st.image(img, use_container_width=True)
                st.caption(f"📄 {uploaded.name}  ·  {w} × {h} px")

            with col_result:
                already_done = (st.session_state.get("crowd_source") == uploaded.name
                                and st.session_state.get("crowd_count", 0) > 0)

                if already_done:
                    c = st.session_state["crowd_count"]
                    st.markdown(f"## 👥 {c:,} people")
                    st.success("✅ Already analyzed. Switch to **Monitor & Act** tab.")

                    # Show density map if cached
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
                    st.markdown(
                        "Click below. The AI will count all visible people in the photo."
                    )

                    if st.button("🚀 Count People", type="primary",
                                 use_container_width=True, key="photo_analyze"):
                        if not api_ok:
                            st.error(
                                "AI Engine is offline. "
                                "Start it with `python backend.py` then try again."
                            )
                        else:
                            with st.spinner("AI is counting people…"):
                                result = _predict_bytes(file_bytes, uploaded.name)

                            if result and result.get("success"):
                                count = int(float(result["estimated_count"]))
                                _push_count(count, uploaded.name)

                                # Cache density map so it survives reruns
                                if result.get("density_map"):
                                    st.session_state["last_density_map"] = result["density_map"]

                                st.markdown(f"## 👥 {count:,} people")
                                st.success(
                                    "✅ Done! Count sent to **Monitor & Act** automatically."
                                )

                                if result.get("density_map"):
                                    try:
                                        raw = base64.b64decode(
                                            result["density_map"].split(",")[1])
                                        st.image(
                                            Image.open(io.BytesIO(raw)),
                                            caption="Heat map — brighter areas = more people",
                                            use_container_width=True,
                                        )
                                    except Exception:
                                        pass

                                st.rerun()
                            else:
                                st.error(
                                    "Analysis failed. "
                                    "Check that `python backend.py` is running, "
                                    "then try again."
                                )

    # ═══ VIDEO MODE ═══════════════════════════════════════════════════
    else:
        uploaded_vid = st.file_uploader(
            "Drag and drop a crowd video, or click Browse",
            type=["mp4", "mov", "avi"],
            key="video_uploader",
        )

        if uploaded_vid is not None:
            # Read all bytes immediately
            vid_bytes = uploaded_vid.getvalue()

            # Write to a persistent temp file (keyed to file name + size so
            # it's not re-written on every rerun for the same upload)
            cache_key = f"vid_tmppath_{uploaded_vid.name}_{len(vid_bytes)}"
            if cache_key not in st.session_state:
                tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                tfile.write(vid_bytes)
                tfile.flush()
                tfile.close()
                st.session_state[cache_key] = tfile.name

            tmp_path = st.session_state[cache_key]

            # Read video metadata
            cap = cv2.VideoCapture(tmp_path)
            fps         = cap.get(cv2.CAP_PROP_FPS) or 25
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration    = frame_count / fps if fps > 0 else 0
            cap.release()

            st.video(vid_bytes)
            st.info(f"📹 Duration: **{duration:.0f} seconds**  ·  {frame_count:,} frames at {fps:.0f} fps")

            # Settings always rendered (not inside expander) so interval is always defined
            interval = st.slider(
                "Analyze one frame every ___ seconds",
                min_value=1, max_value=30, value=5,
                help="Lower = more readings, slower. Higher = faster, fewer data points.",
                key="vid_interval",
            )

            col_btn, col_info = st.columns([1, 2])
            with col_btn:
                run_analysis = st.button(
                    "🚀 Start Analysis",
                    type="primary",
                    use_container_width=True,
                    key="vid_analyze",
                    disabled=not api_ok,
                )
            with col_info:
                if not api_ok:
                    st.warning("AI Engine is offline. Start `python backend.py` first.")
                else:
                    total_frames_to_analyze = max(1, int(duration / interval))
                    st.caption(
                        f"Will analyze **~{total_frames_to_analyze} frames** "
                        f"(one every {interval}s). "
                        f"Estimated time: {total_frames_to_analyze * 3}–{total_frames_to_analyze * 8}s"
                    )

            if run_analysis:
                frames_step = max(1, int(fps * interval))
                task_q      = queue.Queue()
                result_q    = queue.Queue()
                results: list = []

                worker_thread = threading.Thread(
                    target=_worker, args=(task_q, result_q), daemon=True)
                worker_thread.start()

                st.markdown("---")
                st.markdown("### 📊 Analysis in progress")
                prog     = st.progress(0.0, text="Starting…")
                chart_ph = st.empty()
                frame_ph = st.empty()

                cap = cv2.VideoCapture(tmp_path)
                current = 0

                while cap.isOpened():
                    ret, frame = cap.read()
                    if not ret:
                        break

                    if current % frames_step == 0:
                        ts  = current / fps
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        pil = Image.fromarray(rgb)
                        buf = io.BytesIO()
                        pil.save(buf, format="PNG")
                        frame_ph.image(
                            pil,
                            caption=f"Frame at {ts:.0f}s",
                            use_container_width=True,
                        )
                        task_q.put((ts, buf.getvalue()))

                    # Drain completed results
                    while not result_q.empty():
                        res = result_q.get()
                        if res["ok"]:
                            results.append({
                                "Time (s)": round(res["time"], 1),
                                "Count":    int(res["count"]),
                            })
                            chart_ph.line_chart(
                                pd.DataFrame(results).set_index("Time (s)"))

                    pct = min(current / max(frame_count, 1), 1.0)
                    prog.progress(pct, text=f"Analyzing… {pct*100:.0f}%")
                    current += 1

                cap.release()
                task_q.put(None)  # signal worker to stop

                # Wait for remaining API responses
                while worker_thread.is_alive():
                    while not result_q.empty():
                        res = result_q.get()
                        if res["ok"]:
                            results.append({
                                "Time (s)": round(res["time"], 1),
                                "Count":    int(res["count"]),
                            })
                    time.sleep(0.15)

                # Final drain
                while not result_q.empty():
                    res = result_q.get()
                    if res["ok"]:
                        results.append({
                            "Time (s)": round(res["time"], 1),
                            "Count":    int(res["count"]),
                        })

                prog.progress(1.0, text="Done!")
                frame_ph.empty()

                if results:
                    df    = pd.DataFrame(results).set_index("Time (s)")
                    chart_ph.line_chart(df)

                    peak  = int(df["Count"].max())
                    avg   = int(df["Count"].mean())
                    final = int(df["Count"].iloc[-1])

                    _push_count(peak, f"{uploaded_vid.name} (peak)")

                    st.markdown("---")
                    c1, c2, c3 = st.columns(3)
                    c1.metric("🔺 Peak count",    f"{peak:,}",
                              help="Highest count in the video — used for safety planning")
                    c2.metric("📊 Average count", f"{avg:,}")
                    c3.metric("⏱ Final count",    f"{final:,}",
                              help="Count at the last analyzed frame")

                    st.success(
                        f"✅ Analysis complete. "
                        f"Peak count **{peak:,}** sent to **Monitor & Act** automatically. "
                        "Switch to that tab now."
                    )
                else:
                    st.error(
                        "No results returned. "
                        "Check that the AI engine is running and try again."
                    )


# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 — MONITOR & ACT
# ═════════════════════════════════════════════════════════════════════════════

with tab_monitor:
    monitor.render(
        crowd_count=int(st.session_state.get("crowd_count", 0)),
        crowd_source=str(st.session_state.get("crowd_source", "")),
    )


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 — PLAN EXIT ROUTES
# ═════════════════════════════════════════════════════════════════════════════

with tab_disperse:
    disperse.render(
        crowd_count=int(st.session_state.get("crowd_count", 0)),
    )


if __name__ == "__main__":
    pass
