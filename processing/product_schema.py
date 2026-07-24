from __future__ import annotations

from typing import Any
import re


MARKETPLACES = ("amazon", "ajio", "columbia", "adventuras", "myntra", "tatacliq")


def availability_display(site: str, card: dict | None, value: Any = "NA") -> Any:
    """Return the marketplace value with the agreed availability semantics.

    Only Columbia and Adventuras explicitly report stock state in the source
    data.  For those sites, an explicit ``false`` means out of stock.  A
    missing card (AJIO, Myntra, TataCliq, or any other marketplace) means the
    product was not found/matched and must remain ``NA``; it is not evidence of
    being out of stock.  Amazon records are evidence that the product exists,
    even when its price is unavailable.
    """
    normalized_site = "adventuras" if site == "adventure" else site
    if (
        normalized_site in {"columbia", "adventuras"}
        and isinstance(card, dict)
        and card.get("availability", card.get("available")) is False
    ):
        return "OOS"
    return "NA" if value in (None, "") else value


def normalize_sku(value: Any) -> str | None:
    """Remove size/fit suffixes from Columbia-style product SKU values.

    The first two hyphen-separated parts identify the style and colour. Later
    parts are sellable variants, such as ``S``, ``O/S``, ``UK-7`` or ``2``.
    """
    if value is None:
        return None
    sku = str(value).strip().upper()
    if not sku:
        return None
    sku = re.sub(r"[\u2010-\u2015\u2212]", "-", sku)
    sku = re.sub(r"\s*-\s*", "-", sku)
    parts = sku.split("-")
    if len(parts) >= 3 and all(re.fullmatch(r"[A-Z0-9]+", part or "") for part in parts[:2]):
        return "-".join(parts[:2])
    return sku


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
    return f"₹{value:,.2f}" if value is not None else None


def _first_value(product: dict, *fields: str):
    """Read a field from normalized products and their preserved raw payload."""
    raw = product.get("raw") if isinstance(product.get("raw"), dict) else {}
    for field in fields:
        value = product.get(field)
        if value not in (None, ""):
            return value
        value = raw.get(field)
        if value not in (None, ""):
            return value
    return None


def product_card(product: dict | None) -> dict | None:
    if not isinstance(product, dict):
        return None
    source_product_id = _first_value(product, "source_product_id", "product_id", "productId", "id", "asin", "sku")
    return {
        "source_product_id": str(source_product_id) if source_product_id is not None else None,
        "product_id": _first_value(product, "product_id", "productId", "id"),
        "sku": _first_value(product, "sku"),
        "asin": _first_value(product, "asin"),
        "ean": _first_value(product, "ean", "upc"),
        "title": _first_value(product, "title", "name"),
        "image": _first_value(product, "image", "image_url"),
        "url": _first_value(product, "url", "link"),
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
