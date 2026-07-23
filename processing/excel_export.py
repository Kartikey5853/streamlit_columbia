from __future__ import annotations

from io import BytesIO

from .product_schema import MARKETPLACES, format_inr, normalize_sku, price_value


SITE_LABELS = {
    "amazon": "Amazon", "ajio": "AJIO", "columbia": "Columbia",
    "adventuras": "Adventuras", "myntra": "Myntra", "tatacliq": "TataCliQ",
}


def _value(card: dict | None, field: str):
    if not isinstance(card, dict):
        return None
    return card.get(field)


def _availability_value(row: dict, source: str, card: dict | None, value):
    status = row.get("status", {}).get(source, {}) if isinstance(row.get("status"), dict) else {}
    if isinstance(card, dict) and (card.get("availability") is False or status.get("available") is False):
        return "OOS"
    if not isinstance(card, dict):
        return "NA"
    return value if value not in (None, "") else "NA"


def tuple_export_rows(products: dict, options: dict[str, bool]) -> list[dict]:
    rows: list[dict] = []
    for ean, row in sorted(products.items()):
        item: dict = {}
        if options.get("identifiers", True):
            item["Canonical Product ID"] = row.get("canonical_product_id")
            item["EAN"] = row.get("EAN")
            item["Columbia SKU"] = normalize_sku(row.get("columbia_sku") or _value(row.get("columbia"), "sku"))
            item["Columbia Product ID"] = row.get("columbia_product_id") or _value(row.get("columbia"), "source_product_id")
        if options.get("source_ids", True):
            for source in MARKETPLACES:
                item[f"{SITE_LABELS[source]} Product ID"] = _value(row.get(source), "source_product_id")
                item[f"{SITE_LABELS[source]} SKU"] = normalize_sku(_value(row.get(source), "sku"))
        if options.get("prices", True):
            for source in MARKETPLACES:
                card = row.get(source)
                raw_price = _value(card, "normal_price") or _value(card, "price")
                item[f"{SITE_LABELS[source]} Price"] = _availability_value(row, source, card, format_inr(price_value(raw_price)))
        if options.get("special_prices", True):
            card = row.get("ajio")
            item["AJIO Special Price"] = _availability_value(row, "ajio", card, format_inr(price_value(_value(card, "offer_price"))))
        if options.get("titles", True):
            for source in MARKETPLACES:
                item[f"{SITE_LABELS[source]} Title"] = _value(row.get(source), "title")
        if options.get("urls", True):
            for source in MARKETPLACES:
                item[f"{SITE_LABELS[source]} URL"] = _value(row.get(source), "url")
        if options.get("image_urls", True):
            for source in MARKETPLACES:
                item[f"{SITE_LABELS[source]} Image URL"] = _value(row.get(source), "image")
        rows.append(item)
    return rows


def excel_bytes(rows: list[dict], sheet_name: str = "tuples") -> bytes:
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name[:31]
    headers = list(rows[0]) if rows else []
    sheet.append(headers or ["No data"])
    for row in rows:
        sheet.append([row.get(header) for header in headers])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
