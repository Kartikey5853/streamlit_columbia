"""Persistent product identity and price-tracking storage.

This module deliberately sits beside (rather than inside) the matcher.  Scrapes
may update price data every day without invoking CLIP, FAISS, or matching.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import re

from .json_store import load_json, product_list, save_json_atomic
from .platform_paths import (
    CANONICAL_MAPPING,
    FINAL_TUPLES,
    LATEST_PRICES,
    PRICE_HISTORY,
    UNMATCHED_PRODUCTS,
    current_json_path,
    dated_json_path,
    latest_json_path,
)
from .product_schema import MARKETPLACES, price_value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _asin_from_url(value: Any) -> str | None:
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", str(value or ""), re.I)
    return match.group(1).upper() if match else None


def source_product_id(source: str, product: dict[str, Any]) -> str | None:
    """Return the stable, source-owned identity without title-based fallbacks."""
    for field in ("source_product_id", "product_id", "productId", "id", "asin", "sku"):
        value = _text(product.get(field))
        if value:
            return value
    if source == "amazon":
        return _asin_from_url(product.get("url") or product.get("link")) or _text(product.get("ean") or product.get("upc"))
    return None


def source_key(source: str, product_id: str | None) -> str | None:
    return f"{source}:{product_id}" if source and product_id else None


def primary_price(source: str, normal_price: Any, offer_price: Any) -> Any:
    # AJIO's special price is the current selling price when the API exposes it.
    return offer_price if source == "ajio" and offer_price not in (None, "") else normal_price


def normalize_product(source: str, product: dict[str, Any], scraped_at: str | None = None) -> dict[str, Any]:
    source = source.lower().strip()
    normal_price = product.get("normal_price", product.get("price"))
    offer_price = product.get("offer_price", product.get("special_price"))
    image = product.get("image") or product.get("image_url")
    images = product.get("image_urls") or product.get("images") or ([image] if image else [])
    if isinstance(images, str):
        images = [images]
    result = {
        "source": source,
        "source_product_id": source_product_id(source, product),
        "ean": _text(product.get("ean") or product.get("upc")),
        "title": _text(product.get("title") or product.get("name")),
        "url": _text(product.get("url") or product.get("link")),
        "image_url": _text(image),
        "image_urls": [str(value) for value in images if value],
        "normal_price": normal_price,
        "offer_price": offer_price,
        "price": primary_price(source, normal_price, offer_price),
        "normal_price_value": price_value(normal_price),
        "offer_price_value": price_value(offer_price),
        "price_value": price_value(primary_price(source, normal_price, offer_price)),
        "availability": product.get("availability", product.get("available")),
        "scraped_at": scraped_at or product.get("scraped_at") or datetime.now().isoformat(timespec="seconds"),
        # Preserve source-specific fields for future debugging/migrations.
        "raw": product,
    }
    return result


def card_from_product(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_product_id": product.get("source_product_id"),
        "ean": product.get("ean"),
        "title": product.get("title"),
        "image": product.get("image_url"),
        "image_urls": product.get("image_urls") or [],
        "url": product.get("url"),
        "price": product.get("price"),
        "price_value": product.get("price_value"),
        "normal_price": product.get("normal_price"),
        "normal_price_value": product.get("normal_price_value"),
        "offer_price": product.get("offer_price"),
        "offer_price_value": product.get("offer_price_value"),
        "availability": product.get("availability"),
        "scraped_at": product.get("scraped_at"),
    }


def _store_records(path: Path, kind: str) -> dict[str, Any]:
    payload = load_json(path, {"schema_version": 1, "records": {}})
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("schema_version", 1)
    payload.setdefault("kind", kind)
    payload.setdefault("records", {})
    if not isinstance(payload["records"], dict):
        payload["records"] = {}
    return payload


def _all_normalized_sources() -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for source in MARKETPLACES:
        for raw in product_list(load_json(current_json_path(source), {})):
            product = normalize_product(source, raw)
            key = source_key(source, product.get("source_product_id"))
            if key:
                records[key] = product
    return records


def sync_canonical_mapping(payload: dict[str, Any], *, write: bool = True) -> dict[str, Any]:
    """Backfill stable source IDs into tuples and persist source -> canonical IDs."""
    mapping = _store_records(CANONICAL_MAPPING, "canonical_product_mapping")
    records = mapping["records"]
    sources = _all_normalized_sources()
    products = payload.get("products", {}) if isinstance(payload, dict) else {}
    if not isinstance(products, dict):
        return mapping

    for ean, row in products.items():
        if not isinstance(row, dict):
            continue
        known_ids: set[str] = set()
        for source in MARKETPLACES:
            card = row.get(source)
            if not isinstance(card, dict):
                continue
            product_id = source_product_id(source, card)
            if not product_id:
                for candidate in sources.values():
                    if candidate["source"] != source:
                        continue
                    if card.get("url") and card.get("url") == candidate.get("url"):
                        product_id = candidate.get("source_product_id")
                        break
                    if source == "amazon" and str(candidate.get("ean") or "") == str(ean):
                        product_id = candidate.get("source_product_id")
                        break
            if product_id:
                card["source_product_id"] = product_id
                existing = records.get(source_key(source, product_id) or "", {})
                if isinstance(existing, dict) and existing.get("canonical_product_id"):
                    known_ids.add(str(existing["canonical_product_id"]))
        canonical_id = row.get("canonical_product_id") or (sorted(known_ids)[0] if known_ids else f"canonical:{ean}")
        row["canonical_product_id"] = canonical_id
        row["EAN"] = row.get("EAN") or str(ean)
        for source in MARKETPLACES:
            card = row.get(source)
            if not isinstance(card, dict):
                continue
            product_id = card.get("source_product_id")
            key = source_key(source, _text(product_id))
            if not key:
                continue
            normalized = sources.get(key)
            records[key] = {
                "canonical_product_id": canonical_id,
                "source": source,
                "source_product_id": str(product_id),
                "ean": card.get("ean") or str(ean),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "product": normalized or card,
            }
    mapping["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if write:
        save_json_atomic(CANONICAL_MAPPING, mapping)
    return mapping


def ensure_final_tuple_identity() -> dict[str, Any]:
    """One-time backward-compatible migration for pre-canonical tuple files."""
    payload = load_json(FINAL_TUPLES, {"products": {}})
    products = payload.get("products", {}) if isinstance(payload, dict) else {}
    if isinstance(products, dict) and any(isinstance(row, dict) and not row.get("canonical_product_id") for row in products.values()):
        sync_canonical_mapping(payload, write=True)
        save_json_atomic(FINAL_TUPLES, payload)
    # Import legacy inline history once into the consolidated long-format file.
    history_store = _store_records(PRICE_HISTORY, "price_history")
    migrated = False
    if isinstance(products, dict):
        for ean, row in products.items():
            if not isinstance(row, dict) or not row.get("canonical_product_id"):
                continue
            legacy_history = row.get("history", {})
            for source, entries in legacy_history.items() if isinstance(legacy_history, dict) else []:
                card = row.get(source)
                product_id = card.get("source_product_id") if isinstance(card, dict) else None
                if source not in MARKETPLACES or not product_id or not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict) or not entry.get("date"):
                        continue
                    key = f"{source}:{product_id}:{entry['date']}"
                    if key in history_store["records"]:
                        continue
                    history_store["records"][key] = {
                        "canonical_product_id": row["canonical_product_id"],
                        "source": source,
                        "source_product_id": product_id,
                        "ean": str(ean),
                        "scrape_date": entry["date"],
                        "normal_price": entry.get("price"),
                        "normal_price_value": price_value(entry.get("price")),
                        "offer_price": None,
                        "offer_price_value": None,
                        "availability": entry.get("availability"),
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    migrated = True
    if migrated:
        history_store["updated_at"] = datetime.now().isoformat(timespec="seconds")
        save_json_atomic(PRICE_HISTORY, history_store)
    return payload


def ingest_scrape(source: str, raw_payload: Any, scrape_date: str | None = None) -> dict[str, Any]:
    """Normalize a completed scrape, persist it, and update prices without rematching."""
    scrape_date = scrape_date or datetime.now().date().isoformat()
    normalized = [normalize_product(source, product) for product in product_list(raw_payload)]
    payload = {
        "schema_version": 3,
        "source": source,
        "scrape_date": scrape_date,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "products": normalized,
    }
    save_json_atomic(latest_json_path(source), payload)
    save_json_atomic(dated_json_path(source, scrape_date), payload)

    tuples = load_json(FINAL_TUPLES, {"products": {}})
    mapping = sync_canonical_mapping(tuples, write=True)
    mapping_records = mapping["records"]
    latest = _store_records(LATEST_PRICES, "latest_prices")
    history = _store_records(PRICE_HISTORY, "price_history")
    unmatched = _store_records(UNMATCHED_PRODUCTS, "unmatched_products")
    tuple_changed = False
    known = 0

    rows_by_canonical = {
        str(row.get("canonical_product_id")): row
        for row in tuples.get("products", {}).values()
        if isinstance(row, dict) and row.get("canonical_product_id")
    }
    for product in normalized:
        key = source_key(source, product.get("source_product_id"))
        if not key:
            continue
        mapping_row = mapping_records.get(key, {})
        canonical_id = mapping_row.get("canonical_product_id") if isinstance(mapping_row, dict) else None
        latest["records"][key] = {**product, "canonical_product_id": canonical_id, "scrape_date": scrape_date}
        history_key = f"{key}:{scrape_date}"
        history["records"][history_key] = {
            "canonical_product_id": canonical_id,
            "source": source,
            "source_product_id": product["source_product_id"],
            "ean": product.get("ean"),
            "scrape_date": scrape_date,
            "normal_price": product.get("normal_price"),
            "normal_price_value": product.get("normal_price_value"),
            "offer_price": product.get("offer_price"),
            "offer_price_value": product.get("offer_price_value"),
            "availability": product.get("availability"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        if canonical_id and canonical_id in rows_by_canonical:
            row = rows_by_canonical[canonical_id]
            row[source] = card_from_product(product)
            row.setdefault("status", {}).setdefault(source, {})["available"] = product.get("availability") is not False
            row["status"][source]["last_seen"] = scrape_date
            known += 1
            tuple_changed = True
            unmatched["records"].pop(key, None)
        else:
            unmatched["records"][key] = {**product, "first_seen": unmatched["records"].get(key, {}).get("first_seen", scrape_date), "last_seen": scrape_date}

    now = datetime.now().isoformat(timespec="seconds")
    for store in (latest, history, unmatched):
        store["updated_at"] = now
    save_json_atomic(LATEST_PRICES, latest)
    save_json_atomic(PRICE_HISTORY, history)
    save_json_atomic(UNMATCHED_PRODUCTS, unmatched)
    if tuple_changed:
        tuples["updated_at"] = now
        save_json_atomic(FINAL_TUPLES, tuples)
    return {"source": source, "products": len(normalized), "known_products": known, "unmatched_products": len(normalized) - known}


def tuples_with_latest_prices(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or ensure_final_tuple_identity()
    products = payload.get("products", {}) if isinstance(payload, dict) else {}
    if isinstance(products, dict) and any(isinstance(row, dict) and not row.get("canonical_product_id") for row in products.values()):
        sync_canonical_mapping(payload, write=True)
        save_json_atomic(FINAL_TUPLES, payload)
    latest = _store_records(LATEST_PRICES, "latest_prices")["records"]
    for row in payload.get("products", {}).values():
        if not isinstance(row, dict):
            continue
        for source in MARKETPLACES:
            card = row.get(source)
            if not isinstance(card, dict):
                continue
            record = latest.get(source_key(source, _text(card.get("source_product_id"))) or "")
            if isinstance(record, dict):
                row[source] = {**card, **card_from_product(record)}
    return payload


def price_history_for(canonical_product_id: str) -> list[dict[str, Any]]:
    records = _store_records(PRICE_HISTORY, "price_history")["records"].values()
    return sorted(
        [record for record in records if record.get("canonical_product_id") == canonical_product_id],
        key=lambda record: (str(record.get("scrape_date")), str(record.get("source"))),
    )
