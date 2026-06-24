from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from .config import load_config
from .json_store import load_json, save_json_atomic
from .platform_paths import FINAL_TUPLES, log_path
from .product_schema import format_inr, price_value
from .structured_logging import get_scraper_logger, log_event


PRICE_SELECTORS = [
    "meta[property='product:price:amount']",
    "meta[property='og:price:amount']",
    ".a-price .a-offscreen",
    ".prod-sp",
    ".price-item--sale",
    ".price-item--regular",
    "[class*='price']",
]


async def extract_latest_price(page, url: str) -> float | None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(700)
    for selector in PRICE_SELECTORS:
        locator = page.locator(selector).first
        try:
            if not await locator.count():
                continue
            if selector.startswith("meta"):
                raw = await locator.get_attribute("content")
            else:
                raw = await locator.text_content()
            parsed = price_value(raw)
            if parsed is not None:
                return parsed
        except Exception:
            continue
    parsed = price_value(await page.locator("body").inner_text(timeout=3000))
    return parsed


async def refresh_site(site: str, tuples_path: Path, headless: bool = True) -> dict:
    config = load_config()
    logger = get_scraper_logger(site, log_path(site))
    payload = load_json(tuples_path, {"products": {}})
    products = payload.get("products", payload) if isinstance(payload, dict) else {}
    if not isinstance(products, dict):
        raise RuntimeError(f"{tuples_path} must contain a products object keyed by EAN.")

    updated = 0
    checked = 0
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        page = await browser.new_page(locale="en-IN")
        for ean, row in products.items():
            card = row.get(site) if isinstance(row, dict) else None
            url = card.get("url") if isinstance(card, dict) else None
            if not url:
                continue
            checked += 1
            try:
                latest = await extract_latest_price(page, url)
                if latest is None:
                    log_event(logger, logging.WARNING, ean, "price refresh could not read latest price")
                    continue
                old = price_value(card.get("price"))
                card["price"] = format_inr(latest)
                card["price_value"] = latest
                card["price_refreshed_at"] = datetime.now().isoformat(timespec="seconds")
                if old != latest:
                    updated += 1
                    log_event(logger, logging.INFO, ean, f"price changed from {old} to {latest}")
                else:
                    log_event(logger, logging.INFO, ean, "price unchanged")
                await asyncio.sleep(float(config["price_refresh_delay_seconds"]))
            except Exception as exc:
                log_event(logger, logging.ERROR, ean, f"price refresh failed: {exc}")
        await browser.close()
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_json_atomic(tuples_path, payload)
    return {"checked": checked, "updated": updated}


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh prices from stored product URLs only.")
    parser.add_argument("site", choices=["amazon", "ajio", "columbia", "adventuras", "myntra", "tatacliq"])
    parser.add_argument("--tuples", default=str(FINAL_TUPLES))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headed", action="store_false", dest="headless")
    parser.set_defaults(headless=True)
    args = parser.parse_args()
    result = asyncio.run(refresh_site(args.site, Path(args.tuples), args.headless))
    print(result)


if __name__ == "__main__":
    main()
