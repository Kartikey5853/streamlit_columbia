import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.platform_paths import NORMALIZED_PRODUCTS
from streamlit_app.ui_common import enable_auto_refresh, python_cmd, read_json, render_live_panel, start_process, stop_process


st.title("Pipeline")
enable_auto_refresh()
st.info("One pipeline: exact SKU/EAN matching for Columbia, Amazon, AJIO, and Adventuras, followed by Columbia-to-Myntra/TataCliq CLIP matching.")
st.caption("1. Normalize SKUs and aggregate Columbia EANs. 2. Attach Amazon by EAN and AJIO/Adventuras by SKU. 3. Index Columbia, Myntra, and TataCliq images. 4. For every normalized Columbia SKU, keep the closest Myntra and TataCliq result from its top 100 CLIP candidates.")

left, right = st.columns(2)
with left:
    if st.button("Run full pipeline", type="primary", use_container_width=True):
        start_process("matcher", [python_cmd(), "-m", "processing.indexing_pipeline", "--step", "all"])
with right:
    if st.button("Stop pipeline", use_container_width=True):
        stop_process("matcher")

payload = read_json(NORMALIZED_PRODUCTS, {"summary": {}})
summary = payload.get("summary", {})
cols = st.columns(4)
cols[0].metric("Normalized SKUs", summary.get("normalized_products", 0))
cols[1].metric("Columbia CLIP queries", summary.get("columbia_clip_queries", 0))
cols[2].metric("Myntra / TataCliq links", f"{summary.get('myntra_clip_linked', 0)} / {summary.get('tatacliq_clip_linked', 0)}")
cols[3].metric("Last build", payload.get("created_at", "-"))

render_live_panel("matcher")
