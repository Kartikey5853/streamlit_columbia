from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.excel_export import excel_bytes
from processing.platform_paths import FINAL_TUPLES
from processing.product_schema import format_inr, normalize_sku
from processing.product_store import all_price_history_by_tuple, price_history_for, resolve_tuples, tuples_with_latest_prices
from streamlit_app.ui_common import read_json


SITE_LABELS = {
    "amazon": "Amazon", "ajio": "AJIO", "columbia": "Columbia",
    "adventuras": "Adventuras", "myntra": "Myntra", "tatacliq": "TataCliQ",
}


def _tuple_title(row: dict) -> str:
    for site in ("columbia", "amazon", "ajio", "myntra", "tatacliq", "adventuras"):
        card = row.get(site)
        if isinstance(card, dict) and card.get("title"):
            return str(card["title"])
    return "Untitled product"


def _site_identifier(row: dict, site: str) -> tuple[str, str] | None:
    """Return the marketplace-owned ID that lets a user identify this listing."""
    card = row.get(site)
    if not isinstance(card, dict):
        return None
    if site == "amazon":
        value = card.get("ean") or row.get("EAN")
        if value:
            return "EAN", str(value)
        value = card.get("asin") or card.get("source_product_id")
        return ("ASIN", str(value)) if value else None
    if site == "columbia":
        value = normalize_sku(card.get("sku") or row.get("columbia_sku"))
        return ("SKU", value) if value else None
    value = card.get("product_id") or card.get("source_product_id") or card.get("sku")
    return ("Product ID", str(value)) if value else None


def _site_label(row: dict, site: str) -> str:
    identifier = _site_identifier(row, site)
    if not identifier:
        return SITE_LABELS[site]
    label, value = identifier
    return f"{SITE_LABELS[site]} · {label}: {value}"


st.title("Time Series")
st.caption("Find a canonical tuple by ID, SKU, EAN, source product ID/ASIN, or title; then select one available platform.")
payload = tuples_with_latest_prices(read_json(FINAL_TUPLES, {"products": {}}))


def _price_changes(products: dict) -> list[dict]:
    history_by_tuple = all_price_history_by_tuple()
    changes = []
    for canonical_id, row in products.items():
        if not isinstance(row, dict):
            continue
        for site in SITE_LABELS:
            history = [r for r in history_by_tuple.get(str(canonical_id), []) if r.get("source") == site]
            history.sort(key=lambda r: str(r.get("scrape_date") or ""))
            if len(history) < 2:
                continue
            previous = history[-2]
            latest = history[-1]
            previous_price = previous.get("offer_price_value") if previous.get("offer_price_value") is not None else previous.get("normal_price_value")
            latest_price = latest.get("offer_price_value") if latest.get("offer_price_value") is not None else latest.get("normal_price_value")
            if previous_price is None or latest_price is None or float(previous_price) == float(latest_price):
                continue
            changes.append({
                "Canonical Product ID": canonical_id,
                "Product": _tuple_title(row),
                "Platform": _site_label(row, site),
                "Previous Price": format_inr(float(previous_price)),
                "Latest Price": format_inr(float(latest_price)),
                "Change": format_inr(float(latest_price) - float(previous_price)),
                "Changed On": latest.get("scrape_date"),
                "Source Product ID": latest.get("source_product_id"),
            })
    return sorted(changes, key=lambda item: str(item.get("Changed On") or ""), reverse=True)


st.subheader("Products with changed prices")
changed_rows = _price_changes(payload.get("products", {}) if isinstance(payload, dict) else {})
if changed_rows:
    try:
        import pandas as pd
        st.dataframe(pd.DataFrame(changed_rows), use_container_width=True, hide_index=True)
    except Exception:
        st.write(changed_rows)
else:
    st.info("No product price changes found in the stored history.")

query = st.text_input("Search product", placeholder="Canonical ID, Columbia SKU, EAN, ASIN/product ID, or title")

if not query.strip():
    st.info("Enter an identifier or title to find a canonical product.")
    st.stop()

matches = resolve_tuples(query, payload)
if not matches:
    st.warning("No canonical tuple matched that identifier or title.")
    st.stop()

labels = {
    f"{canonical_id} | {row.get('columbia_sku') or '-'} | {_tuple_title(row)}": (canonical_id, row)
    for canonical_id, row in matches
}
selected_label = st.selectbox("Matching canonical tuples", list(labels))
canonical_id, row = labels[selected_label]
st.caption(f"Canonical Product ID: {canonical_id} • Columbia SKU: {row.get('columbia_sku') or '-'} • EAN: {row.get('EAN') or '-'}")

available_sites = [site for site in SITE_LABELS if isinstance(row.get(site), dict)]
if not available_sites:
    st.warning("This canonical tuple has no source products to chart.")
    st.stop()
platform = st.selectbox("Platform", available_sites, format_func=lambda site: _site_label(row, site))
history = [record for record in price_history_for(canonical_id) if record.get("source") == platform]

if not history:
    st.info(f"No stored {SITE_LABELS[platform]} price observations exist for this tuple yet.")
    st.stop()

chart_rows = []
table_rows = []
for record in history:
    normal = record.get("normal_price_value")
    special = record.get("offer_price_value")
    selling = special if special is not None else normal
    chart_rows.append({
        "Scrape Date": record.get("scrape_date"),
        "Selling / Special Price": selling,
        "Normal Price": normal,
        "Special Price": special,
    })
    table_rows.append({
        "Date": record.get("scrape_date"),
        "Normal Price": record.get("normal_price"),
        "Offer / Special Price": record.get("offer_price"),
        "Availability": record.get("availability"),
        "Source Product ID": record.get("source_product_id"),
    })

try:
    import pandas as pd

    chart_frame = pd.DataFrame(chart_rows).sort_values("Scrape Date")
    # The main series reflects selling price, including AJIO's special price.
    st.line_chart(chart_frame, x="Scrape Date", y="Selling / Special Price", use_container_width=True)
    st.subheader("Full historical price records")
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
except Exception:
    st.write(chart_rows)
    st.write(table_rows)

if st.button("Create price-history Excel export"):
    st.download_button(
        "Download price_history.xlsx",
        data=excel_bytes(table_rows, "price_history"),
        file_name=f"price_history_{canonical_id.replace(':', '_')}_{platform}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
