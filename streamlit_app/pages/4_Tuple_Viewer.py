from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.platform_paths import FINAL_TUPLES
from processing.excel_export import excel_bytes, tuple_export_rows
from processing.product_schema import MARKETPLACES
from processing.product_store import tuples_with_latest_prices
from streamlit_app.ui_common import read_json


def _card_value(card: dict | None, key: str) -> str:
    if not isinstance(card, dict):
        return "-"
    value = card.get(key)
    if value is None or str(value).strip() == "":
        return "-"
    return str(value)


def _flatten_row(ean: str, row: dict) -> dict:
    amazon = row.get("amazon") if isinstance(row, dict) else None
    flat = {
        "Amazon Image": _card_value(amazon, "image"),
        "Canonical Product ID": _card_value(row, "canonical_product_id"),
        "EAN": ean,
        "Amazon Price": _card_value(amazon, "normal_price") if _card_value(amazon, "normal_price") != "-" else _card_value(amazon, "price"),
        "AJIO Price": _card_value(row.get("ajio") if isinstance(row, dict) else None, "normal_price") if _card_value(row.get("ajio") if isinstance(row, dict) else None, "normal_price") != "-" else _card_value(row.get("ajio") if isinstance(row, dict) else None, "price"),
        "AJIO Special Price": _card_value(row.get("ajio") if isinstance(row, dict) else None, "offer_price"),
    }

    price_headers = {
        "columbia": "Columbia Price",
        "adventuras": "Adventure Price",
        "myntra": "Myntra Price",
        "tatacliq": "TataCliQ Price",
    }
    for site, header in price_headers.items():
        flat[header] = _card_value(row.get(site) if isinstance(row, dict) else None, "price")

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
payload = tuples_with_latest_prices(payload)
products = payload.get("products", {}) if isinstance(payload, dict) else {}

if not isinstance(products, dict) or not products:
    st.info("No tuples available yet.")
    st.stop()

rows = [_flatten_row(str(ean), row if isinstance(row, dict) else {}) for ean, row in products.items()]
rows.sort(key=lambda item: item.get("EAN", ""))

query = st.text_input("Filter by EAN or title", "").strip().lower()
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
            "Amazon Image": st.column_config.ImageColumn("Amazon Image"),
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
