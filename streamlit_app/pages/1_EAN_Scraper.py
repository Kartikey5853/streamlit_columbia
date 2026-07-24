import streamlit as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.platform_paths import JSON_DIR
from streamlit_app.ui_common import apply_theme, managed_scraper_command, render_operational_console, start_process, stop_process

apply_theme()

st.title("EAN Scraper")
st.info(
    "Runs Amazon Scraper V2 only.  \n"
    "Turn Headless mode off to see the actual scraping process.  \n"
    "Refresh the page to view live logs and track the scraper's progress."
)
headless = st.toggle("Run in Headless Mode", value=True)
left, right = st.columns(2)
with left:
    if st.button("Start scraper", use_container_width=True):
        start_process("amazon", managed_scraper_command("amazon", headless))
with right:
    if st.button("Stop scraper", use_container_width=True):
        stop_process("amazon")
render_operational_console("amazon", JSON_DIR / "amazon")
