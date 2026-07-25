import streamlit as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.platform_paths import JSON_DIR
from streamlit_app.ui_common import (
    amazon_v3_refresh_command,
    apply_theme,
    managed_scraper_command,
    render_operational_console,
    start_process,
    stop_process,
)

apply_theme()

st.title("EAN Scraper")
st.info(
    "Runs Amazon scraper jobs and refreshes prices for existing Amazon JSON records.  \n"
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

st.divider()
st.subheader("Refresh Price")
st.caption("Uses Amazon Scraper V3 to open only the saved Amazon product links and refresh prices for EANs already present in the Amazon JSON.")
tabs = st.number_input("Tabs running at once", min_value=1, max_value=20, value=5, step=1)
refresh_left, refresh_right = st.columns(2)
with refresh_left:
    if st.button("Refresh price", use_container_width=True):
        start_process("amazon_v3_refresh", amazon_v3_refresh_command(headless, int(tabs)))
with refresh_right:
    if st.button("Stop price refresh", use_container_width=True):
        stop_process("amazon_v3_refresh")
render_operational_console("amazon_v3_refresh", JSON_DIR / "amazon")
