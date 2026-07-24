import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from processing.platform_paths import FINAL_TUPLES, PROCESS_STATUS
from streamlit_app.ui_common import apply_theme, enable_auto_refresh, read_json

apply_theme()


st.set_page_config(page_title="Columbia Price Intelligence", layout="wide")
enable_auto_refresh(4)

st.info(
    "This page shows which scraper is currently running.  \n"
    "Go to **EAN Scraper** to scrape Amazon.  \n"
    "Go to **Fast Scraper** to scrape all other supported websites."
)


status = read_json(PROCESS_STATUS, {})
tuples = read_json(FINAL_TUPLES, {"summary": {}})

running = [site for site, record in status.items() if record.get("running")]
cols = st.columns(3)
cols[0].metric("Running jobs", len(running))
cols[1].metric("Tuple count", tuples.get("summary", {}).get("tuples", 0))
cols[2].metric("Accepted matches", tuples.get("summary", {}).get("accepted_cross_market_matches", 0))

if running:
    st.success("Active: " + ", ".join(running))
else:
    st.info("No active scraper jobs.")
