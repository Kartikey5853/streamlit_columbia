from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streamlit_app.ui_common import apply_theme

apply_theme()

from processing.excel_export import excel_bytes
from processing.json_store import load_json
from processing.platform_paths import LATEST_PRICES, PRICE_HISTORY
from processing.product_schema import format_inr, price_value
from processing.unified_products import load_normalized_products, resolve_normalized_product


SITE_LABELS = {
    "amazon": "Amazon", "ajio": "AJIO", "columbia": "Columbia",
    "adventure": "Adventuras", "myntra": "Myntra", "tatacliq": "TataCliq",
}
HISTORY_SOURCE = {"adventure": "adventuras"}
SOURCE_SITE = {value: key for key, value in HISTORY_SOURCE.items()}


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
    records_iterable = raw_records.values() if isinstance(raw_records, dict) else raw_records
    if isinstance(records_iterable, list) or hasattr(records_iterable, "__iter__"):
        for record in records_iterable:
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
            source = HISTORY_SOURCE.get(site, site)
            identifiers = [
                card.get("source_product_id"),
                card.get("product_id"),
                card.get("asin"),
                card.get("ean"),
                card.get("sku"),
            ]
            records = []
            for identifier in identifiers:
                if not identifier:
                    continue
                records = by_listing.get((source, str(identifier)), [])
                if records:
                    break
            if records:
                sites[site] = records
        if sites:
            result[str(sku)] = sites
    return result


def _joined_listing_ids(histories: dict[str, dict[str, list[dict]]]) -> set[tuple[str, str]]:
    listing_ids: set[tuple[str, str]] = set()
    for sites in histories.values():
        for site, records in sites.items():
            source = HISTORY_SOURCE.get(site, site)
            for record in records:
                product_id = record.get("source_product_id")
                if product_id:
                    listing_ids.add((source, str(product_id)))
    return listing_ids


def _latest_price_cards() -> dict[tuple[str, str], dict]:
    raw_records = load_json(LATEST_PRICES, {"records": {}}).get("records", {})
    records_iterable = raw_records.values() if isinstance(raw_records, dict) else raw_records
    latest: dict[tuple[str, str], dict] = {}
    if isinstance(records_iterable, list) or hasattr(records_iterable, "__iter__"):
        for record in records_iterable:
            if not isinstance(record, dict):
                continue
            source, product_id = str(record.get("source") or ""), record.get("source_product_id")
            if source and product_id:
                latest[(source, str(product_id))] = record
    return latest


def _selling_price(record: dict) -> float | None:
    value = record.get("offer_price_value")
    if value is None:
        value = record.get("normal_price_value")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _changed_price_rows(products: dict[str, dict], histories: dict[str, dict[str, list[dict]]], window: int = 7) -> list[dict]:
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
            recent = priced[-window:]
            first_record, first_price = recent[0]
            latest_record, latest_price = recent[-1]
            changed_pairs = [
                (previous, current)
                for previous, current in zip(recent, recent[1:])
                if previous[1] != current[1]
            ]
            if not changed_pairs:
                continue
            previous_record, previous_price = changed_pairs[-1][0]
            change_record, change_price = changed_pairs[-1][1]
            card = row.get(site) if isinstance(row.get(site), dict) else {}
            product_id = card.get("source_product_id") or card.get("product_id") or card.get("asin") or "-"
            rows.append({
                "Canonical Product ID": f"canonical:sku:{sku}",
                "Title": card.get("title") or _title(row),
                "Product ID": product_id,
                "Platform": SITE_LABELS[site],
                "Previous Price": format_inr(previous_price) or previous_price,
                "New Price": format_inr(change_price) or change_price,
                "Latest Price": format_inr(latest_price) or latest_price,
                "Difference": format_inr(change_price - previous_price) or (change_price - previous_price),
                "7-Scrape Start Price": format_inr(first_price) or first_price,
                "Normalized SKU": sku,
                "EAN(s)": ", ".join(row.get("ean_numbers") or []) or "-",
                "Changed On": change_record.get("scrape_date") or "-",
                "Latest Scrape": latest_record.get("scrape_date") or "-",
                "_site": site,
            })
    return sorted(rows, key=lambda item: str(item["Changed On"]), reverse=True)


def _history_only_changed_rows(
    histories: dict[str, dict[str, list[dict]]],
    window: int = 7,
) -> list[dict]:
    """Show changed-price rows even when a source listing is not in normalized_products."""
    raw_records = load_json(PRICE_HISTORY, {"records": {}}).get("records", {})
    records_iterable = raw_records.values() if isinstance(raw_records, dict) else raw_records
    by_listing: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if isinstance(records_iterable, list) or hasattr(records_iterable, "__iter__"):
        for record in records_iterable:
            if not isinstance(record, dict):
                continue
            source, product_id = str(record.get("source") or ""), record.get("source_product_id")
            if source and product_id:
                by_listing[(source, str(product_id))].append(record)
    for records in by_listing.values():
        records.sort(key=lambda item: str(item.get("scrape_date") or ""))

    joined = _joined_listing_ids(histories)
    latest_cards = _latest_price_cards()
    rows: list[dict] = []
    for (source, product_id), records in by_listing.items():
        if (source, product_id) in joined:
            continue
        site = SOURCE_SITE.get(source, source)
        if site not in SITE_LABELS:
            continue
        priced = [(record, _selling_price(record)) for record in records]
        priced = [(record, value) for record, value in priced if value is not None]
        if len(priced) < 2:
            continue
        recent = priced[-window:]
        first_record, first_price = recent[0]
        latest_record, latest_price = recent[-1]
        changed_pairs = [
            (previous, current)
            for previous, current in zip(recent, recent[1:])
            if previous[1] != current[1]
        ]
        if not changed_pairs:
            continue
        previous_record, previous_price = changed_pairs[-1][0]
        change_record, change_price = changed_pairs[-1][1]
        latest_card = latest_cards.get((source, product_id), {})
        sku = latest_card.get("sku") or latest_card.get("ean") or product_id
        rows.append({
            "Canonical Product ID": "-",
            "Title": latest_card.get("title") or f"{SITE_LABELS[site]} listing {product_id}",
            "Product ID": product_id,
            "Platform": SITE_LABELS[site],
            "Previous Price": format_inr(previous_price) or previous_price,
            "New Price": format_inr(change_price) or change_price,
            "Latest Price": format_inr(latest_price) or latest_price,
            "Difference": format_inr(change_price - previous_price) or (change_price - previous_price),
            "7-Scrape Start Price": format_inr(first_price) or first_price,
            "Normalized SKU": sku or "-",
            "EAN(s)": latest_card.get("ean") or "-",
            "Changed On": change_record.get("scrape_date") or "-",
            "Latest Scrape": latest_record.get("scrape_date") or "-",
            "_site": site,
            "_history_only": True,
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
st.info(
    "Check which products changed price within the last 7 scrapes, or search for a specific product using the search bar."
)
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
    table_rows = sorted(
        _changed_price_rows(products, histories, window=7)
        + _history_only_changed_rows(histories, window=7),
        key=lambda item: str(item["Changed On"]),
        reverse=True,
    )
    st.subheader("Products with changed prices in last 7 scrapes")
    try:
        import pandas as pd
        display_rows = [{key: value for key, value in item.items() if not key.startswith("_")} for item in table_rows]
        event = st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True, on_select="rerun", selection_mode="single-row")
        selected_rows = event.selection.rows if event else []
        if selected_rows:
            change = table_rows[selected_rows[0]]
            sku = change["Normalized SKU"]
            if not change.get("_history_only") and sku in products:
                selected = (sku, products[sku])
                st.session_state["time_series_selected_site"] = change["_site"]
            else:
                st.info("This changed listing is not linked to a normalized product yet, so only the table row is available.")
    except TypeError:
        st.dataframe([{key: value for key, value in item.items() if not key.startswith("_")} for item in table_rows], use_container_width=True, hide_index=True)
        normalized_choices = [row["Normalized SKU"] for row in table_rows if not row.get("_history_only") and row["Normalized SKU"] in products]
        if normalized_choices:
            sku = st.selectbox("Choose a product", normalized_choices)
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
