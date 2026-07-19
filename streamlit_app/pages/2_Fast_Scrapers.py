import streamlit as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.platform_paths import JSON_DIR
from streamlit_app.ui_common import fast_scraper_command, render_operational_console, start_process, stop_process

st.title("Fast Scrapers")
st.info("Runs AJIO, Myntra, TataCliQ, Columbia, and Adventuras together. A failure is logged and does not stop the remaining sources.")
headless = st.toggle("Run in Headless Mode", value=True)
left, right = st.columns(2)
with left:
    if st.button("Start fast scrapers", use_container_width=True):
        start_process("fast_scrapers", fast_scraper_command(headless))
with right:
    if st.button("Stop fast scrapers", use_container_width=True):
        stop_process("fast_scrapers")
render_operational_console("fast_scrapers", JSON_DIR)
