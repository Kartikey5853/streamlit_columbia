from __future__ import annotations

from io import BytesIO

from .product_schema import MARKETPLACES


SITE_LABELS = {
    "amazon": "Amazon", "ajio": "AJIO", "columbia": "Columbia",
    "adventuras": "Adventuras", "myntra": "Myntra", "tatacliq": "TataCliQ",
}


def _value(card: dict | None, field: str):
    if not isinstance(card, dict):
        return None
    return card.get(field)


def tuple_export_rows(products: dict, options: dict[str, bool]) -> list[dict]:
    rows: list[dict] = []
    for ean, row in sorted(products.items()):
        item: dict = {}
        if options.get("identifiers", True):
            item["Canonical Product ID"] = row.get("canonical_product_id")
            item["EAN"] = row.get("EAN") or str(ean)
        if options.get("source_ids", True):
            for source in MARKETPLACES:
                item[f"{SITE_LABELS[source]} Product ID"] = _value(row.get(source), "source_product_id")
        if options.get("prices", True):
            for source in MARKETPLACES:
                item[f"{SITE_LABELS[source]} Price"] = _value(row.get(source), "normal_price") or _value(row.get(source), "price")
        if options.get("special_prices", True):
            item["AJIO Special Price"] = _value(row.get("ajio"), "offer_price")
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
