import streamlit as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.platform_paths import FINAL_TUPLES
from processing.product_schema import MARKETPLACES
from streamlit_app.ui_common import read_json

st.title("Tuple Viewer")
ean = st.text_input("EAN")
payload = read_json(FINAL_TUPLES, {"products": {}})
row = payload.get("products", {}).get(ean) if ean else None

if ean and not row:
    st.warning("No tuple found for this EAN.")
elif row:
    for site in MARKETPLACES:
        card = row.get(site)
        st.header(site.title())
        if not card:
            st.caption("No accepted product.")
            continue
        cols = st.columns([1, 3])
        with cols[0]:
            if card.get("image"):
                st.image(card["image"], use_container_width=True)
        with cols[1]:
            st.write(card.get("title"))
            st.write(card.get("price"))
            if card.get("url"):
                st.link_button("Open product", card["url"])
