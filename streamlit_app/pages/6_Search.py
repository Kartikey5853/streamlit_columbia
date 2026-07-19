from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.image_search import search_images
from processing.platform_paths import FINAL_TUPLES
from processing.product_store import tuples_with_latest_prices
from streamlit_app.ui_common import read_json


def _render_tuple(row: dict | None) -> None:
    if not isinstance(row, dict):
        st.write("-")
        return
    for site in ("amazon", "ajio", "columbia", "adventuras", "myntra", "tatacliq"):
        card = row.get(site)
        cols = st.columns([1, 3])
        with cols[0]:
            if isinstance(card, dict) and card.get("image"):
                st.image(card["image"], width=120)
            else:
                st.write("-")
        with cols[1]:
            st.markdown(f"**{site.title()}**")
            if not isinstance(card, dict):
                st.write("-")
                continue
            st.write(card.get("title") or "-")
            st.write(card.get("price") or "-")
            if card.get("url"):
                st.markdown(f"[Open {site}]({card['url']})")


st.title("Search")
st.caption("Search complete canonical tuples by EAN or by up to 50 images.")

ean_query = st.text_area("Batch EAN search (one EAN per line or comma-separated)", "")
if ean_query and st.button("Search EANs"):
    requested = {value.strip() for value in ean_query.replace(",", "\n").splitlines() if value.strip()}
    tuples = tuples_with_latest_prices(read_json(FINAL_TUPLES, {"products": {}})).get("products", {})
    matches = [{"EAN": ean, **row} for ean, row in tuples.items() if str(ean) in requested]
    if matches:
        for match in matches:
            with st.expander(f"EAN {match['EAN']}", expanded=True):
                _render_tuple(match)
    else:
        st.info("No final canonical tuples matched those EANs.")

st.subheader("Batch Image Search")

top_k = st.number_input("Top K candidates", min_value=1, max_value=50, value=5, step=1)
minimum_similarity = st.slider("Minimum confidence", min_value=0.0, max_value=1.0, value=0.0, step=0.01)
uploads = st.file_uploader(
    "Upload images",
    type=["jpg", "jpeg", "png", "webp"],
    accept_multiple_files=True,
)

if uploads and len(uploads) > 50:
    st.error("Please upload 50 images or fewer.")

if uploads and 0 < len(uploads) <= 50 and st.button("Run batch search"):
    temp_paths: list[Path] = []
    try:
        for file in uploads:
            suffix = Path(file.name).suffix or ".jpg"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                handle.write(file.getbuffer())
                temp_paths.append(Path(handle.name))

        with st.spinner("Running CLIP + FAISS search..."):
            output = search_images(temp_paths, top_k=int(top_k), minimum_similarity=float(minimum_similarity))

        results = output.get("results", []) if isinstance(output, dict) else []
        if not results:
            st.warning("No matches found.")
        else:
            summary_rows = []
            for item in results:
                summary_rows.append({
                    "Filename": item.get("filename") or "-",
                    "Matched EAN": item.get("matched_ean") or "-",
                    "Confidence": item.get("confidence") if item.get("confidence") is not None else "-",
                })

            try:
                import pandas as pd

                st.dataframe(pd.DataFrame(summary_rows), use_container_width=True)
            except Exception:
                st.write(summary_rows)

            for index, item in enumerate(results, start=1):
                filename = item.get("filename") or f"image_{index}"
                ean = item.get("matched_ean") or "-"
                confidence = item.get("confidence")
                header = f"{filename} | EAN: {ean} | Confidence: {confidence if confidence is not None else '-'}"
                with st.expander(header):
                    row = item.get("tuple")
                    if isinstance(row, dict):
                        row = {"EAN": ean, **row}
                    _render_tuple(row)
    except Exception as exc:
        st.error(str(exc))
    finally:
        for temp_path in temp_paths:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
