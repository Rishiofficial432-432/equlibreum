# ⚖️ Equilibrium: Route Resilience Pipeline
### Occlusion-Robust Road Extraction & Graph-Theoretic Criticality Analysis for Urban Mobility
*ISRO National Natural Resources Management System (NNRMS) Problem Statement*

---

## 📌 Overview
Standard satellite-based road extraction often fails due to "spectral blindness" caused by tree canopies, building shadows, and cloud cover. These broken masks lack topological connectivity, making them unsuitable for disaster response, navigation, or traffic simulation. 

**Equilibrium** solves this by implementing a resilient 4-phase pipeline that extracts roads from satellite imagery, topologically heals fractures/occlusions, analyzes network bottlenecks, and simulates route collapses to measure urban mobility resilience.

---

## 🛠️ System Architecture & Features

The pipeline is structured into four distinct phases:

### 🔍 Phase I: Road Extraction & Segmentation (`segmentation.py`)
- **Neural Network:** PyTorch implementation of **Attention U-Net** (`AttentionUNet`) with Channel & Spatial Attention Gates to focus on narrow road features.
- **Fallback Engine:** Classical Computer Vision fallback using **CLAHE (Contrast Limited Adaptive Histogram Equalization)**, adaptive thresholding, and morphological structuring (thinning/skeletonization) to ensure robust zero-training deployment.

### 🩹 Phase II: Topological Healing & Graph Extraction (`healing.py`)
- **Skeleton to Graph:** Converts pixel skeletons to NetworkX spatial graphs using coordinate-mapping junctions and endpoints.
- **Topological Healing:** Uses a combination of Minimum Spanning Trees (MST) and Disjoint Set/Union-Find. Stubborn disconnected fragments are bridged based on geographic distance, angle alignment tolerances, and path continuation vectors.

### 📊 Phase III: Graph-Theoretic Criticality Analysis (`criticality.py`)
- **Bottleneck Detection:** Calculates Edge/Node Betweenness Centralities to identify critical "gatekeeper" nodes and bottleneck road segments.
- **Resilience Index ($R$):** Quantifies network connectivity using a normalized ratio of the giant component size to the total nodes under simulated attacks.
- **Stress-Testing:** Runs an ablation series (sequential deletion of highest-centrality edges) to plot network degradation curves.

### 🗺️ Phase IV: Interactive Geospatial Dashboard (`equilibrium.py` / `app.py`)
- **Folium Interactive Leaflet Map:** Displays the extracted road graph over an interactive map.
- **Centrality Heatmap:** A visual plasma-gradient heatmap representing traffic load and bottlenecks.
- **Dynamic Simulation:** Interactive UI allowing users to click on any node/edge, trigger a simulated collapse, and immediately visualize rerouted traffic, updated bottleneck shifts, and the resulting Resilience Index gauge.

---

## 🚀 Setup & Installation

### Requirements
- macOS (tested on macOS)
- Python 3.9+
- Virtual environment (`.venv`)

### Installation Steps
1. **Clone/Navigate to the directory**:
   ```bash
   cd "/Users/apple/Documents/vs code/equlibrium"
   ```

2. **Initialize and Activate Virtual Environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install Dependencies**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Launch the Dashboard**:
   ```bash
   chmod +x start.sh
   ./start.sh
   ```
   Or launch directly:
   ```bash
   streamlit run app.py
   ```
   Open your browser at `http://localhost:8501`.

---

## 📂 Project Structure

```
equlibrium/
├── app.py              # Main Streamlit application launcher & style injector
├── equilibrium.py      # Phase IV Dashboard rendering and UI component orchestrator
├── segmentation.py     # Phase I Road Extraction (Attention U-Net / Classical CV)
├── healing.py          # Phase II Graph extraction & Topological Healing (MST/Union-Find)
├── criticality.py      # Phase III Graph-Theoretic metrics, Centrality, and Simulation
├── requirements.txt    # Clean, lightweight dependency list (free of crowd counting)
├── start.sh            # One-click startup script for Streamlit
└── sample_images/      # Folder containing demo/test satellite images
```

---

## 🧠 Training the Deep Learning Model

To use the **Attention U-Net** instead of the Classical CV extractor:
1. Obtain a satellite dataset like **DeepGlobe Road Extraction** or **SpaceNet Roads**.
2. Run the Attention U-Net training loop (defined in `segmentation.py` or write a custom `train.py` using `AttentionUNet`).
3. Load the trained PyTorch state dict (weights file) directly in the settings panel of the Streamlit UI.
