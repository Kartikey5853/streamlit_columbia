from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.excel_export import excel_bytes
from processing.json_store import load_json
from processing.platform_paths import PRICE_HISTORY
from processing.product_schema import format_inr, price_value
from processing.unified_products import load_normalized_products, resolve_normalized_product


SITE_LABELS = {
    "amazon": "Amazon", "ajio": "AJIO", "columbia": "Columbia",
    "adventure": "Adventuras", "myntra": "Myntra", "tatacliq": "TataCliq",
}
HISTORY_SOURCE = {"adventure": "adventuras"}


def _title(row: dict) -> str:
    for site in ("columbia", "amazon", "ajio", "adventure", "myntra", "tatacliq"):
        card = row.get(site)
        if isinstance(card, dict) and card.get("title"):
            return str(card["title"])
    return "Untitled product"


def _history_by_sku(products: dict[str, dict]) -> dict[str, dict[str, list[dict]]]:
    """Join stored price-history records to current unified cards by site + ID."""
    raw_records = load_json(PRICE_HISTORY, {"records": {}}).get("records", {})
    by_listing: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if isinstance(raw_records, dict):
        for record in raw_records.values():
            if not isinstance(record, dict):
                continue
            source, product_id = str(record.get("source") or ""), record.get("source_product_id")
            if source and product_id:
                by_listing[(source, str(product_id))].append(record)
    for records in by_listing.values():
        records.sort(key=lambda item: str(item.get("scrape_date") or ""))

    result: dict[str, dict[str, list[dict]]] = {}
    for sku, row in products.items():
        if not isinstance(row, dict):
            continue
        sites: dict[str, list[dict]] = {}
        for site in SITE_LABELS:
            card = row.get(site)
            if not isinstance(card, dict):
                continue
            product_id = card.get("source_product_id") or card.get("product_id")
            records = by_listing.get((HISTORY_SOURCE.get(site, site), str(product_id)), []) if product_id else []
            if records:
                sites[site] = records
        if sites:
            result[str(sku)] = sites
    return result


def _selling_price(record: dict) -> float | None:
    value = record.get("offer_price_value")
    if value is None:
        value = record.get("normal_price_value")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _changed_price_rows(products: dict[str, dict], histories: dict[str, dict[str, list[dict]]]) -> list[dict]:
    rows: list[dict] = []
    for sku, sites in histories.items():
        row = products.get(sku)
        if not isinstance(row, dict):
            continue
        for site, records in sites.items():
            priced = [(record, _selling_price(record)) for record in records]
            priced = [(record, value) for record, value in priced if value is not None]
            if len(priced) < 2:
                continue
            previous_record, previous_price = priced[-2]
            latest_record, latest_price = priced[-1]
            if previous_price == latest_price:
                continue
            card = row.get(site) if isinstance(row.get(site), dict) else {}
            product_id = card.get("source_product_id") or card.get("product_id") or card.get("asin") or "-"
            rows.append({
                "Canonical Product ID": f"canonical:sku:{sku}",
                "Title": card.get("title") or _title(row),
                "Product ID": product_id,
                "Platform": SITE_LABELS[site],
                "Previous Price": format_inr(previous_price) or previous_price,
                "New Price": format_inr(latest_price) or latest_price,
                "Difference": format_inr(latest_price - previous_price) or (latest_price - previous_price),
                "Normalized SKU": sku,
                "EAN(s)": ", ".join(row.get("ean_numbers") or []) or "-",
                "Changed On": latest_record.get("scrape_date") or "-",
                "_site": site,
            })
    return sorted(rows, key=lambda item: str(item["Changed On"]), reverse=True)


def _render_chart(sku: str, row: dict, histories: dict[str, list[dict]], selected_site: str | None = None) -> None:
    sites = list(histories)
    initial_index = sites.index(selected_site) if selected_site in sites else 0
    platform = st.selectbox("Platform", sites, index=initial_index, format_func=lambda site: SITE_LABELS[site])
    chart_rows, table_rows = [], []
    for record in histories[platform]:
        normal, special = record.get("normal_price_value"), record.get("offer_price_value")
        chart_rows.append({
            "Scrape Date": record.get("scrape_date"),
            "Selling / Special Price": special if special is not None else normal,
            "Normal Price": normal,
            "Special Price": special,
        })
        table_rows.append({
            "Date": record.get("scrape_date"),
            "Normalized SKU": sku,
            "Normal Price": record.get("normal_price"),
            "Offer / Special Price": record.get("offer_price"),
            "Availability": record.get("availability"),
        })
    import pandas as pd
    st.line_chart(pd.DataFrame(chart_rows).sort_values("Scrape Date"), x="Scrape Date", y="Selling / Special Price", use_container_width=True)
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
    st.download_button(
        "Download price history.xlsx",
        data=excel_bytes(table_rows, "price_history"),
        file_name=f"price_history_{sku}_{platform}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


st.title("Time Series")
st.caption("Search by normalized SKU or any EAN, or select a row to view its price history.")
payload = load_normalized_products()
products = payload.get("products", {}) if isinstance(payload, dict) else {}
histories = _history_by_sku(products)
query = st.text_input("Search product", placeholder="Normalized SKU or EAN").strip()

selected: tuple[str, dict] | None = None
if query:
    found = resolve_normalized_product(query, payload)
    if not found:
        st.warning("No matching unified product was found.")
    else:
        selected = found
else:
    table_rows = _changed_price_rows(products, histories)
    st.subheader("Products with changed prices")
    try:
        import pandas as pd
        display_rows = [{key: value for key, value in item.items() if key != "_site"} for item in table_rows]
        event = st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True, on_select="rerun", selection_mode="single-row")
        selected_rows = event.selection.rows if event else []
        if selected_rows:
            change = table_rows[selected_rows[0]]
            sku = change["Normalized SKU"]
            selected = (sku, products[sku])
            st.session_state["time_series_selected_site"] = change["_site"]
    except TypeError:
        st.dataframe([{key: value for key, value in item.items() if key != "_site"} for item in table_rows], use_container_width=True, hide_index=True)
        sku = st.selectbox("Choose a product", [row["Normalized SKU"] for row in table_rows])
        selected = (sku, products[sku])

if selected:
    sku, row = selected
    st.divider()
    st.subheader(_title(row))
    st.caption(f"Normalized SKU: {sku} | EAN(s): {', '.join(row.get('ean_numbers') or []) or '-'}")
    row_histories = histories.get(sku, {})
    if row_histories:
        _render_chart(sku, row, row_histories, st.session_state.get("time_series_selected_site"))
    else:
        st.info("This product is in the unified dataset, but no matching historical price records exist yet.")
