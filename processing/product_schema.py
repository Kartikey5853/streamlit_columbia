from __future__ import annotations

from typing import Any


MARKETPLACES = ("amazon", "ajio", "columbia", "adventuras", "myntra", "tatacliq")


def price_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    digits = []
    seen_digit = False
    for ch in str(value):
        if ch.isdigit():
            digits.append(ch)
            seen_digit = True
        elif seen_digit and ch in {",", "."}:
            digits.append(ch)
        elif seen_digit:
            break
    raw = "".join(digits).replace(",", "")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def format_inr(value: float | None) -> str | None:
    return f"Rs. {value:,.0f}" if value is not None else None


def product_card(product: dict | None) -> dict | None:
    if not isinstance(product, dict):
        return None
    source_product_id = next((product.get(key) for key in ("source_product_id", "product_id", "productId", "id", "asin", "sku") if product.get(key)), None)
    return {
        "source_product_id": str(source_product_id) if source_product_id is not None else None,
        "ean": product.get("ean") or product.get("upc"),
        "title": product.get("title") or product.get("name"),
        "image": product.get("image") or product.get("image_url"),
        "url": product.get("url") or product.get("link"),
        "price": product.get("offer_price") if product.get("offer_price") not in (None, "") and product.get("source") == "ajio" else product.get("price"),
        "price_value": product.get("price_value"),
        "normal_price": product.get("normal_price", product.get("price")),
        "normal_price_value": product.get("normal_price_value", product.get("price_value")),
        "offer_price": product.get("offer_price"),
        "offer_price_value": product.get("offer_price_value"),
        "availability": product.get("availability", product.get("available")),
        "scraped_at": product.get("scraped_at"),
    }


def empty_tuple(ean: str) -> dict:
    row = {"EAN": ean}
    for site in MARKETPLACES:
        row[site] = None
    return row
