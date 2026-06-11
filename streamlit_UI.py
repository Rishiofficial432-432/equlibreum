import streamlit as st
import requests
import io
import base64
from PIL import Image
import tempfile
import cv2
import pandas as pd
import threading
import queue
import time
import subprocess
import sys
import time

# Configure page
st.set_page_config(
    page_title="Crowd Counting App",
    page_icon="👥",
    layout="wide"
)

# API Configuration
API_BASE_URL = "http://localhost:8000"

@st.cache_resource
def start_backend():
    """
    Starts the FastAPI backend in a background process.
    This is necessary for hosting on Hugging Face Spaces.
    """
    # Check if backend is already running (simple check)
    try:
        requests.get(f"{API_BASE_URL}/health", timeout=1)
        print("✅ Backend already running")
        return
    except:
        print("⏳ Starting backend server...")
    
    # Start Uvicorn subprocess
    # We use sys.executable to ensure we use the same python environment
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"],
        stdout=subprocess.DEVNULL, # Hide logs to keep UI clean
        stderr=subprocess.DEVNULL
    )
    
    # Wait for server to start
    for i in range(30):
        try:
            requests.get(f"{API_BASE_URL}/health", timeout=1)
            print("✅ Backend started successfully!")
            break
        except:
            time.sleep(1)

def check_api_health():
    """Check if API is running"""
    try:
        response = requests.get(f"{API_BASE_URL}/health", timeout=5)
        return response.status_code == 200
    except:
        return False

def predict_crowd_count(image_file):
    """Send image to API for prediction"""
    try:
        files = {"file": image_file}
        response = requests.post(f"{API_BASE_URL}/predict", files=files, timeout=20)
        
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        return None

def api_worker(task_queue, result_queue):
    """
    Consumer Thread:
    Pulls frames from the queue and sends them to the API.
    """
    while True:
        item = task_queue.get()
        if item is None: # Sentinel value to stop the thread
            break
            
        timestamp, img_bytes_io = item
        
        try:
            # Send to API (This is the slow blocking part)
            files = {"file": ("frame.png", img_bytes_io, "image/png")}
            response = requests.post(f"{API_BASE_URL}/predict", files=files, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                count = data.get("estimated_count", 0)
                result_queue.put({"success": True, "time": timestamp, "count": count})
            else:
                result_queue.put({"success": False, "error": f"API {response.status_code}", "time": timestamp})
                
        except Exception as e:
            result_queue.put({"success": False, "error": str(e), "time": timestamp})
        finally:
            task_queue.task_done()

def main():
    st.title("👥 Crowd Counting Application")
    
    # Check API status
    col1, col2 = st.columns([3, 1])
    with col2:
        if check_api_health():
            st.success("🟢 API Connected")
        else:
            st.error("🔴 API Disconnected")
            return
    
    st.markdown("---")
    tab1, tab2 = st.tabs(["🖼️ Image Analysis", "🎥 Video Analysis"])

    # ----------------------- TAB 1: IMAGE ANALYSIS -----------------------
    with tab1:
        st.subheader("📤 Upload Image")
        uploaded_files = st.file_uploader(
            "Choose image file(s)", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True
        )

        if uploaded_files:
            for i, uploaded_file in enumerate(uploaded_files):
                c1, c2 = st.columns(2)
                with c1:
                    image = Image.open(uploaded_file)
                    st.image(image, use_container_width=True)

                    # Calculate Aspect Ratio
                    width, height = image.size
                    aspect_ratio = width / height
                  
                    # Show Name and Details just below the image
                    st.markdown(f"**📄 {uploaded_file.name}**")
                    st.caption(f"📏 {width}x{height} px | Aspect Ratio: {aspect_ratio:.2f}:1")
                with c2:
                    if st.button(f"🚀 Analyze Image {i+1}", key=f"btn_{i}", use_container_width=True, type="primary"):
                        with st.spinner("Analyzing..."):
                            uploaded_file.seek(0)
                            result = predict_crowd_count(uploaded_file)
                            if result and result.get("success"):
                                st.metric("Estimated Count", f"{result['estimated_count']:,.0f}")
                                
                                if result.get("density_map"):
                                    dmap_data = base64.b64decode(result["density_map"].split(",")[1])
                                    st.image(Image.open(io.BytesIO(dmap_data)), caption="Density Map", use_container_width=True)
                            else:
                                st.error("Analysis failed.")

    # ----------------------- TAB 2: VIDEO ANALYSIS -----------------------
    with tab2:
        st.subheader("Upload Video for Trend Analysis")
        video_file = st.file_uploader("Choose a video", type=['mp4', 'mov', 'avi'])
        
        interval = st.slider("Analysis Interval (seconds)", min_value=1, max_value=60, value=10)
        
        if video_file:
            # 1. Media Player
            st.markdown("### 📺 Preview")
            st.video(video_file)
            
            # Setup Temp File
            tfile = tempfile.NamedTemporaryFile(delete=False) 
            tfile.write(video_file.read())
            
            cap = cv2.VideoCapture(tfile.name)
            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration = frame_count / fps
            
            st.info(f"Video Duration: {duration:.2f}s | FPS: {fps:.2f} | Total Frames: {frame_count}")
            
            if st.button("🚀 Start Video Analysis"):
                frames_to_process = int(fps * interval)
                
                # 2. Initialize Queues and Thread
                task_queue = queue.Queue()
                result_queue = queue.Queue()
                results_list = []
                
                # Start the background worker
                worker = threading.Thread(target=api_worker, args=(task_queue, result_queue), daemon=True)
                worker.start()
                
                # 3. Layout Setup
                st.markdown("---")
                st.subheader("⚡ Live Analysis Dashboard")
                
                col_chart, col_preview = st.columns([2, 1])
                with col_chart:
                    chart_placeholder = st.empty()
                    status_placeholder = st.empty()
                with col_preview:
                    frame_placeholder = st.empty()
                
                progress_bar = st.progress(0)
                
                # 4. Producer Loop (Reads Video)
                current_frame = 0
                
                while cap.isOpened():
                    ret, frame = cap.read()
                    if not ret:
                        break
                        
                    # Check if this frame is on the interval
                    if current_frame % frames_to_process == 0:
                        # --- PRODUCER WORK ---
                        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        pil_img = Image.fromarray(frame_rgb)
                        
                        # Update "Live" Preview
                        frame_placeholder.image(
                            pil_img, 
                            caption=f"Captured Frame at {current_frame/fps:.1f}s", 
                            use_container_width=True
                        )
                        
                        # Prepare for Queue
                        img_byte_arr = io.BytesIO()
                        pil_img.save(img_byte_arr, format='PNG') 
                        img_byte_arr.seek(0)
                        
                        # Push to Queue
                        timestamp = current_frame / fps
                        task_queue.put((timestamp, img_byte_arr))
                    
                    # --- CONSUMER CHECK (Partial updates) ---
                    # Check if any results are ready while we are still reading video
                    while not result_queue.empty():
                        res = result_queue.get()
                        if res["success"]:
                            results_list.append({"Time (s)": round(res["time"], 1), "Count": int(res["count"])})
                            df = pd.DataFrame(results_list)
                            chart_placeholder.line_chart(df.set_index("Time (s)"))
                        else:
                            st.toast(f"Error at {res.get('time', 0):.1f}s: {res.get('error')}", icon="⚠️")

                    current_frame += 1
                    progress_bar.progress(min(current_frame / frame_count, 1.0))
                
                cap.release()
                
                # ---------------------------------------------------------
                # 5. FIXED CLEANUP LOGIC
                # ---------------------------------------------------------
                remaining = task_queue.qsize()
                if remaining > 0:
                    status_placeholder.info(f"Video reading done. Processing {remaining} remaining frames...")
                
                # Signal worker to stop
                task_queue.put(None)
                
                # Loop while the worker is still alive
                while worker.is_alive():
                    # Keep draining results so the UI updates LIVE
                    while not result_queue.empty():
                        res = result_queue.get()
                        if res["success"]:
                            results_list.append({"Time (s)": round(res["time"], 1), "Count": int(res["count"])})
                            df = pd.DataFrame(results_list)
                            chart_placeholder.line_chart(df.set_index("Time (s)"))
                        else:
                            st.toast(f"Error at {res.get('time', 0):.1f}s: {res.get('error')}", icon="⚠️")
                    
                    # Sleep briefly to give the worker CPU time
                    time.sleep(0.1)
                
                # Final check to ensure absolutely nothing was missed
                while not result_queue.empty():
                    res = result_queue.get()
                    if res["success"]:
                        results_list.append({"Time (s)": round(res["time"], 1), "Count": int(res["count"])})
                
                # Final Success Message
                if results_list:
                    df = pd.DataFrame(results_list)
                    chart_placeholder.line_chart(df.set_index("Time (s)"))
                    status_placeholder.success("✅ Video Analysis Complete!")
                else:
                    status_placeholder.warning("Analysis finished but no results were generated.")

with st.sidebar:
        st.header("ℹ️ About")
        st.markdown("""
        This application uses state-of-the-art deep learning models to count people in images.
        
        **Features:**
        - Real-time crowd counting
        - Density map visualization
        - Multiple model support
        - Easy-to-use interface
        
        **Supported Models:**
        - DM-Count (Default)  
        - CSRNet
        - SFANet
        - Bayesian Counting
        """)
        
        st.header("🔧 Usage Tips")
        st.markdown("""
        - Upload clear images with visible people.
        - Works best with crowd scenes.
        - Processing may take 10-30 seconds.
        - Larger images take longer to process.
        """)
        
        # st.header("📈 Model Performance")
        # st.markdown("""
        # **Typical Accuracy:**
        # - Dense crowds: ±60-70 people
        # - Sparse crowds: ±8-12 people
        # - Overall correlation: >0.85
        # """)

if __name__ == "__main__":
    main()