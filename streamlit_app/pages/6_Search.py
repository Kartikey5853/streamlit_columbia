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
from processing.product_schema import normalize_sku
from processing.product_store import resolve_tuples, tuples_with_latest_prices
from streamlit_app.ui_common import read_json


SITE_LABELS = {
    "amazon": "Amazon", "ajio": "AJIO", "columbia": "Columbia",
    "adventuras": "Adventuras", "myntra": "Myntra", "tatacliq": "TataCliQ",
}


def _render_tuple(row: dict | None) -> None:
    if not isinstance(row, dict):
        st.write("-")
        return
    st.caption(
        f"Canonical Product ID: {row.get('canonical_product_id') or '-'} • "
        f"Columbia SKU: {row.get('columbia_sku') or '-'} • EAN: {row.get('EAN') or '-'}"
    )
    for site, label in SITE_LABELS.items():
        card = row.get(site)
        cols = st.columns([1, 3])
        with cols[0]:
            if isinstance(card, dict) and card.get("image"):
                st.image(card["image"], width=120)
            else:
                st.write("-")
        with cols[1]:
            st.markdown(f"**{label}**")
            if not isinstance(card, dict):
                st.write("-")
                continue
            st.write(card.get("title") or "-")
            st.write(f"Product ID: {card.get('source_product_id') or card.get('product_id') or '-'}")
            if card.get("sku"):
                st.write(f"SKU: {normalize_sku(card['sku']) or '-'}")
            normal = card.get("normal_price") or card.get("price") or "-"
            st.write(f"Price: {normal}")
            if site == "ajio":
                st.write(f"Special Price: {card.get('offer_price') or '-'}")
            if card.get("url"):
                st.markdown(f"[Open {label}]({card['url']})")


st.title("Search")
st.caption("Resolve complete canonical tuples by identifier/text, or by image. Exact identifiers are preferred; title searches support partial/fuzzy matching.")
payload = tuples_with_latest_prices(read_json(FINAL_TUPLES, {"products": {}}))

identifier_query = st.text_area(
    "Identifier or title search (one per line, comma-separated also supported)",
    placeholder="Columbia SKU, EAN, canonical ID, ASIN, AJIO/Myntra/Tata/Columbia/Adventuras product ID, or product title",
)
if identifier_query and st.button("Search products"):
    requested = [value.strip() for value in identifier_query.replace(",", "\n").splitlines() if value.strip()]
    resolved: list[tuple[str, dict]] = []
    seen: set[str] = set()
    missing: list[str] = []
    for value in requested:
        found = resolve_tuples(value, payload)
        if not found:
            missing.append(value)
            continue
        for canonical_id, row in found:
            if canonical_id not in seen:
                seen.add(canonical_id)
                resolved.append((canonical_id, row))
    if missing:
        st.caption("No tuple found for: " + ", ".join(missing))
    if resolved:
        for canonical_id, row in resolved:
            with st.expander(f"{canonical_id} | {row.get('columbia_sku') or '-'}", expanded=True):
                _render_tuple(row)
    elif not missing:
        st.info("No final canonical tuples matched those inputs.")

st.subheader("Batch Image Search")
top_k = st.number_input("Top K candidates", min_value=1, max_value=50, value=5, step=1)
minimum_similarity = st.slider("Minimum confidence", min_value=0.0, max_value=1.0, value=0.0, step=0.01)
uploads = st.file_uploader("Upload images", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True)

if uploads and len(uploads) > 50:
    st.error("Please upload 50 images or fewer.")

if uploads and 0 < len(uploads) <= 50 and st.button("Run batch image search"):
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
            summary_rows = [{
                "Filename": item.get("filename") or "-",
                "Canonical Product ID": item.get("canonical_product_id") or "-",
                "EAN": item.get("matched_ean") or "-",
                "Confidence": item.get("confidence") if item.get("confidence") is not None else "-",
            } for item in results]
            try:
                import pandas as pd
                st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)
            except Exception:
                st.write(summary_rows)
            displayed: set[str] = set()
            for index, item in enumerate(results, start=1):
                canonical_id = item.get("canonical_product_id") or "-"
                row = item.get("tuple")
                if canonical_id in displayed or not isinstance(row, dict):
                    continue
                displayed.add(canonical_id)
                with st.expander(f"{item.get('filename') or f'image_{index}'} | {canonical_id}"):
                    _render_tuple(row)
    except Exception as exc:
        st.error(str(exc))
    finally:
        for temp_path in temp_paths:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
