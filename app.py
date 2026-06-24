"""
app.py — Equilibrium: Route Resilience
========================================
ISRO NNRMS Problem Statement:
  Occlusion-Robust Road Extraction & Graph-Theoretic
  Criticality Analysis for Urban Mobility

Entry point for the Streamlit application.
All logic lives in the supporting modules:

  segmentation.py  — Phase I : Attention U-Net + Classical CV road extraction
  healing.py       — Phase II : MST + Disjoint Set topological healing
  criticality.py   — Phase III: Betweenness, Resilience Index, ablation
  equilibrium.py   — Phase IV : Interactive Streamlit dashboard

Run with:
    streamlit run app.py
"""

import streamlit as st
import equilibrium

st.set_page_config(
    page_title="Equilibrium — Route Resilience | ISRO NNRMS",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={
        "About": (
            "**Equilibrium** — Route Resilience Pipeline\n\n"
            "Occlusion-Robust Road Extraction & Graph-Theoretic "
            "Criticality Analysis for Urban Mobility.\n\n"
            "Built for ISRO NNRMS Problem Statement."
        )
    },
)

# ── Global CSS ────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }

    /* Dark background */
    .stApp {
        background: #0a0f1e;
        color: #e2e8f0;
    }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background: #0d1526;
        border-right: 1px solid #1e293b;
    }

    /* Metric cards */
    [data-testid="metric-container"] {
        background: #111827;
        border: 1px solid #1e293b;
        border-radius: 10px;
        padding: 12px 16px;
    }

    /* Dataframe */
    .stDataFrame {
        border: 1px solid #1e293b;
        border-radius: 8px;
    }

    /* Buttons */
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #2563eb, #7c3aed);
        border: none;
        border-radius: 8px;
        font-weight: 600;
        letter-spacing: 0.5px;
        transition: opacity 0.2s;
    }
    .stButton > button[kind="primary"]:hover {
        opacity: 0.88;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background: #0d1526;
        border-radius: 10px;
        padding: 4px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px;
        padding: 6px 16px;
        font-weight: 500;
        color: #94a3b8;
    }
    .stTabs [aria-selected="true"] {
        background: #1e3a5f !important;
        color: #60a5fa !important;
    }

    /* Expander */
    .streamlit-expanderHeader {
        background: #111827;
        border-radius: 8px;
        border: 1px solid #1e293b;
    }

    /* Divider */
    hr {
        border-color: #1e293b;
    }

    /* Success/Error/Warning */
    .stAlert {
        border-radius: 10px;
    }

    /* Progress bar */
    .stProgress > div > div {
        background: linear-gradient(90deg, #2563eb, #7c3aed);
        border-radius: 4px;
    }

    /* File uploader */
    [data-testid="stFileUploader"] {
        background: #111827;
        border: 1px dashed #334155;
        border-radius: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Render the main Equilibrium dashboard ─────────────────────────────────────
equilibrium.render()
