import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.pipeline_artifacts import import_pipeline_artifacts
from processing.platform_paths import NORMALIZED_PRODUCTS
from streamlit_app.ui_common import apply_theme, enable_auto_refresh, python_cmd, read_json, render_live_panel, start_process, stop_process

apply_theme()


st.title("Pipeline")
enable_auto_refresh()
st.info(
    "This page combines the entire pipeline into a single process.  \n"
    "Just click **Run Pipeline** and pray your laptop doesn't explode. 😄  \n"
    "The process can take anywhere from **10 minutes to 2–3 hours**, depending on how powerful your laptop is. Good luck!"
)
st.caption(
    "You can monitor the progress in the live logs. The pipeline consists of only three steps.  \n"
    "A **'Pipeline Completed'** message will appear once the process finishes, but you'll need to refresh the page to see it."
)

left, right = st.columns(2)
with left:
    if st.button("Run full pipeline", type="primary", use_container_width=True):
        start_process("matcher", [python_cmd(), "-m", "processing.indexing_pipeline", "--step", "all"])
with right:
    if st.button("Stop pipeline", use_container_width=True):
        stop_process("matcher")

st.subheader("Restore exported CLIP embeddings")
st.caption("Upload the `pipeline_artifacts.zip` created on the Export page. This restores the CLIP index, metadata, cache, and tuples without running CLIP again.")
artifact_upload = st.file_uploader("Pipeline artifact zip", type=["zip"], key="pipeline_artifact_upload")
if artifact_upload is not None and st.button("Restore pipeline artifacts", use_container_width=True):
    try:
        result = import_pipeline_artifacts(artifact_upload.getvalue())
        st.success("Restored pipeline artifacts without rebuilding CLIP embeddings: " + ", ".join(result["imported"]))
        if result["missing_optional"]:
            st.caption("Optional artifacts not included: " + ", ".join(result["missing_optional"]))
    except Exception as exc:
        st.error(f"Could not restore pipeline artifacts: {exc}")

payload = read_json(NORMALIZED_PRODUCTS, {"summary": {}})
summary = payload.get("summary", {})
cols = st.columns(4)
cols[0].metric("Normalized SKUs", summary.get("normalized_products", 0))
cols[1].metric("Columbia CLIP queries", summary.get("columbia_clip_queries", 0))
cols[2].metric("Myntra / TataCliq links", f"{summary.get('myntra_clip_linked', 0)} / {summary.get('tatacliq_clip_linked', 0)}")
cols[3].metric("Last build", payload.get("created_at", "-"))

render_live_panel("matcher")
