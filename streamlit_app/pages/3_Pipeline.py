import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.platform_paths import FINAL_TUPLES
from processing.platform_paths import NORMALIZED_PRODUCTS
from processing.pipeline_artifacts import import_pipeline_artifacts
from streamlit_app.ui_common import enable_auto_refresh, python_cmd, read_json, render_live_panel, start_process, stop_process


st.title("Pipeline")
enable_auto_refresh()
st.info("Load exported pipeline artifacts to reuse CLIP/FAISS and canonical tuples, or run the existing matcher when new products need discovery.")

st.subheader("Unified SKU/EAN dataset")
st.caption("Builds the Columbia-first normalized dataset without CLIP, embeddings, vector search, or fuzzy matching.")
left, right = st.columns(2)
with left:
    if st.button("Build unified dataset", use_container_width=True):
        start_process("normalized", [python_cmd(), "-m", "processing.unified_products", "--output", str(NORMALIZED_PRODUCTS)])
with right:
    if st.button("Stop unified build", use_container_width=True):
        stop_process("normalized")
render_live_panel("normalized")

st.divider()

upload = st.file_uploader("Import pipeline artifacts (.zip)", type=["zip"])
if upload and st.button("Import artifacts"):
    try:
        result = import_pipeline_artifacts(upload.getvalue())
        st.success("Imported: " + ", ".join(result["imported"]))
    except Exception as exc:
        st.error(str(exc))

steps = [
    ("index", "Build the existing shared CLIP/FAISS index"),
    ("match", "Run the existing Amazon-to-target matching and write canonical tuples"),
    ("all", "Run the full pipeline in one pass"),
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

step = st.segmented_control("Run one step", options=["index", "match", "all"], default="index")
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
