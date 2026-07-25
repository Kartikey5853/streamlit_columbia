from __future__ import annotations

import argparse
import json
import logging
import os
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .json_store import load_json, product_list, save_json_atomic
from .platform_paths import (
    NORMALIZED_IDENTIFIER_LOOKUP,
    NORMALIZED_PRODUCTS,
    current_json_path,
    log_path,
    preferred_json_paths,
)
from .product_schema import availability_display, normalize_sku, price_value
from .product_store import normalize_product
from .structured_logging import get_scraper_logger, log_event
from .process_status import mark_started, mark_stopped, update_site_status


NORMALIZER_SITE = "normalized"
UNIFIED_SOURCES = {
    "columbia": "columbia",
    "amazon": "amazon",
    "ajio": "ajio",
    "adventure": "adventuras",
}


def _dedupe(values: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def _extract_eans(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]

    eans: list[str] = []
    for item in values:
        if isinstance(item, (list, tuple, set, dict)):
            eans.extend(_extract_eans(item))
            continue
        for match in re.findall(r"\d{12,13}", str(item or "")):
            eans.append(match)
    return _dedupe(eans)


def _normalized_sku(value: object) -> str | None:
    """Return the shared style/colour SKU: the first two dash segments.

    Columbia, AJIO, and Adventuras append sellable variants (size, fit, etc.)
    to the same product identifier.  A row in the unified catalogue therefore
    represents ``XXXXX-XXX``, never an individual size variant.
    """
    return normalize_sku(value)


def _raw_payload(raw: dict) -> dict:
    nested = raw.get("raw")
    return nested if isinstance(nested, dict) else {}


def _source_sku(raw: dict) -> str | None:
    """Extract the source SKU before any marketplace comparisons occur."""
    nested = _raw_payload(raw)
    # The top-level field wins when present; otherwise source data keeps it
    # in marketplace-specific fields such as ``raw.sku`` or ``sellerSku``.
    return _normalized_sku(
        raw.get("sku")
        or raw.get("sellerSku")
        or nested.get("sku")
        or nested.get("sellerSku")
    )


def _source_eans(raw: dict) -> list[str]:
    """Extract EAN-like identifiers into the shared normalized EAN field."""
    nested = _raw_payload(raw)
    values: list[object] = []
    for source in (raw, nested):
        values.extend([
            source.get("ean"),
            source.get("upc"),
            source.get("code"),
            source.get("barcode"),
            source.get("ean_numbers"),
            source.get("eans"),
            source.get("all_eans"),
            source.get("variant_mapping"),
        ])
    return _dedupe(ean for value in values for ean in _extract_eans(value))


def _source_product(source: str, raw: dict) -> dict:
    product = normalize_product(source, raw)
    image = product.get("image_url")
    original_raw = _raw_payload(raw) or raw
    # Identifiers are extracted first into this card.  The merge functions
    # below compare only card["sku"] / card["ean_numbers"], never raw JSON.
    sku = _source_sku(raw)
    eans = _source_eans(raw)
    result = {
        **product,
        "sku": sku,
        "ean": eans[0] if eans else product.get("ean"),
        "ean_numbers": eans,
        "image": image,
        "raw": original_raw,
    }
    if raw.get("amazon_url") and not result.get("url"):
        result["url"] = raw.get("amazon_url")
    if raw.get("status"):
        result["status"] = raw.get("status")
    if raw.get("match_method"):
        result["match_method"] = raw.get("match_method")
    return result


def parse_columbia(raw: dict) -> dict:
    return _source_product("columbia", raw)


def parse_amazon(raw: dict) -> dict:
    return _source_product("amazon", raw)


def parse_ajio(raw: dict) -> dict:
    return _source_product("ajio", raw)


def parse_adventure(raw: dict) -> dict:
    return _source_product("adventure", raw)


def _empty_record(sku: str, columbia_card: dict) -> dict:
    return {
        "sku": sku,
        "ean_numbers": list(columbia_card.get("ean_numbers") or []),
        "columbia": dict(columbia_card),
        "amazon": None,
        "ajio": None,
        "adventure": None,
        "myntra": None,
        "tatacliq": None,
    }


def _merge_card(existing: dict | None, incoming: dict) -> dict:
    if not isinstance(existing, dict):
        return dict(incoming)
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "ean_numbers":
            merged[key] = _dedupe([*(merged.get(key) or []), *value])
            continue
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged


def _load_source_products() -> dict[str, list[dict]]:
    products: dict[str, list[dict]] = {}
    for unified_source, source_name in UNIFIED_SOURCES.items():
        loaded: list[dict] = []
        for path in preferred_json_paths(source_name):
            loaded = product_list(load_json(path, {}))
            if loaded:
                break
        if not loaded:
            loaded = product_list(load_json(current_json_path(source_name), {}))
        products[unified_source] = loaded
    return products


def normalize_source_products(source_products: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Convert every marketplace input to normalized identifier cards once."""
    parsers = {
        "columbia": parse_columbia,
        "amazon": parse_amazon,
        "ajio": parse_ajio,
        "adventure": parse_adventure,
    }
    normalized: dict[str, list[dict]] = {}
    for source, products in source_products.items():
        if source not in parsers:
            continue
        cards = [parsers[source](raw) for raw in products if isinstance(raw, dict)]
        if source == "amazon":
            cards = [
                card for card in cards
                if card.get("url") and (card.get("title") or card.get("price") or card.get("price_value"))
            ]
        normalized[source] = cards
    return normalized


def _normalize_columbia_products(products: list[dict]) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for card in products:
        sku = card.get("sku")
        if not sku:
            continue
        row = records.get(sku)
        if row is None:
            records[sku] = _empty_record(sku, card)
            continue
        row["columbia"] = _merge_card(row.get("columbia"), card)
        row["ean_numbers"] = _dedupe([*row.get("ean_numbers", []), *card.get("ean_numbers", [])])
    return records


def _build_ean_lookup(records: dict[str, dict]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for sku, row in records.items():
        for ean in row.get("ean_numbers", []):
            if ean not in lookup:
                lookup[ean] = sku
    return lookup


def _attach_by_ean(
    records: dict[str, dict],
    products: list[dict],
    *,
    source: str,
    ean_lookup: dict[str, str],
) -> int:
    attached = 0
    for card in products:
        target_sku = None
        matched_ean = None
        for ean in card.get("ean_numbers", []):
            target_sku = ean_lookup.get(ean)
            if target_sku:
                matched_ean = ean
                break
        if not target_sku or target_sku not in records:
            continue
        row = records[target_sku]
        row[source] = _merge_card(row.get(source), card)
        row["ean_numbers"] = _dedupe([*row.get("ean_numbers", []), *card.get("ean_numbers", [])])
        if matched_ean and matched_ean not in row["ean_numbers"]:
            row["ean_numbers"].append(matched_ean)
        attached += 1
    return attached


def _attach_by_sku(records: dict[str, dict], products: list[dict], *, source: str) -> int:
    attached = 0
    for card in products:
        sku = card.get("sku")
        if not sku or sku not in records:
            continue
        row = records[sku]
        row[source] = _merge_card(row.get(source), card)
        row["ean_numbers"] = _dedupe([*row.get("ean_numbers", []), *card.get("ean_numbers", [])])
        attached += 1
    return attached


def _finalize_records(records: dict[str, dict]) -> dict[str, dict]:
    final: dict[str, dict] = {}
    for sku, row in records.items():
        final[sku] = {
            "sku": sku,
            "ean_numbers": _dedupe(row.get("ean_numbers", [])),
            "columbia": row.get("columbia") or None,
            "amazon": row.get("amazon") or None,
            "ajio": row.get("ajio") or None,
            "adventure": row.get("adventure") or None,
            "myntra": row.get("myntra") or None,
            "tatacliq": row.get("tatacliq") or None,
        }
    return final


def normalization_debug(
    normalized_sources: dict[str, list[dict]],
    records: dict[str, dict],
    ean_lookup: dict[str, str],
    *,
    example_limit: int = 20,
) -> dict:
    """Return auditable counts and exact-match examples from normalized cards."""
    columbia_skus = {card["sku"] for card in normalized_sources.get("columbia", []) if card.get("sku")}
    ajio_skus = {card["sku"] for card in normalized_sources.get("ajio", []) if card.get("sku")}
    adventure_skus = {card["sku"] for card in normalized_sources.get("adventure", []) if card.get("sku")}
    amazon_eans = {
        ean for card in normalized_sources.get("amazon", []) for ean in card.get("ean_numbers", [])
    }

    examples_by_source: dict[str, list[dict]] = {"ajio": [], "adventure": [], "amazon": []}
    sku_matches = 0
    for source in ("ajio", "adventure"):
        for card in normalized_sources.get(source, []):
            sku = card.get("sku")
            if not sku or sku not in records:
                continue
            sku_matches += 1
            examples_by_source[source].append({
                "match_type": "SKU", "source": source, "identifier": sku, "columbia_sku": sku,
            })

    ean_matches = 0
    for card in normalized_sources.get("amazon", []):
        matched_ean = next((ean for ean in card.get("ean_numbers", []) if ean in ean_lookup), None)
        if not matched_ean:
            continue
        ean_matches += 1
        examples_by_source["amazon"].append({
            "match_type": "EAN",
            "source": "amazon",
            "identifier": matched_ean,
            "columbia_sku": ean_lookup[matched_ean],
        })

    # Rotate sources so the debug preview demonstrates each available join,
    # instead of showing only the first marketplace with many matches.
    examples: list[dict] = []
    positions = {source: 0 for source in examples_by_source}
    while len(examples) < example_limit:
        added = False
        for source in ("ajio", "adventure", "amazon"):
            position = positions[source]
            candidates = examples_by_source[source]
            if position >= len(candidates):
                continue
            examples.append(candidates[position])
            positions[source] += 1
            added = True
            if len(examples) == example_limit:
                break
        if not added:
            break

    return {
        "columbia_normalized_skus": len(columbia_skus),
        "ajio_normalized_skus": len(ajio_skus),
        "adventure_normalized_skus": len(adventure_skus),
        "amazon_normalized_eans": len(amazon_eans),
        "sku_matches": sku_matches,
        "ean_matches": ean_matches,
        "example_matches": examples,
    }


def build_normalized_identifier_lookup(payload: dict | None = None, *, write: bool = True) -> dict:
    if payload is None:
        payload = load_json(NORMALIZED_PRODUCTS, {"products": {}})
    products = payload.get("products", {}) if isinstance(payload, dict) else {}
    records: dict[str, dict] = {}
    if isinstance(products, dict):
        for sku, row in products.items():
            if not isinstance(row, dict):
                continue
            normalized_sku = _normalized_sku(row.get("sku") or sku)
            if normalized_sku:
                records[f"sku:{normalized_sku}"] = {
                    "identifier_type": "sku",
                    "identifier_value": normalized_sku,
                    "sku": normalized_sku,
                }
            for ean in _extract_eans(row.get("ean_numbers")):
                records[f"ean:{ean}"] = {
                    "identifier_type": "ean",
                    "identifier_value": ean,
                    "sku": normalized_sku or str(sku),
                }
    result = {
        "schema_version": 1,
        "kind": "normalized_identifier_lookup",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "records": records,
    }
    if write:
        save_json_atomic(NORMALIZED_IDENTIFIER_LOOKUP, result)
    return result


def resolve_normalized_product(query: str, payload: dict | None = None) -> tuple[str, dict] | None:
    payload = payload or load_json(NORMALIZED_PRODUCTS, {"products": {}})
    products = payload.get("products", {}) if isinstance(payload, dict) else {}
    if not isinstance(products, dict) or not products:
        return None
    lookup = build_normalized_identifier_lookup(payload, write=False).get("records", {})
    normalized_sku = _normalized_sku(query)
    if normalized_sku:
        key = f"sku:{normalized_sku}"
        record = lookup.get(key)
        if isinstance(record, dict):
            sku = str(record.get("sku") or normalized_sku)
            row = products.get(sku)
            if isinstance(row, dict):
                return sku, row
    ean = "".join(ch for ch in str(query or "") if ch.isdigit())
    if len(ean) in {12, 13}:
        record = lookup.get(f"ean:{ean}")
        if isinstance(record, dict):
            sku = str(record.get("sku") or "")
            row = products.get(sku)
            if isinstance(row, dict):
                return sku, row
    return None


def build_normalized_products(
    output: Path = NORMALIZED_PRODUCTS,
    *,
    source_products: dict[str, list[dict]] | None = None,
    write_lookup: bool = True,
) -> dict:
    logger = get_scraper_logger(NORMALIZER_SITE, log_path(NORMALIZER_SITE))
    mark_started(NORMALIZER_SITE, os.getpid(), "Normalized product build starting")
    log_event(logger, logging.INFO, "PIPELINE", "START unified SKU/EAN normalization")
    started_total = datetime.now()

    try:
        source_products = source_products or _load_source_products()
        normalized_sources = normalize_source_products(source_products)
        columbia_products = normalized_sources.get("columbia", [])
        if not columbia_products:
            raise RuntimeError("Columbia catalog data is required to build the unified SKU/EAN dataset.")

        records = _normalize_columbia_products(columbia_products)
        ean_lookup = _build_ean_lookup(records)
        amazon_attached = _attach_by_ean(records, normalized_sources.get("amazon", []), source="amazon", ean_lookup=ean_lookup)
        ajio_attached = _attach_by_sku(records, normalized_sources.get("ajio", []), source="ajio")
        adventure_attached = _attach_by_sku(records, normalized_sources.get("adventure", []), source="adventure")
        debug = normalization_debug(normalized_sources, records, ean_lookup)

        finalized = _finalize_records(records)
        payload = {
            "schema_version": 2,
            "primary_key": "sku",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "rules": {
                "source_of_truth": "columbia",
                "matching": {
                    "amazon": "exact EAN match against Columbia EAN list",
                    "ajio": "exact SKU match against Columbia SKU",
                    "adventure": "exact SKU match against Columbia SKU",
                },
                "scope": ["columbia", "amazon", "ajio", "adventure", "myntra", "tatacliq"],
                "clip_enrichment": "Columbia images query Myntra/TataCliq candidates after exact tuple evidence is assembled",
            },
            "summary": {
                "columbia_products": len(columbia_products),
                "normalized_products": len(finalized),
                "amazon_products": len(normalized_sources.get("amazon", [])),
                "amazon_linked": amazon_attached,
                "ajio_products": len(normalized_sources.get("ajio", [])),
                "ajio_linked": ajio_attached,
                "adventure_products": len(normalized_sources.get("adventure", [])),
                "adventure_linked": adventure_attached,
                "unique_ean_values": len(_build_ean_lookup(finalized)),
            },
            "normalization_debug": debug,
            "products": finalized,
        }
        save_json_atomic(output, payload)
        build_normalized_identifier_lookup(payload, write=write_lookup)
        elapsed = (datetime.now() - started_total).total_seconds()
        log_event(
            logger,
            logging.INFO,
            "PIPELINE",
            (
                f"DONE unified normalization in {elapsed:.2f}s; "
                f"rows={payload['summary']['normalized_products']} "
                f"amazon_linked={amazon_attached} ajio_linked={ajio_attached} "
                f"adventure_linked={adventure_attached}"
            ),
        )
        log_event(
            logger,
            logging.INFO,
            "IDENTIFIERS",
            (
                f"Columbia normalized SKUs: {debug['columbia_normalized_skus']}; "
                f"AJIO normalized SKUs: {debug['ajio_normalized_skus']}; "
                f"Adventure normalized SKUs: {debug['adventure_normalized_skus']}; "
                f"Amazon normalized EANs: {debug['amazon_normalized_eans']}; "
                f"SKU matches: {debug['sku_matches']}; EAN matches: {debug['ean_matches']}"
            ),
        )
        update_site_status(NORMALIZER_SITE, {"message": "Unified SKU/EAN dataset ready", "success_count": 1})
        mark_stopped(NORMALIZER_SITE, "Unified SKU/EAN normalization complete")
        return payload
    except Exception as exc:
        trace = traceback.format_exc(limit=20)
        log_event(logger, logging.ERROR, "PIPELINE", f"ERROR {exc}")
        log_event(logger, logging.ERROR, "PIPELINE", f"TRACEBACK {trace}")
        update_site_status(NORMALIZER_SITE, {"failure_count": 1, "message": f"Failed: {exc}"})
        mark_stopped(NORMALIZER_SITE, f"Failed: {exc}")
        raise


def load_normalized_products(path: Path = NORMALIZED_PRODUCTS) -> dict:
    return load_json(path, {"schema_version": 1, "primary_key": "sku", "products": {}})


def flattened_rows(payload: dict | None = None) -> list[dict]:
    """Return the complete, reviewable unified tuple table.

    The viewer keeps only the review fields: product image, normalized SKU,
    EANs, marketplace prices, titles, and product-page links.
    """
    payload = payload or load_normalized_products()
    products = payload.get("products", {}) if isinstance(payload, dict) else {}
    if not isinstance(products, dict):
        return []

    def _card_value(site: str, card: dict | None, key: str) -> str:
        if not isinstance(card, dict):
            return "NA"
        value = card.get(key)
        return "NA" if value is None or str(value).strip() == "" else str(value)

    def _price_text(site: str, card: dict | None) -> str:
        if not isinstance(card, dict):
            return "NA"
        raw = card.get("price") or card.get("normal_price") or card.get("offer_price")
        parsed = price_value(raw)
        value = f"INR {parsed:,.2f}" if parsed is not None else raw
        return str(availability_display(site, card, value))

    rows: list[dict] = []
    for sku, row in products.items():
        if not isinstance(row, dict):
            continue
        columbia = row.get("columbia") if isinstance(row.get("columbia"), dict) else None
        amazon = row.get("amazon") if isinstance(row.get("amazon"), dict) else None
        ajio = row.get("ajio") if isinstance(row.get("ajio"), dict) else None
        adventure = row.get("adventure") if isinstance(row.get("adventure"), dict) else None
        myntra = row.get("myntra") if isinstance(row.get("myntra"), dict) else None
        tatacliq = row.get("tatacliq") if isinstance(row.get("tatacliq"), dict) else None
        image_card = columbia or amazon or ajio or adventure or myntra or tatacliq
        image_site = next((site for site, card in (("columbia", columbia), ("amazon", amazon), ("ajio", ajio), ("adventure", adventure), ("myntra", myntra), ("tatacliq", tatacliq)) if card is image_card), "columbia")
        image = _card_value(image_site, image_card, "image")
        if image == "NA":
            image = _card_value(image_site, image_card, "image_url")
        result = {
            "Product Image": image,
            "Columbia SKU": _normalized_sku(row.get("sku") or sku) or "NA",
            "EAN(s)": ", ".join(row.get("ean_numbers") or []) or "NA",
        }
        marketplace_cards = (
            ("amazon", "Amazon", amazon),
            ("ajio", "AJIO", ajio),
            ("adventure", "Adventuras", adventure),
            ("columbia", "Columbia", columbia),
            ("myntra", "Myntra", myntra),
            ("tatacliq", "TataCliq", tatacliq),
        )
        for site, label, card in marketplace_cards:
            result[f"{label} Price"] = _price_text(site, card)
        for site, label, card in marketplace_cards:
            result[f"{label} Title"] = _card_value(site, card, "title")
        for site, label, card in marketplace_cards:
            result[f"{label} Product URL"] = _card_value(site, card, "url")
        rows.append(result)
    rows.sort(key=lambda item: item.get("Columbia SKU", ""))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the unified SKU/EAN product dataset.")
    parser.add_argument("--output", default=str(NORMALIZED_PRODUCTS))
    parser.add_argument("--no-write-lookup", action="store_true", help="Skip writing the identifier lookup file.")
    parser.add_argument("--debug", action="store_true", help="Print normalized identifier diagnostics and example exact matches.")
    args = parser.parse_args()

    payload = build_normalized_products(Path(args.output), write_lookup=not args.no_write_lookup)
    print(json.dumps(payload.get("summary", {}), indent=2, ensure_ascii=False))
    if args.debug:
        debug = payload.get("normalization_debug", {})
        print(f"Columbia normalized SKUs : {debug.get('columbia_normalized_skus', 0)}")
        print(f"AJIO normalized SKUs : {debug.get('ajio_normalized_skus', 0)}")
        print(f"Adventure normalized SKUs : {debug.get('adventure_normalized_skus', 0)}")
        print(f"Amazon normalized EANs : {debug.get('amazon_normalized_eans', 0)}")
        print(f"SKU matches : {debug.get('sku_matches', 0)}")
        print(f"EAN matches : {debug.get('ean_matches', 0)}")
        print("Example matches:")
        for example in debug.get("example_matches", []):
            print(json.dumps(example, ensure_ascii=False))


if __name__ == "__main__":
    main()
