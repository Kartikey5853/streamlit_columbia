from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import streamlit as st

from processing.excel_export import excel_bytes, tuple_export_rows
from processing.pipeline_artifacts import ARTIFACTS, export_pipeline_artifacts
from processing.platform_paths import FINAL_TUPLES
from processing.product_store import tuples_with_latest_prices
from streamlit_app.ui_common import read_json


SITE_LABELS = {
    "amazon": "Amazon",
    "ajio": "AJIO",
    "columbia": "Columbia",
    "adventuras": "Adventure",
    "myntra": "Myntra",
    "tatacliq": "TataCliQ",
}


def _card_field(row: dict, site: str, key: str) -> str:
    card = row.get(site) if isinstance(row, dict) else None
    if not isinstance(card, dict):
        return "-"
    value = card.get(key)
    if value is None or str(value).strip() == "":
        return "-"
    return str(value)


def _build_excel_rows(products: dict) -> list[dict]:
    rows = []
    for ean, row in sorted(products.items()):
        item = {"EAN": str(ean)}
        for site, label in SITE_LABELS.items():
            item[f"{label} Price"] = _card_field(row, site, "price")
        for site, label in SITE_LABELS.items():
            item[f"{label} Link"] = _card_field(row, site, "url")
        for site, label in SITE_LABELS.items():
            item[f"{label} Title"] = _card_field(row, site, "title")
        rows.append(item)
    return rows


def _to_excel_bytes(rows: list[dict]) -> bytes:
    try:
        from openpyxl import Workbook
    except Exception as exc:
        raise RuntimeError("openpyxl is required for Excel export. Please install openpyxl.") from exc

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "tuples"

    if not rows:
        sheet.append(["EAN"])
    else:
        headers = list(rows[0].keys())
        sheet.append(headers)
        for row in rows:
            sheet.append([row.get(header, "-") for header in headers])

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer.read()


st.title("Export")

st.subheader("Pipeline Artifacts")
st.caption("Exports CLIP index/metadata/cache plus final tuples and the persistent canonical source-product mapping.")
if st.button("Create pipeline artifact zip"):
    st.download_button(
        "Download pipeline_artifacts.zip",
        data=export_pipeline_artifacts(),
        file_name="pipeline_artifacts.zip",
        mime="application/zip",
    )

st.divider()
st.subheader("Export Tuples To Excel")
payload = tuples_with_latest_prices(read_json(FINAL_TUPLES, {"products": {}}))
products = payload.get("products", {}) if isinstance(payload, dict) else {}

if not isinstance(products, dict) or not products:
    st.info("No tuples found to export.")
else:
    options = {
        "identifiers": st.checkbox("Include EAN / identifiers", value=True),
        "source_ids": st.checkbox("Include source product IDs", value=True),
        "prices": st.checkbox("Include prices", value=True),
        "special_prices": st.checkbox("Include special/offer prices", value=True),
        "titles": st.checkbox("Include product titles", value=True),
        "urls": st.checkbox("Include product page URLs", value=True),
        "image_urls": st.checkbox("Include image URLs", value=True),
    }
    rows = tuple_export_rows(products, options)
    st.caption(f"Rows: {len(rows)}")
    if st.button("Create Excel file"):
        try:
            workbook_bytes = excel_bytes(rows)
            st.download_button(
                "Download tuples.xlsx",
                data=workbook_bytes,
                file_name="tuples.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception as exc:
            st.error(str(exc))
