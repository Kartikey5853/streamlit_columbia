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
    return {
        "title": product.get("title") or product.get("name"),
        "image": product.get("image") or product.get("image_url"),
        "url": product.get("url") or product.get("link"),
        "price": product.get("price"),
    }


def empty_tuple(ean: str) -> dict:
    row = {"EAN": ean}
    for site in MARKETPLACES:
        row[site] = None
    return row
