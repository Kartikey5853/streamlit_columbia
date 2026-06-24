import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from data_scraper.adventuras_scraper import AdventurasScraper
from data_scraper.ajio_scraper import AjioScraper
from data_scraper.columbia_scraper import ColumbiaScraper
from data_scraper.marketplace_common import (
    load_json,
    parse_price,
    save_json_atomic,
    scrape_with_retries,
)


BASE_DIR = Path(__file__).resolve().parent
AMAZON_PRODUCTS = BASE_DIR / "data" / "json" / "latest_amazon.json"
MARKETPLACE_PRODUCTS = BASE_DIR / "data" / "json" / "marketplace_products.json"
THROTTLED_BLOCKED_SITES = {"columbia", "adventuras"}

SCRAPERS = {
    "ajio": AjioScraper(),
    "columbia": ColumbiaScraper(),
    "adventuras": AdventurasScraper(),
}

log = logging.getLogger("marketplaces")


def amazon_products(path: Path) -> dict[str, dict]:
    data = load_json(path, {})
    products = data.get("products", data)
    if not isinstance(products, dict):
        return {}
    return {
        str(upc): product
        for upc, product in products.items()
        if str(upc).isdigit() and len(str(upc)) == 12
    }


def amazon_source(product: dict) -> dict:
    return {
        "title": product.get("title"),
        "image": product.get("image_url"),
        "price": product.get("price"),
        "link": product.get("url"),
    }


def initial_store(path: Path) -> dict:
    value = load_json(path, {})
    if isinstance(value, dict) and isinstance(value.get("products"), dict):
        value["schema_version"] = 2
        value["price_tolerance_inr"] = 1000
        return value
    return {
        "schema_version": 2,
        "primary_key": "upc",
        "price_tolerance_inr": 1000,
        "updated_at": None,
        "products": {},
    }


async def scrape_one(
    pages: dict,
    cooldowns: dict,
    upc: str,
    product: dict,
    existing: dict,
    block_delay: float,
) -> tuple[str, dict]:
    reference_price = parse_price(product.get("price_value") or product.get("price"))
    record = {
        "upc": upc,
        "title": product.get("title"),
        "material_composition": product.get("material_composition"),
        "amazon": amazon_source(product),
        "ajio": existing.get("ajio"),
        "columbia": existing.get("columbia"),
        "adventuras": existing.get("adventuras"),
    }
    if reference_price is None:
        return upc, record

    async def run_site(site: str):
        if isinstance(record.get(site), dict) and record[site].get("title"):
            return site, record[site]
        if isinstance(record.get(site), dict) and record[site].get("status") == "not_found":
            return site, record[site]

        now = asyncio.get_running_loop().time()
        wait_seconds = max(0.0, cooldowns.get(site, 0.0) - now)
        if wait_seconds:
            log.info("%s still cooling down %.1fs; skipping %s for now", site, wait_seconds, upc)
            return site, {
                "status": "blocked",
                "checked_at": datetime.now().isoformat(),
                "retry_after_seconds": round(wait_seconds, 1),
            }

        result = await scrape_with_retries(
            SCRAPERS[site],
            pages[site],
            upc,
            reference_price,
        )
        if isinstance(result, dict) and result.get("status") == "blocked":
            cooldowns[site] = asyncio.get_running_loop().time() + block_delay
            log.info("%s blocked on %s; cooling this site for %.1fs", site, upc, block_delay)
            if site in THROTTLED_BLOCKED_SITES:
                return site, {
                    "status": "blocked",
                    "checked_at": datetime.now().isoformat(),
                    "retry_after_seconds": round(block_delay, 1),
                }
            return site, result

        if isinstance(result, dict) and result.get("status") == "not_found":
            result = {
                "status": "not_found",
                "checked_at": datetime.now().isoformat(),
            }
        return site, result

    results = await asyncio.gather(*(run_site(site) for site in SCRAPERS))
    for site, result in results:
        record[site] = result
    return upc, record


async def run(args: argparse.Namespace) -> None:
    products = amazon_products(Path(args.amazon_products))
    store = initial_store(Path(args.output))
    stored_products = store["products"]
    items = list(products.items())
    if args.upc:
        wanted = set(args.upc)
        items = [(upc, product) for upc, product in items if upc in wanted]
    if args.limit:
        items = items[:args.limit]
    if not items:
        log.warning("No UPCs selected from %s", args.amazon_products)
        return

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=args.headless,
            slow_mo=args.slow_mo,
            args=[
                "--disable-dev-shm-usage",
                "--start-maximized",
            ],
        )
        context = await browser.new_context(
            locale="en-IN",
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )
        pages = {site: await context.new_page() for site in SCRAPERS}
        cooldowns = {site: 0.0 for site in SCRAPERS}
        for site, page in pages.items():
            await page.goto(
                SCRAPERS[site].config.base_url,
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await page.wait_for_timeout(1500)
            log.info("%s tab ready: %s", site, page.url)

        for index, (upc, product) in enumerate(items, start=1):
            _, record = await scrape_one(
                pages,
                cooldowns,
                upc,
                product,
                stored_products.get(upc, {}),
                args.block_delay,
            )
            stored_products[upc] = record
            store["updated_at"] = datetime.now().isoformat()
            save_json_atomic(Path(args.output), store)
            found = sum(
                isinstance(record.get(site), dict) and bool(record[site].get("title"))
                for site in SCRAPERS
            )
            not_found = sum(
                isinstance(record.get(site), dict)
                and record[site].get("status") == "not_found"
                for site in SCRAPERS
            )
            log.info(
                "%s complete: %s/3 marketplace matches, %s confirmed missing",
                upc,
                found,
                not_found,
            )

            if index < len(items) and args.delay:
                log.info("waiting %.1fs before next UPC", args.delay)
                await asyncio.sleep(args.delay)

        await context.close()
        await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich Amazon UPC products with AJIO, Columbia, and Adventuras."
    )
    parser.add_argument("--amazon-products", default=str(AMAZON_PRODUCTS))
    parser.add_argument("--output", default=str(MARKETPLACE_PRODUCTS))
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Deprecated; the scraper now reuses one tab per site.",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--upc", action="append", help="Only scrape this UPC. Repeatable.")
    parser.add_argument("--delay", type=float, default=4.0)
    parser.add_argument("--block-delay", type=float, default=300.0)
    parser.add_argument("--slow-mo", type=int, default=250)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headed", action="store_false", dest="headless")
    parser.set_defaults(headless=False)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
