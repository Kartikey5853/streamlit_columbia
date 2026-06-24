from pathlib import Path
import tempfile
import sys

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.image_search import search_image

st.title("Image Search")
uploaded = st.file_uploader("Upload image", type=["jpg", "jpeg", "png", "webp"])

if uploaded and st.button("Search"):
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(uploaded.name).suffix) as handle:
        handle.write(uploaded.getbuffer())
        image_path = Path(handle.name)
    try:
        with st.spinner("Searching image index..."):
            result = search_image(image_path)
    except Exception as exc:
        st.error(str(exc))
    else:
        if not result:
            st.warning("No Amazon-index tuple was found for this image.")
        else:
            # Be tolerant of multiple result shapes: dict (expected), list/tuple, or others.
            ean = "-"
            row = {}
            if isinstance(result, dict):
                ean = result.get("EAN") or result.get("ean") or "-"
                row = result.get("tuple") or result.get("tuples") or {}
            elif isinstance(result, (list, tuple)) and result:
                first = result[0]
                if isinstance(first, dict):
                    ean = first.get("EAN") or first.get("ean") or "-"
                    row = first.get("tuple") or first.get("tuples") or {}
                else:
                    ean = str(first)
            else:
                try:
                    # Fallback: try treating result as a mapping-like object
                    ean = str(result.get("EAN") if hasattr(result, "get") else result)
                except Exception:
                    ean = str(result)

            st.subheader(f"EAN {ean}")
            if not isinstance(row, dict):
                row = {}
            for site, card in row.items():
                if site in {"EAN", "match", "updated_at"} or not isinstance(card, dict):
                    continue
                st.write(f"{site.title()}: {card.get('title')} | {card.get('price')}")
                if card.get("image"):
                    st.image(card["image"], width=160)
                if card.get("url"):
                    st.link_button(f"Open {site}", card["url"])
