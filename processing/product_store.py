"""Persistent product identity and price-tracking storage.

This module deliberately sits beside (rather than inside) the matcher.  Scrapes
may update price data every day without invoking CLIP, FAISS, or matching.
"""
from __future__ import annotations

from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import re

from .json_store import load_json, product_list, save_json_atomic
from .platform_paths import (
    CANONICAL_MAPPING,
    FINAL_TUPLES,
    IDENTIFIER_LOOKUP,
    LATEST_PRICES,
    PRICE_HISTORY,
    UNMATCHED_PRODUCTS,
    current_json_path,
    dated_json_path,
    latest_json_path,
)
from .product_schema import MARKETPLACES, normalize_sku, price_value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _raw_product(product: dict[str, Any]) -> dict[str, Any]:
    """Return the source payload retained by normalized product records."""
    raw = product.get("raw")
    return raw if isinstance(raw, dict) else {}


def _field(product: dict[str, Any], *names: str) -> Any:
    """Read a product field without losing values retained under ``raw``."""
    raw = _raw_product(product)
    for name in names:
        value = product.get(name)
        if value not in (None, ""):
            return value
        value = raw.get(name)
        if value not in (None, ""):
            return value
    return None


def _asin_from_url(value: Any) -> str | None:
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", str(value or ""), re.I)
    return match.group(1).upper() if match else None


def source_product_id(source: str, product: dict[str, Any]) -> str | None:
    """Return the stable, source-owned identity without title-based fallbacks."""
    # An ASIN is Amazon's actual source identity; do not replace it with EAN.
    if source == "amazon":
        url_asin = _asin_from_url(_field(product, "url", "link", "amazon_url"))
        asin = _text(_field(product, "asin"))
        if url_asin or asin:
            return url_asin or asin
        value = _text(_field(product, "source_product_id"))
        # V3 search result rows carry the Columbia input product ID in
        # source_product_id. Do not use that as Amazon's listing identity.
        if value and not value.startswith("gid://") and not (value.isdigit() and len(value) not in {12, 13}):
            return value
        return _text(_field(product, "ean", "upc"))
    for field in ("source_product_id", "product_id", "productId", "id", "code", "sellerSku", "sku"):
        value = _text(_field(product, field))
        if value:
            return value
    return None


def source_key(source: str, product_id: str | None) -> str | None:
    return f"{source}:{product_id}" if source and product_id else None


def primary_price(source: str, normal_price: Any, offer_price: Any) -> Any:
    # AJIO's special price is the current selling price when the API exposes it.
    return offer_price if source == "ajio" and offer_price not in (None, "") else normal_price


def normalize_product(source: str, product: dict[str, Any], scraped_at: str | None = None) -> dict[str, Any]:
    source = source.lower().strip()
    normal_price = _field(product, "normal_price", "price")
    offer_price = _field(product, "offer_price", "special_price")
    image = _field(product, "image", "image_url")
    images = _field(product, "image_urls", "images") or ([image] if image else [])
    if isinstance(images, str):
        images = [images]
    result = {
        "source": source,
        "source_product_id": source_product_id(source, product),
        "product_id": _text(_field(product, "product_id", "productId", "id")),
        "sku": normalize_sku(_field(product, "sku")),
        "asin": _text(_field(product, "asin")) or (_asin_from_url(_field(product, "url", "link")) if source == "amazon" else None),
        "ean": _text(_field(product, "ean", "upc")),
        "title": _text(_field(product, "title", "name")),
        "url": _text(_field(product, "url", "link", "amazon_url")),
        "image_url": _text(image),
        "image_urls": [str(value) for value in images if value],
        "normal_price": normal_price,
        "offer_price": offer_price,
        "price": primary_price(source, normal_price, offer_price),
        "normal_price_value": price_value(normal_price),
        "offer_price_value": price_value(offer_price),
        "price_value": price_value(primary_price(source, normal_price, offer_price)),
        "availability": _field(product, "availability", "available"),
        "scraped_at": scraped_at or _field(product, "scraped_at") or datetime.now().isoformat(timespec="seconds"),
        # Preserve source-specific fields for future debugging/migrations.
        "raw": product,
    }
    return result


def card_from_product(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_product_id": product.get("source_product_id"),
        "product_id": product.get("product_id"),
        "sku": product.get("sku"),
        "asin": product.get("asin"),
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


def canonical_rows(payload: dict[str, Any] | None = None) -> dict[str, tuple[str, dict[str, Any]]]:
    """Index tuple rows by their internal canonical identity.

    Older files used EAN as the JSON key.  The returned original key keeps
    them readable while the rest of the application uses canonical IDs.
    """
    if payload is None:
        payload = load_json(FINAL_TUPLES, {"products": {}})
    products = payload.get("products", {}) if isinstance(payload, dict) else {}
    rows: dict[str, tuple[str, dict[str, Any]]] = {}
    if not isinstance(products, dict):
        return rows
    for key, row in products.items():
        if not isinstance(row, dict):
            continue
        canonical_id = _text(row.get("canonical_product_id"))
        if canonical_id:
            rows[canonical_id] = (str(key), row)
    return rows


def _normal_identifier(value: Any) -> str | None:
    value = _text(value)
    if not value:
        return None
    return " ".join(value.casefold().split())


def tuple_identifiers(row: dict[str, Any]) -> dict[str, list[str]]:
    """Return all exact identifiers retained by one canonical tuple."""
    values: dict[str, set[str]] = {
        "canonical_product_id": set(), "ean": set(), "sku": set(),
        "asin": set(), "source_product_id": set(), "product_id": set(),
        "title": set(),
    }
    canonical_id = _text(row.get("canonical_product_id"))
    if canonical_id:
        values["canonical_product_id"].add(canonical_id)
    columbia_sku = _text(row.get("columbia_sku"))
    if columbia_sku:
        values["sku"].add(columbia_sku)
        normalized_sku = normalize_sku(columbia_sku)
        if normalized_sku:
            values["sku"].add(normalized_sku)
    for root_name, target_name in (("EAN", "ean"), ("ean", "ean")):
        value = _text(row.get(root_name))
        if value:
            values[target_name].add(value)
    for source in MARKETPLACES:
        card = row.get(source)
        if not isinstance(card, dict):
            continue
        for field in ("ean", "sku", "asin", "source_product_id", "product_id", "title"):
            value = _text(card.get(field))
            if value:
                values[field].add(value)
                if field == "sku":
                    normalized_sku = normalize_sku(value)
                    if normalized_sku:
                        values[field].add(normalized_sku)
    return {name: sorted(items) for name, items in values.items() if items}


def apply_tuple_identifiers(row: dict[str, Any]) -> None:
    identifiers = tuple_identifiers(row)
    row["identifiers"] = identifiers
    eans = identifiers.get("ean", [])
    row["EAN"] = eans[0] if eans else None
    columbia = row.get("columbia")
    if isinstance(columbia, dict):
        row["columbia_sku"] = normalize_sku(columbia.get("sku"))
        row["columbia_product_id"] = columbia.get("source_product_id") or columbia.get("product_id")


def normalize_tuple_skus(payload: dict[str, Any]) -> None:
    """Apply SKU normalization to imported legacy tuple cards in memory."""
    products = payload.get("products", {}) if isinstance(payload, dict) else {}
    if not isinstance(products, dict):
        return
    for row in products.values():
        if not isinstance(row, dict):
            continue
        for source in MARKETPLACES:
            card = row.get(source)
            if isinstance(card, dict) and card.get("sku") not in (None, ""):
                card["sku"] = normalize_sku(card["sku"])
        apply_tuple_identifiers(row)


def build_identifier_lookup(payload: dict[str, Any] | None = None, *, write: bool = True) -> dict[str, Any]:
    """Build the small exact-identifier -> canonical-product lookup artifact."""
    if payload is None:
        payload = load_json(FINAL_TUPLES, {"products": {}})
    records: dict[str, dict[str, Any]] = {}
    for canonical_id, (_, row) in canonical_rows(payload).items():
        identifiers = tuple_identifiers(row)
        for identifier_type, values in identifiers.items():
            for value in values:
                normalized = _normal_identifier(value)
                if not normalized:
                    continue
                key = f"{identifier_type}:{normalized}"
                record = records.setdefault(key, {
                    "identifier_type": identifier_type,
                    "identifier_value": value,
                    "canonical_product_ids": [],
                })
                if canonical_id not in record["canonical_product_ids"]:
                    record["canonical_product_ids"].append(canonical_id)
    for record in records.values():
        record["canonical_product_ids"].sort()
    result = {
        "schema_version": 1,
        "kind": "identifier_lookup",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "records": records,
    }
    if write:
        save_json_atomic(IDENTIFIER_LOOKUP, result)
    return result


def resolve_tuples(query: str, payload: dict[str, Any] | None = None, limit: int = 30) -> list[tuple[str, dict[str, Any]]]:
    """Resolve any supported ID exactly, then fall back to title substring/fuzzy search."""
    normalized = _normal_identifier(normalize_sku(query) or query)
    if not normalized:
        return []
    payload = payload or load_json(FINAL_TUPLES, {"products": {}})
    rows = canonical_rows(payload)
    lookup = build_identifier_lookup(payload, write=False).get("records", {})
    exact_ids: list[str] = []
    if isinstance(lookup, dict):
        for record in lookup.values():
            if not isinstance(record, dict) or record.get("identifier_value") is None:
                continue
            if _normal_identifier(record.get("identifier_value")) == normalized:
                exact_ids.extend(str(value) for value in record.get("canonical_product_ids", []))
    resolved: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for canonical_id in exact_ids:
        found = rows.get(canonical_id)
        if found and canonical_id not in seen:
            resolved.append((canonical_id, found[1]))
            seen.add(canonical_id)
    if resolved:
        return resolved[:limit]

    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for canonical_id, (_, row) in rows.items():
        titles = tuple_identifiers(row).get("title", [])
        best = 0.0
        for title in titles:
            candidate = _normal_identifier(title) or ""
            if normalized in candidate:
                best = max(best, 1.0)
            else:
                best = max(best, SequenceMatcher(None, normalized, candidate).ratio())
        if best >= 0.45:
            ranked.append((best, canonical_id, row))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [(canonical_id, row) for _, canonical_id, row in ranked[:limit]]


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
        row_ean = _text(row.get("EAN")) or (str(ean) if not str(ean).startswith("canonical:") else None)
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
                if source == "amazon" and re.fullmatch(r"[A-Z0-9]{10}", str(product_id), re.I):
                    card["asin"] = str(product_id).upper()
                existing = records.get(source_key(source, product_id) or "", {})
                if isinstance(existing, dict) and existing.get("canonical_product_id"):
                    known_ids.add(str(existing["canonical_product_id"]))
        columbia_card = row.get("columbia") if isinstance(row.get("columbia"), dict) else None
        columbia_id = source_product_id("columbia", columbia_card) if columbia_card else None
        canonical_id = row.get("canonical_product_id") or (sorted(known_ids)[0] if known_ids else (f"canonical:columbia:{columbia_id}" if columbia_id else f"canonical:legacy:{ean}"))
        row["canonical_product_id"] = canonical_id
        row["EAN"] = row_ean
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
                "ean": card.get("ean") or row_ean,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "product": normalized or card,
            }
        apply_tuple_identifiers(row)
    mapping["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if write:
        save_json_atomic(CANONICAL_MAPPING, mapping)
        build_identifier_lookup(payload, write=True)
    return mapping


def ensure_final_tuple_identity() -> dict[str, Any]:
    """One-time backward-compatible migration for pre-canonical tuple files."""
    payload = load_json(FINAL_TUPLES, {"products": {}})
    products = payload.get("products", {}) if isinstance(payload, dict) else {}
    if isinstance(products, dict) and any(isinstance(row, dict) and not row.get("canonical_product_id") for row in products.values()):
        sync_canonical_mapping(payload, write=True)
        save_json_atomic(FINAL_TUPLES, payload)
    elif isinstance(products, dict):
        # Keep lightweight lookup data current for imported older artifacts.
        build_identifier_lookup(payload, write=True)
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


def backfill_price_canonical_ids(payload: dict[str, Any] | None = None, *, write: bool = True) -> dict[str, int]:
    """Attach pre-existing daily price records after canonical tuples are rebuilt.

    Price records are intentionally joined by source + source_product_id.  This
    lets a Columbia tuple created today retain its earlier daily observations.
    """
    payload = payload or load_json(FINAL_TUPLES, {"products": {}})
    mapping = sync_canonical_mapping(payload, write=False).get("records", {})
    changed = {"latest_prices": 0, "price_history": 0}
    for path, label in ((LATEST_PRICES, "latest_prices"), (PRICE_HISTORY, "price_history")):
        store = _store_records(path, label)
        for record in store["records"].values():
            if not isinstance(record, dict):
                continue
            key = source_key(str(record.get("source") or ""), _text(record.get("source_product_id")))
            mapping_row = mapping.get(key or "") if isinstance(mapping, dict) else None
            canonical_id = mapping_row.get("canonical_product_id") if isinstance(mapping_row, dict) else None
            if canonical_id and record.get("canonical_product_id") != canonical_id:
                record["canonical_product_id"] = canonical_id
                changed[label] += 1
        if write and changed[label]:
            store["updated_at"] = datetime.now().isoformat(timespec="seconds")
            save_json_atomic(path, store)
    return changed


def migrate_legacy_source_ids(*, write: bool = True) -> dict[str, int]:
    """Repair legacy price-store keys that used an Amazon UPC instead of ASIN."""
    changed = {"latest_prices": 0, "price_history": 0}
    for path, label in ((LATEST_PRICES, "latest_prices"), (PRICE_HISTORY, "price_history")):
        store = _store_records(path, label)
        replacements: list[tuple[str, str, dict[str, Any]]] = []
        for old_key, record in list(store["records"].items()):
            if not isinstance(record, dict):
                continue
            source = str(record.get("source") or "")
            corrected_id = source_product_id(source, record)
            if not corrected_id or corrected_id == record.get("source_product_id"):
                continue
            record["source_product_id"] = corrected_id
            new_key = source_key(source, corrected_id) or old_key
            if label == "price_history":
                new_key = f"{new_key}:{record.get('scrape_date')}"
            replacements.append((old_key, new_key, record))
        for old_key, new_key, record in replacements:
            store["records"].pop(old_key, None)
            store["records"][new_key] = record
            changed[label] += 1
        if write and replacements:
            store["updated_at"] = datetime.now().isoformat(timespec="seconds")
            save_json_atomic(path, store)
    return changed


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
                # Older latest-price records may not include a SKU. Preserve the
                # canonical card value instead of replacing it with ``None``.
                refreshed_card = {key: value for key, value in card_from_product(record).items() if value is not None}
                row[source] = {**card, **refreshed_card}
                row.setdefault("status", {}).setdefault(source, {})["available"] = record.get("availability") is not False
    normalize_tuple_skus(payload)
    return payload


def latest_price_timestamp() -> str | None:
    value = _store_records(LATEST_PRICES, "latest_prices").get("updated_at")
    return str(value) if value else None


def price_history_for(canonical_product_id: str) -> list[dict[str, Any]]:
    records = _store_records(PRICE_HISTORY, "price_history")["records"].values()
    return sorted(
        [record for record in records if record.get("canonical_product_id") == canonical_product_id],
        key=lambda record: (str(record.get("scrape_date")), str(record.get("source"))),
    )


def all_price_history_by_tuple() -> dict[str, list[dict[str, Any]]]:
    """Read price history once and group it for catalog-wide change views."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in _store_records(PRICE_HISTORY, "price_history")["records"].values():
        if not isinstance(record, dict) or not record.get("canonical_product_id"):
            continue
        grouped.setdefault(str(record["canonical_product_id"]), []).append(record)
    for records in grouped.values():
        records.sort(key=lambda record: (str(record.get("source")), str(record.get("scrape_date"))))
    return grouped
