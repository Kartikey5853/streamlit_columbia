from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.image_search import search_images
from processing.product_schema import format_inr, normalize_sku, price_value
from processing.unified_products import flattened_rows, load_normalized_products, resolve_normalized_product


SITE_LABELS = {
    "amazon": "Amazon", "ajio": "AJIO", "columbia": "Columbia",
    "adventuras": "Adventuras", "myntra": "Myntra", "tatacliq": "TataCliQ",
}


def _card_price(card: dict | None) -> str:
    if not isinstance(card, dict):
        return "NA"
    for field in ("price_value", "offer_price_value", "normal_price_value", "price", "offer_price", "normal_price"):
        value = card.get(field)
        if isinstance(value, dict):
            value = value.get("value") or value.get("formattedValue")
        parsed = price_value(value)
        if parsed is not None:
            return format_inr(parsed) or f"INR {parsed:,.2f}"
    return "NA"


def _render_platform_card(site: str, label: str, card: dict | None) -> None:
    """Render a fixed text-left/image-right card without empty image columns."""
    if not isinstance(card, dict):
        return
    text, image = st.columns([5, 1], vertical_alignment="center")
    with text:
        st.markdown(f"**{label}**")
        if site != "ajio":
            st.write(card.get("title") or "NA")
            st.write(f"Price: {_card_price(card)}")
            identifier = card.get("source_product_id") or card.get("product_id") or card.get("asin") or card.get("sku")
            st.caption(f"ID: {identifier or 'NA'}")
        else:
            # AJIO data has inconsistent nested price objects; show only its
            # normalized selling price as requested.
            st.write(f"Price: {_card_price(card)}")
        if card.get("url"):
            st.link_button(f"Open {label}", card["url"])
    with image:
        if card.get("image"):
            st.image(card["image"], use_container_width=True)


def _render_unified_row(row: dict | None) -> None:
    if not isinstance(row, dict):
        st.write("-")
        return
    st.caption(
        f"Columbia SKU: {row.get('sku') or '-'} | "
        f"EAN(s): {', '.join(row.get('ean_numbers') or []) or '-'}"
    )
    for source, label in (("amazon", "Amazon"), ("ajio", "AJIO"), ("adventure", "Adventuras"), ("columbia", "Columbia"), ("myntra", "Myntra"), ("tatacliq", "TataCliq")):
        _render_platform_card(source, label, row.get(source))


st.title("Search")
st.caption("Resolve the unified product tuple by normalized SKU or by any EAN attached to that product.")
normalized_payload = load_normalized_products()

identifier_query = st.text_area(
    "Identifier or title search (one per line, comma-separated also supported)",
    placeholder="SKU or EAN for unified records, or a legacy canonical ID/title",
)
if identifier_query and st.button("Search products"):
    requested = [value.strip() for value in identifier_query.replace(",", "\n").splitlines() if value.strip()]
    resolved: list[tuple[str, dict]] = []
    seen: set[str] = set()
    missing: list[str] = []
    for value in requested:
        unified = resolve_normalized_product(value, normalized_payload)
        if unified:
            sku, row = unified
            if sku not in seen:
                seen.add(sku)
                resolved.append((sku, row))
            continue
        missing.append(value)
    if missing:
        st.caption("No tuple found for: " + ", ".join(missing))
    if resolved:
        for canonical_id, row in resolved:
            with st.expander(f"{canonical_id}", expanded=True):
                if isinstance(row, dict) and "ean_numbers" in row:
                    _render_unified_row(row)
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
                    _render_unified_row(row)
    except Exception as exc:
        st.error(str(exc))
    finally:
        for temp_path in temp_paths:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
