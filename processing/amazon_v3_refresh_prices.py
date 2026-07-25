from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from data_scraper.amazon_scraper_v3 import USER_AGENTS, scrape_product_page

from .json_store import load_json, product_list, save_json_atomic
from .platform_paths import NORMALIZED_PRODUCTS, SCRAPER_JSON_PRODUCTS
from .process_status import mark_started, mark_stopped, update_site_status
from .product_store import ingest_scrape
from .structured_logging import get_scraper_logger, log_event
from .unified_products import build_normalized_products


STATUS_SITE = "amazon_v3_refresh"


def _amazon_records(payload: Any) -> list[tuple[str | int, dict]]:
    if isinstance(payload, dict) and isinstance(payload.get("products"), dict):
        return [(str(key), item) for key, item in payload["products"].items() if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("products"), list):
        return [(index, item) for index, item in enumerate(payload["products"]) if isinstance(item, dict)]
    if isinstance(payload, list):
        return [(index, item) for index, item in enumerate(payload) if isinstance(item, dict)]
    return [(index, item) for index, item in enumerate(product_list(payload))]


def _record_ean(record: dict, key: str | int) -> str:
    for field in ("upc", "ean"):
        value = record.get(field)
        if value:
            return str(value)
    return str(key)


def _apply_scraped_price(record: dict, scraped: dict) -> bool:
    price = scraped.get("price")
    if not price:
        return False
    changed = record.get("price") != price or record.get("price_value") != scraped.get("price_value")
    record["price"] = price
    record["price_value"] = scraped.get("price_value")
    record["currency"] = scraped.get("currency") or record.get("currency") or "INR"
    record["scraped_at"] = datetime.now().isoformat(timespec="seconds")
    if scraped.get("title"):
        record["title"] = scraped["title"]
    if scraped.get("amazon_url"):
        record["url"] = scraped["amazon_url"]
    if scraped.get("asin"):
        record["asin"] = scraped["asin"]
    return changed


async def _worker(
    name: int,
    page,
    queue: "asyncio.Queue[tuple[str | int, dict]]",
    counters: dict[str, int],
    lock: asyncio.Lock,
    logger,
    timeout: int,
    delay: float,
) -> None:
    while True:
        try:
            key, record = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        ean = _record_ean(record, key)
        update_site_status(STATUS_SITE, {"current_ean": ean, "message": f"Refreshing {ean}"})
        url = record.get("url") or record.get("amazon_url")
        if not url:
            async with lock:
                counters["failed"] += 1
            log_event(logger, logging.WARNING, ean, "missing Amazon URL; skipped")
            continue

        try:
            scraped = await scrape_product_page(page, str(url), timeout)
            changed = _apply_scraped_price(record, scraped)
            async with lock:
                counters["ok"] += 1
                if changed:
                    counters["changed"] += 1
            price_text = scraped.get("price") or "N/A"
            log_event(logger, logging.INFO, ean, f"tab={name} refreshed price={price_text} changed={changed}")
        except Exception as exc:
            async with lock:
                counters["failed"] += 1
            log_event(logger, logging.ERROR, ean, f"refresh failed: {type(exc).__name__}: {exc}")
        finally:
            async with lock:
                update_site_status(STATUS_SITE, {
                    "success_count": counters["ok"],
                    "failure_count": counters["failed"],
                    "warning_count": counters["changed"],
                })
            await asyncio.sleep(delay)


async def refresh_prices(args: argparse.Namespace) -> dict[str, int]:
    input_path = Path(args.input)
    payload = load_json(input_path, {"schema_version": 2, "primary_key": "upc", "products": {}})
    records = _amazon_records(payload)
    records = [(key, record) for key, record in records if record.get("url") or record.get("amazon_url")]
    if args.limit:
        records = records[: args.limit]

    logger = get_scraper_logger(STATUS_SITE)
    log_event(logger, logging.INFO, "PIPELINE", f"START Amazon V3 price refresh records={len(records)} tabs={args.tabs}")

    counters = {"ok": 0, "failed": 0, "changed": 0}
    if not records:
        log_event(logger, logging.WARNING, "PIPELINE", "No Amazon records with URLs found")
        return counters

    queue: "asyncio.Queue[tuple[str | int, dict]]" = asyncio.Queue()
    for item in records:
        queue.put_nowait(item)

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": args.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        browser = await p.chromium.launch(**launch_kwargs)
        contexts = [
            await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1366, "height": 900},
                locale="en-IN",
            )
            for _ in range(max(1, args.tabs))
        ]
        pages = [await context.new_page() for context in contexts]
        lock = asyncio.Lock()
        await asyncio.gather(*[
            _worker(index, pages[index], queue, counters, lock, logger, args.timeout, args.delay)
            for index in range(len(pages))
        ])
        for context in contexts:
            await context.close()
        await browser.close()

    if isinstance(payload, dict):
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_json_atomic(input_path, payload)
    ingest_scrape("amazon", payload, datetime.now().date().isoformat())
    build_normalized_products(NORMALIZED_PRODUCTS)
    log_event(
        logger,
        logging.INFO,
        "PIPELINE",
        f"DONE Amazon V3 price refresh ok={counters['ok']} changed={counters['changed']} failed={counters['failed']}",
    )
    return counters


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh prices for existing Amazon JSON records using Amazon Scraper V3.")
    parser.add_argument("--input", default=str(SCRAPER_JSON_PRODUCTS["amazon"]))
    parser.add_argument("--tabs", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=30000)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument("--headed", dest="headless", action="store_false")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    mark_started(STATUS_SITE, os.getpid(), "Amazon V3 price refresh running")
    try:
        counters = asyncio.run(refresh_prices(args))
        mark_stopped(
            STATUS_SITE,
            f"Amazon V3 price refresh complete: ok={counters['ok']} changed={counters['changed']} failed={counters['failed']}",
        )
    except Exception as exc:
        logger = get_scraper_logger(STATUS_SITE)
        log_event(logger, logging.ERROR, "PIPELINE", f"FAILED {type(exc).__name__}: {exc}")
        mark_stopped(STATUS_SITE, f"Amazon V3 price refresh failed: {exc}")
        raise


if __name__ == "__main__":
    main()
