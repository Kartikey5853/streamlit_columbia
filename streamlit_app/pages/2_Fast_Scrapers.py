import streamlit as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.platform_paths import JSON_DIR
from streamlit_app.ui_common import apply_theme, fast_scraper_command, render_operational_console, start_process, stop_process

apply_theme()
from data_scraper.ajio_scraper_wrapper import is_cdp_available, start_chrome

st.title("Fast Scrapers")
st.info("Runs AJIO, Myntra, TataCliQ, Columbia, and Adventuras together. A failure is logged and does not stop the remaining sources.")
headless = st.toggle("Run in Headless Mode", value=False)
left, middle, right = st.columns(3)
with left:
    if st.button("Start fast scrapers", use_container_width=True):
        start_process("fast_scrapers", fast_scraper_command(headless))
with middle:
    if st.button("Stop fast scrapers", use_container_width=True):
        stop_process("fast_scrapers")
with right:
    if st.button("Open Scraper Chrome", use_container_width=True):
        try:
            process = start_chrome(headless=False)
            if process is None and is_cdp_available():
                st.success("Scraper Chrome is already open; reusing its dedicated debugging profile.")
            else:
                st.success("Opened the dedicated scraper Chrome profile. No scraper was started.")
        except Exception as exc:
            st.error(f"Could not open scraper Chrome: {exc}")
render_operational_console("fast_scrapers", JSON_DIR)
