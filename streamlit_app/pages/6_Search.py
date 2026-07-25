from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streamlit_app.ui_common import apply_theme

apply_theme()

from processing.excel_export import excel_bytes
from processing.image_search import search_images
from processing.product_schema import availability_display, format_inr, normalize_sku, price_value
from processing.unified_products import load_normalized_products, resolve_normalized_product


SITE_LABELS = {
    "amazon": "Amazon", "ajio": "AJIO", "columbia": "Columbia",
    "adventuras": "Adventuras", "myntra": "Myntra", "tatacliq": "TataCliQ",
}


def _card_price(site: str, card: dict | None) -> str:
    if not isinstance(card, dict):
        return "NA"
    for field in ("price_value", "offer_price_value", "normal_price_value", "price", "offer_price", "normal_price"):
        value = card.get(field)
        if isinstance(value, dict):
            value = value.get("value") or value.get("formattedValue")
        parsed = price_value(value)
        if parsed is not None:
            return str(availability_display(site, card, format_inr(parsed) or f"INR {parsed:,.2f}"))
    return str(availability_display(site, card, "NA"))


def _render_platform_card(site: str, label: str, card: dict | None) -> None:
    """Render a fixed text-left/image-right card without empty image columns."""
    text, image = st.columns([5, 1], vertical_alignment="center")
    with text:
        st.markdown(f"**{label}**")
        if not isinstance(card, dict):
            st.write("Price: NA")
            st.caption("Not matched / not present in the data")
            return
        if site != "ajio":
            st.write(card.get("title") or "NA")
            st.write(f"Price: {_card_price(site, card)}")
            identifier = card.get("source_product_id") or card.get("product_id") or card.get("asin") or card.get("sku")
            st.caption(f"ID: {identifier or 'NA'}")
        else:
            # AJIO data has inconsistent nested price objects; show only its
            # normalized selling price as requested.
            st.write(f"Price: {_card_price(site, card)}")
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
        card = row.get(source)
        if source == "adventure" and not isinstance(card, dict):
            card = row.get("adventuras")
        _render_platform_card(source, label, card)


def _card_value(card: dict | None, *fields: str) -> str:
    if not isinstance(card, dict):
        return "NA"
    for field in fields:
        value = card.get(field)
        if value not in (None, "", [], {}):
            return str(value)
    return "NA"


def _export_row(sku: str, row: dict, filename: str | None = None) -> dict:
    sources = (
        ("amazon", "Amazon"),
        ("ajio", "AJIO"),
        ("adventure", "Adventuras"),
        ("columbia", "Columbia"),
        ("myntra", "Myntra"),
        ("tatacliq", "TataCliQ"),
    )
    columbia = row.get("columbia") if isinstance(row.get("columbia"), dict) else None
    image_card = next((
        row.get(source) if source != "adventure" else row.get("adventure") or row.get("adventuras")
        for source, _label in sources
        if isinstance(row.get(source) if source != "adventure" else row.get("adventure") or row.get("adventuras"), dict)
    ), columbia)
    result = {
        "Search File": filename or "Identifier search",
        "Canonical Product ID": row.get("canonical_product_id") or f"canonical:sku:{sku}",
        "Columbia SKU": row.get("sku") or row.get("columbia_sku") or _card_value(columbia, "sku") or sku,
        "EAN(s)": ", ".join(row.get("ean_numbers") or []) or row.get("EAN") or row.get("ean") or "NA",
        "Product Image": _card_value(image_card, "image", "image_url"),
    }
    for source, label in sources:
        card = row.get(source)
        if source == "adventure" and not isinstance(card, dict):
            card = row.get("adventuras")
        result[f"{label} Product ID"] = _card_value(card, "source_product_id", "product_id", "asin")
        result[f"{label} SKU"] = _card_value(card, "sku")
        result[f"{label} Title"] = _card_value(card, "title")
        result[f"{label} Price"] = _card_price(source, card if isinstance(card, dict) else None)
        result[f"{label} URL"] = _card_value(card, "url")
    return result


def _render_export_button() -> None:
    rows = st.session_state.get("search_export_rows") or []
    if not rows:
        return
    st.download_button(
        f"Export {len(rows)} search result(s) to Excel",
        data=excel_bytes(rows, "search_results"),
        file_name="search_results.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


st.title("Search")
st.info(
    "Enter image(s), SKU(s), or any other product ID to view all the data we have for a product.\n\n"
    "• For SKUs, enter them on separate lines or separate them with commas.  \n"
    "• For images, upload one or more files using the **Browse Files** button.  \n"
    "• Image search may take some time. Please wait until the first batch has been fully processed before uploading another batch."
)
normalized_payload = load_normalized_products()
st.session_state.setdefault("search_export_rows", [])

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
        st.session_state["search_export_rows"] = [_export_row(sku, row) for sku, row in resolved if isinstance(row, dict)]
        _render_export_button()
        for canonical_id, row in resolved:
            with st.expander(f"{canonical_id}", expanded=True):
                if isinstance(row, dict) and "ean_numbers" in row:
                    _render_unified_row(row)
    elif not missing:
        st.session_state["search_export_rows"] = []
        st.info("No final canonical tuples matched those inputs.")
else:
    _render_export_button()

st.subheader("Batch Image Search")
uploads = st.file_uploader("Upload image(s)", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True)

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
            # Search uses the pipeline's established candidate and confidence
            # defaults; this page is intentionally upload-only.
            output = search_images(temp_paths)
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
            export_rows: list[dict] = []
            for index, item in enumerate(results, start=1):
                canonical_id = item.get("canonical_product_id") or "-"
                row = item.get("tuple")
                if canonical_id in displayed or not isinstance(row, dict):
                    continue
                displayed.add(canonical_id)
                export_rows.append(_export_row(str(canonical_id), row, item.get("filename")))
                with st.expander(f"{item.get('filename') or f'image_{index}'} | {canonical_id}"):
                    _render_unified_row(row)
            st.session_state["search_export_rows"] = export_rows
            _render_export_button()
    except Exception as exc:
        st.error(str(exc))
    finally:
        for temp_path in temp_paths:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
