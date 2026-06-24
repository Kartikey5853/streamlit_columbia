import streamlit as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streamlit_app.ui_common import controls, enable_auto_refresh, render_live_panel

st.title("Amazon")
enable_auto_refresh()
headless = st.toggle("Headless mode", value=False)
controls("amazon", headless)
render_live_panel("amazon")
