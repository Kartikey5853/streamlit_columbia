import streamlit as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.config import load_config
from streamlit_app.ui_common import controls, enable_auto_refresh, render_live_panel

st.title("Ajio / Columbia / Adventuras")
enable_auto_refresh()
config = load_config()
headless = st.toggle("Headless mode", value=False)

for site in ["ajio", "columbia", "adventuras"]:
    st.header(site.title())
    st.metric("Cooldown seconds", config.get(f"{site}_cooldown", 0))
    controls(site, headless)
    render_live_panel(site)
