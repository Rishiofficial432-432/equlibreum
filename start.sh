#!/usr/bin/env bash
# start.sh — Launch Equilibrium: Route Resilience
# ================================================
# ISRO NNRMS Problem Statement
# Occlusion-Robust Road Extraction & Graph-Theoretic Criticality Analysis

set -e

VENV=".venv"
PORT=8501

echo ""
echo "⚖️  Equilibrium — Route Resilience"
echo "════════════════════════════════════"
echo "  ISRO NNRMS | Road Extraction & Criticality Analysis"
echo ""

# ── Activate virtual environment ───────────────────────────────────────────────
if [ -f "$VENV/bin/activate" ]; then
    source "$VENV/bin/activate"
    echo "✅ Virtual environment activated"
else
    echo "⚠️  .venv not found — using system Python"
fi

# ── Dependency check ────────────────────────────────────────────────────────────
echo "🔍 Checking dependencies…"
python -c "import streamlit, equilibrium, segmentation, healing, criticality; print('✅ All modules OK')"

# ── Launch ─────────────────────────────────────────────────────────────────────
echo ""
echo "🚀 Starting Streamlit on http://localhost:$PORT"
echo "   Press Ctrl+C to stop."
echo ""

streamlit run app.py \
    --server.port $PORT \
    --server.headless false \
    --browser.gatherUsageStats false \
    --theme.base dark \
    --theme.primaryColor "#2563eb" \
    --theme.backgroundColor "#0a0f1e" \
    --theme.secondaryBackgroundColor "#111827" \
    --theme.textColor "#e2e8f0"
