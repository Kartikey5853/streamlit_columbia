from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import streamlit as st

from processing.platform_paths import CLIP_INDEX, EMBEDDINGS_DIR, FINAL_TUPLES, METADATA_PKL
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

st.subheader("Deployment Export")
st.markdown("Download only the files required by the frontend deployment: clip.index and metadata.pkl.")

available = []
for path in (CLIP_INDEX, METADATA_PKL):
    if path.exists():
        available.append(path)

if not available:
    st.info(f"No embedding/index files found in {EMBEDDINGS_DIR}")
else:
    st.write("Files to include:")
    for path in available:
        st.write(f"- {path.name}")
    if st.button("Create deployment zip"):
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            for path in available:
                archive.writestr(path.name, path.read_bytes())
        buffer.seek(0)
        st.download_button(
            "Download deployment_embeddings.zip",
            data=buffer.read(),
            file_name="deployment_embeddings.zip",
        )

st.divider()
st.subheader("Export Tuples To Excel")
payload = read_json(FINAL_TUPLES, {"products": {}})
products = payload.get("products", {}) if isinstance(payload, dict) else {}

if not isinstance(products, dict) or not products:
    st.info("No tuples found to export.")
else:
    rows = _build_excel_rows(products)
    st.caption(f"Rows: {len(rows)}")
    if st.button("Create Excel file"):
        try:
            excel_bytes = _to_excel_bytes(rows)
            st.download_button(
                "Download tuples.xlsx",
                data=excel_bytes,
                file_name="tuples.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception as exc:
            st.error(str(exc))
