from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path
import os

from playwright.async_api import async_playwright

from data_scraper.adventuras_scraper import AdventurasScraper
from data_scraper.ajio_scraper import AjioScraper
from data_scraper.columbia_scraper import ColumbiaScraper
from data_scraper.marketplace_common import parse_price, scrape_with_retries
from processing.checkpoints import is_done, mark_done
from processing.config import load_config
from processing.json_store import load_json, products_by_ean, save_json_atomic
from processing.platform_paths import AMAZON_PRODUCTS, MARKETPLACE_PRODUCTS, dated_json_path, latest_json_path, log_path
from processing.product_schema import product_card
from processing.structured_logging import get_scraper_logger, log_event


SCRAPER_CLASSES = {
    "ajio": AjioScraper,
    "columbia": ColumbiaScraper,
    "adventuras": AdventurasScraper,
}


def _normalize_product_store(payload: object) -> dict:
    if isinstance(payload, dict):
        payload.setdefault("schema_version", 1)
        payload.setdefault("primary_key", "EAN")
        payload.setdefault("updated_at", None)
        products = payload.get("products")
        if not isinstance(products, dict):
            payload["products"] = {}
        return payload
    # Legacy shape support: previous outputs may be list-based.
    return {
        "schema_version": 1,
        "primary_key": "EAN",
        "updated_at": None,
        "products": {},
    }


def amazon_card(product: dict) -> dict:
    return {
        "title": product.get("title") or product.get("name"),
        "image": product.get("image") or product.get("image_url"),
        "url": product.get("url") or product.get("link"),
        "price": product.get("price"),
    }


async def scrape_site(site: str, headless: bool, eans: list[str] | None = None, limit: int | None = None) -> dict:
    config = load_config()
    cooldown = float(config.get(f"{site}_cooldown", 0))
    logger = get_scraper_logger(site, log_path(site))
    amazon = products_by_ean(load_json(AMAZON_PRODUCTS, {}))
    if eans:
        wanted = set(eans)
        items = [(ean, product) for ean, product in amazon.items() if ean in wanted]
    else:
        items = list(amazon.items())
    if limit:
        items = items[:limit]

    store = _normalize_product_store(load_json(MARKETPLACE_PRODUCTS, {
        "schema_version": 1,
        "primary_key": "EAN",
        "updated_at": None,
        "products": {},
    }))
    latest_site_output = Path(os.environ.get("CPI_OUTPUT", latest_json_path(site)))
    dated_site_output = Path(os.environ.get("CPI_DATED_OUTPUT", dated_json_path(site, datetime.now().date().isoformat())))
    site_payload = _normalize_product_store(load_json(latest_site_output, {
        "schema_version": 1,
        "primary_key": "EAN",
        "products": {},
    }))

    scraper = SCRAPER_CLASSES[site]()
    processed = 0
    skipped = 0
    found = 0
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        page = await browser.new_page(locale="en-IN")
        for ean, product in items:
            if is_done(ean, site):
                skipped += 1
                log_event(logger, logging.INFO, ean, f"{site} already complete; skipping")
                continue
            reference_price = parse_price(product.get("price_value") or product.get("price"))
            if reference_price is None:
                log_event(logger, logging.WARNING, ean, "amazon source price missing; cannot validate candidate")
                continue
            try:
                result = await scrape_with_retries(scraper, page, ean, reference_price)
                row = store["products"].setdefault(ean, {"EAN": ean})
                row["amazon"] = row.get("amazon") or amazon_card(product)
                row[site] = product_card(result) if result and result.get("title") else result
                row["updated_at"] = datetime.now().isoformat(timespec="seconds")
                store["updated_at"] = row["updated_at"]
                save_json_atomic(MARKETPLACE_PRODUCTS, store)
                site_payload["products"][ean] = row.get(site)
                site_payload["updated_at"] = row["updated_at"]
                save_json_atomic(latest_site_output, site_payload)
                save_json_atomic(dated_site_output, site_payload)
                if result and result.get("title"):
                    mark_done(ean, site)
                    found += 1
                    log_event(logger, logging.INFO, ean, f"{site} scraped successfully")
                elif result and result.get("status") == "not_found":
                    mark_done(ean, site)
                    log_event(logger, logging.WARNING, ean, f"{site} not found")
                elif result and result.get("status") == "blocked":
                    log_event(logger, logging.ERROR, ean, f"{site} challenge or block detected")
                    if cooldown:
                        await asyncio.sleep(cooldown)
                else:
                    log_event(logger, logging.WARNING, ean, f"{site} candidate rejected")
                processed += 1
                if cooldown:
                    await asyncio.sleep(cooldown)
            except Exception as exc:
                log_event(logger, logging.ERROR, ean, f"{site} scrape failed: {exc}")
        await browser.close()
    return {"processed": processed, "skipped": skipped, "found": found}


def main() -> None:
    parser = argparse.ArgumentParser(description="EAN-driven marketplace scraper.")
    parser.add_argument("site", choices=sorted(SCRAPER_CLASSES))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headed", action="store_false", dest="headless")
    parser.add_argument("--ean", action="append")
    parser.add_argument("--limit", type=int)
    parser.set_defaults(headless=False)
    args = parser.parse_args()
    result = asyncio.run(scrape_site(args.site, args.headless, args.ean, args.limit))
    print(result)


if __name__ == "__main__":
    main()
