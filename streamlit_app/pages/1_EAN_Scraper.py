import streamlit as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.platform_paths import JSON_DIR
from streamlit_app.ui_common import managed_scraper_command, render_operational_console, start_process, stop_process

st.title("EAN Scraper")
st.info("Runs Amazon Scraper V2 only. Completed scrapes are saved as dated JSON and update known product prices without rerunning matching.")
headless = st.toggle("Run in Headless Mode", value=True)
left, right = st.columns(2)
with left:
    if st.button("Start scraper", use_container_width=True):
        start_process("amazon", managed_scraper_command("amazon", headless))
with right:
    if st.button("Stop scraper", use_container_width=True):
        stop_process("amazon")
render_operational_console("amazon", JSON_DIR / "amazon")
