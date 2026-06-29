import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.platform_paths import FINAL_TUPLES
from streamlit_app.ui_common import enable_auto_refresh, python_cmd, read_json, render_live_panel, start_process, stop_process


st.title("Indexing Pipeline")
enable_auto_refresh()

steps = [
    ("1", "Load Amazon + marketplace JSON and generate master_products.pkl"),
    ("2", "Load Myntra, generate CLIP embeddings, build FAISS indexes"),
    ("3", "Load Tata CLiQ, generate CLIP embeddings, build FAISS indexes"),
    ("4", "Run Amazon to Myntra/Tata matching with title, CLIP, and price"),
    ("5", "Generate final tuples and store products.pkl, clip.index, metadata.pkl, final_tuples.json"),
    ("6", "Display progress, logs, match counts, rejected counts, and tuple counts"),
]

for number, label in steps:
    st.write(f"Step {number}: {label}")

col1, col2 = st.columns(2)
with col1:
    if st.button("Run full indexing pipeline", use_container_width=True):
        start_process("matcher", [python_cmd(), "-m", "processing.indexing_pipeline", "--step", "all"])
with col2:
    if st.button("Stop pipeline", use_container_width=True):
        stop_process("matcher")

step = st.segmented_control("Run one step", options=["1", "2", "3", "4", "5"], default="1")
if st.button("Run selected step"):
    start_process("matcher", [python_cmd(), "-m", "processing.indexing_pipeline", "--step", step])

payload = read_json(FINAL_TUPLES, {"summary": {}})
summary = payload.get("summary", {})
cols = st.columns(4)
cols[0].metric("Tuple count", summary.get("tuples", 0))
cols[1].metric("Accepted matches", summary.get("accepted_cross_market_matches", 0))
cols[2].metric("Rejected", max(0, (summary.get("tuples", 0) * 2) - summary.get("accepted_cross_market_matches", 0)))
cols[3].metric("Last build", payload.get("created_at", "-"))

render_live_panel("matcher")
