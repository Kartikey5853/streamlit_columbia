from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.platform_paths import FINAL_TUPLES
from processing.excel_export import excel_bytes, tuple_export_rows
from processing.product_schema import MARKETPLACES, format_inr, normalize_sku, price_value
from processing.product_store import latest_price_timestamp, tuples_with_latest_prices
from streamlit_app.ui_common import read_json


def _card_value(card: dict | None, key: str) -> str:
    if not isinstance(card, dict):
        return "-"
    value = card.get(key)
    if value is None or str(value).strip() == "":
        return "-"
    return (normalize_sku(value) or "-") if key == "sku" else str(value)


def _normalized_price(row: dict, site: str, card: dict | None, field: str = "price") -> str:
    status = row.get("status", {}).get(site, {}) if isinstance(row.get("status"), dict) else {}
    if isinstance(card, dict) and (card.get("availability") is False or status.get("available") is False):
        return "OOS"
    if not isinstance(card, dict):
        return "NA"
    raw = card.get(field) or card.get("normal_price") or card.get("price")
    return format_inr(price_value(raw)) or "NA"


def _primary_card(row: dict) -> dict | None:
    for site in ("amazon", "columbia", "ajio", "myntra", "tatacliq", "adventuras"):
        card = row.get(site)
        if isinstance(card, dict):
            return card
    return None


def _flatten_row(canonical_id: str, row: dict) -> dict:
    amazon = row.get("amazon") if isinstance(row, dict) else None
    primary = _primary_card(row)
    ean = row.get("EAN") or "-"
    flat = {
        "Product Image": _card_value(primary, "image"),
        "Canonical Product ID": _card_value(row, "canonical_product_id") if _card_value(row, "canonical_product_id") != "-" else canonical_id,
        "EAN": ean,
        "Columbia SKU": normalize_sku(row.get("columbia_sku") or (row.get("columbia") or {}).get("sku")) or "-",
        "Columbia Product ID": _card_value(row, "columbia_product_id") if _card_value(row, "columbia_product_id") != "-" else _card_value(row.get("columbia"), "source_product_id"),
        "Amazon Price": _normalized_price(row, "amazon", amazon),
        "AJIO Price": _normalized_price(row, "ajio", row.get("ajio") if isinstance(row, dict) else None),
        "AJIO Special Price": _normalized_price(row, "ajio", row.get("ajio") if isinstance(row, dict) else None, "offer_price"),
    }

    price_headers = {
        "columbia": "Columbia Price",
        "adventuras": "Adventure Price",
        "myntra": "Myntra Price",
        "tatacliq": "TataCliQ Price",
    }
    for site, header in price_headers.items():
        flat[header] = _normalized_price(row, site, row.get(site) if isinstance(row, dict) else None)

    labels = {
        "amazon": "Amazon",
        "ajio": "AJIO",
        "columbia": "Columbia",
        "adventuras": "Adventure",
        "myntra": "Myntra",
        "tatacliq": "TataCliQ",
    }
    for site in MARKETPLACES:
        card = row.get(site) if isinstance(row, dict) else None
        label = labels[site]
        flat[f"{label} Title"] = _card_value(card, "title")
        flat[f"{label} URL"] = _card_value(card, "url")
        flat[f"{label} Image"] = _card_value(card, "image")
    return flat


st.title("Tuple Viewer")
payload = read_json(FINAL_TUPLES, {"products": {}})
refresh_col, timestamp_col = st.columns([1, 3])
with refresh_col:
    refresh_requested = st.button("Refresh Prices", use_container_width=True)
payload = tuples_with_latest_prices(payload)
if refresh_requested:
    st.session_state["tuple_price_refresh_message"] = "Prices refreshed from latest scraper data."
if st.session_state.get("tuple_price_refresh_message"):
    st.success(st.session_state["tuple_price_refresh_message"])
with timestamp_col:
    timestamp = latest_price_timestamp()
    st.caption(f"Latest price data loaded: {timestamp or 'not available'}")
products = payload.get("products", {}) if isinstance(payload, dict) else {}

if not isinstance(products, dict) or not products:
    st.info("No tuples available yet.")
    st.stop()

rows = [_flatten_row(str(key), row if isinstance(row, dict) else {}) for key, row in products.items()]
rows.sort(key=lambda item: item.get("Canonical Product ID", ""))

query = st.text_input("Filter by canonical ID, SKU, EAN, product ID, or title", "").strip().lower()
if query:
    filtered = []
    for row in rows:
        haystack = " ".join(str(value) for value in row.values()).lower()
        if query in haystack:
            filtered.append(row)
    rows = filtered

st.caption(f"Showing {len(rows)} tuples")

with st.expander("Excel export options"):
    export_options = {
        "identifiers": st.checkbox("Include EAN / identifiers", value=True),
        "source_ids": st.checkbox("Include source product IDs", value=True),
        "prices": st.checkbox("Include prices", value=True),
        "special_prices": st.checkbox("Include special/offer prices", value=True),
        "titles": st.checkbox("Include product titles", value=True),
        "urls": st.checkbox("Include product page URLs", value=True),
        "image_urls": st.checkbox("Include image URLs", value=True),
    }
    if st.button("Create Excel export"):
        try:
            st.download_button(
                "Download tuples.xlsx",
                data=excel_bytes(tuple_export_rows(products, export_options)),
                file_name="tuples.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception as exc:
            st.error(str(exc))

try:
    import pandas as pd

    dataframe = pd.DataFrame(rows)
    st.dataframe(
        dataframe,
        use_container_width=True,
        height=680,
        column_config={
            "Product Image": st.column_config.ImageColumn("Product Image"),
            "Amazon URL": st.column_config.LinkColumn("Amazon URL"),
            "AJIO URL": st.column_config.LinkColumn("AJIO URL"),
            "Columbia URL": st.column_config.LinkColumn("Columbia URL"),
            "Adventure URL": st.column_config.LinkColumn("Adventure URL"),
            "Myntra URL": st.column_config.LinkColumn("Myntra URL"),
            "TataCliQ URL": st.column_config.LinkColumn("TataCliQ URL"),
            "AJIO Image": st.column_config.ImageColumn("AJIO Image"),
            "Columbia Image": st.column_config.ImageColumn("Columbia Image"),
            "Adventure Image": st.column_config.ImageColumn("Adventure Image"),
            "Myntra Image": st.column_config.ImageColumn("Myntra Image"),
            "TataCliQ Image": st.column_config.ImageColumn("TataCliQ Image"),
        },
    )
except Exception:
    st.write(rows)
