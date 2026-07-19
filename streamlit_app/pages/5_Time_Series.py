from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.excel_export import excel_bytes
from processing.platform_paths import FINAL_TUPLES
from processing.product_store import price_history_for, tuples_with_latest_prices
from streamlit_app.ui_common import read_json


st.title("Time Series")
st.info("Select a canonical matched product to compare the recorded selling price across sites. AJIO special price is used when available.")
payload = tuples_with_latest_prices(read_json(FINAL_TUPLES, {"products": {}}))
products = payload.get("products", {}) if isinstance(payload, dict) else {}
choices: dict[str, tuple[str, dict]] = {}
for ean, row in products.items():
    if not isinstance(row, dict) or not row.get("canonical_product_id"):
        continue
    title = next((card.get("title") for card in row.values() if isinstance(card, dict) and card.get("title")), "")
    label = f"{row['canonical_product_id']} | {ean} | {title}"
    choices[label] = (str(ean), row)

if not choices:
    st.info("No canonical tuples available yet. Run or import the pipeline first.")
    st.stop()

selected = st.selectbox("Search by canonical product ID, EAN, or title", sorted(choices))
ean, row = choices[selected]
history = price_history_for(str(row["canonical_product_id"]))
if not history:
    st.info("No price observations have been recorded for this tuple yet.")
    st.stop()

chart_rows = []
for record in history:
    selling = record.get("offer_price_value") if record.get("offer_price_value") is not None else record.get("normal_price_value")
    chart_rows.append({"Scrape Date": record.get("scrape_date"), "Site": record.get("source"), "Price": selling})

try:
    import pandas as pd
    frame = pd.DataFrame(chart_rows)
    st.line_chart(frame, x="Scrape Date", y="Price", color="Site", use_container_width=True)
    latest = frame.sort_values("Scrape Date").groupby("Site", as_index=False).tail(1)
    st.dataframe(latest, use_container_width=True)
except Exception:
    st.write(chart_rows)

if st.button("Create price-history Excel export"):
    st.download_button(
        "Download price_history.xlsx",
        data=excel_bytes(history, "price_history"),
        file_name=f"price_history_{ean}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
