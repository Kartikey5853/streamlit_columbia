#!/usr/bin/env python3
"""
amazon_ean_scraper.py

Reads a product dataset (Columbia-style schema), groups variants by their base
SKU (e.g. "WS3220-275-1X", "WS3220-275-2X" -> base "WS3220-275"), and does ONE
Amazon search per base SKU group using that group's barcode/EAN. This avoids
searching every individual size's EAN separately (20K+ EANs -> just one search
per product group).

For each group, opens the first Amazon search result and scrapes:
  - Title
  - Price
  - ASIN
  - UPC (from the product details / detail bullets table, if listed)
  - Style / Model number (from "Item model number" in product details, if listed)

Runs N concurrent headless Chrome tabs (default 4) via Playwright, and
checkpoints (dumps) results to the output file every X completed groups
(default 50) so nothing is lost on a crash/interrupt.

USAGE
-----
    python3 amazon_ean_scraper.py --input columbia.json --output results.json

    python3 amazon_ean_scraper.py \
        --input columbia.json \
        --output results.csv \
        --tabs 6 \
        --domain amazon.in \
        --no-headless \
        --limit 20 \
        --checkpoint-every 25

ARGUMENTS
---------
    --input             Path to input JSON (Columbia schema).
    --output            Path to output file. Extension decides format (.json or .csv).
    --tabs              Number of concurrent headless tabs/pages. Default: 4
    --domain            Amazon domain to search. Default: amazon.in
    --headless          Run headless (default True). Use --no-headless to see the browser.
    --timeout           Per-page navigation timeout in ms. Default: 30000
    --delay             Delay in seconds between tasks per tab. Default: 1.5
    --limit             Only process the first N SKU groups (useful for testing).
    --retries           Retries per group on failure. Default: 2
    --checkpoint-every  Dump partial results to --output every N completed
                         groups (across all tabs combined). Default: 50
    --user-data-dir     Optional persistent Chrome profile dir (helps avoid
                         repeated bot-check pages). Default: none.

NOTES
-----
- One search is done per base SKU group, using that group's representative
  barcode (raw.barcode, falling back to the top-level `ean`). The output row
  for each group includes `sku` (base SKU), `ean` (the barcode searched), and
  `all_eans` (every size/variant EAN belonging to that group), so results can
  still be joined back to every individual EAN in your dataset.
- Amazon actively blocks/CAPTCHAs automated traffic. Keep concurrency + speed
  modest and treat captcha/not_found statuses as expected outcomes.
- Respect Amazon's Terms of Service and robots.txt for your use case.
"""

import argparse
import asyncio
import csv
import json
import random
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def split_sku(sku: str | None) -> tuple[str, str]:
    if not sku:
        return "", ""

    sku = sku.strip().upper()
    parts = sku.split("-")

    if len(parts) <= 2:
        return sku, ""

    # Base SKU is always first two parts
    base = f"{parts[0]}-{parts[1]}"

    # Everything else is size / region / fit
    size = "-".join(parts[2:])

    return base, size


@dataclass
class SkuGroupTask:
    base_sku: str
    search_ean: str
    all_eans: list[str] = field(default_factory=list)
    source_product_id: str = ""
    source_title: str = ""


@dataclass
class ScrapeResult:
    sku: str
    ean: str = ""                                   # the barcode actually searched
    all_eans: list[str] = field(default_factory=list)
    source_product_id: str = ""
    source_title: str = ""
    status: str = "pending"          # ok | not_found | captcha | error
    amazon_url: Optional[str] = None
    asin: Optional[str] = None
    title: Optional[str] = None
    price: Optional[str] = None
    upc: Optional[str] = None
    style_number: Optional[str] = None
    error: Optional[str] = None


def extract_tasks(data) -> list[SkuGroupTask]:
    """
    Group variants by base SKU (via split_sku) and produce ONE search task per
    group, using the group's barcode as the representative EAN to search.
    """
    tasks: list[SkuGroupTask] = []
    seen_bases = set()

    products = data if isinstance(data, list) else data.get("products", [data])

    for prod in products:
        raw = prod.get("raw", {}) or {}

        # Prefer raw.sku (e.g. "WS3220-275-1X"); fall back to first variant's sku
        sku_source = raw.get("sku")
        if not sku_source:
            variants = prod.get("variant_mapping") or []
            sku_source = variants[0].get("sku") if variants else None

        base_sku, _size = split_sku(sku_source)
        if not base_sku:
            continue
        if base_sku in seen_bases:
            continue
        seen_bases.add(base_sku)

        # Collect every EAN belonging to this group (top-level + all variants)
        all_eans: list[str] = []
        top_ean = prod.get("ean")
        if top_ean:
            all_eans.append(top_ean)
        for variant in prod.get("variant_mapping") or []:
            e = variant.get("ean")
            if e and e not in all_eans:
                all_eans.append(e)

        # The representative barcode for the group == the search term
        search_ean = raw.get("barcode") or top_ean or (all_eans[0] if all_eans else None)
        if not search_ean:
            continue

        tasks.append(SkuGroupTask(
            base_sku=base_sku,
            search_ean=search_ean,
            all_eans=all_eans,
            source_product_id=prod.get("source_product_id", ""),
            source_title=prod.get("title", ""),
        ))

    return tasks


async def find_first_result_url(page: Page, domain: str, ean: str, timeout: int) -> Optional[str]:
    search_url = f"https://www.{domain}/s?k={ean}"
    await page.goto(search_url, timeout=timeout, wait_until="domcontentloaded")

    # Basic captcha/block detection
    body_text = (await page.content()).lower()
    if "api-services-support@amazon.com" in body_text or "enter the characters you see below" in body_text:
        raise RuntimeError("CAPTCHA")

    result_selector = "div[data-component-type='s-search-result']"
    try:
        await page.wait_for_selector(result_selector, timeout=timeout)
    except PWTimeout:
        return None

    first = page.locator(result_selector).first
    link = first.locator("a.a-link-normal.s-no-outline, h2 a").first
    href = await link.get_attribute("href")
    if not href:
        return None
    if href.startswith("/"):
        href = f"https://www.{domain}{href}"
    return href


async def scrape_product_page(page: Page, url: str, timeout: int) -> dict:
    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")

    data = {"amazon_url": url}

    # ASIN — most reliably comes from the URL itself
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    if m:
        data["asin"] = m.group(1)

    # Title
    try:
        title_el = page.locator("#productTitle").first
        data["title"] = (await title_el.inner_text(timeout=5000)).strip()
    except Exception:
        data["title"] = None

    # Price — try a few common selectors
    price = None
    for sel in [
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        ".a-price .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                price = (await loc.inner_text(timeout=3000)).strip()
                if price:
                    break
        except Exception:
            continue
    data["price"] = price

    # Product details table(s): detail bullets + tech spec tables. Look for
    # "ASIN", "UPC", "Item model number" key/value pairs across all of them.
    detail_map = {}
    row_selectors = [
        "#detailBulletsWrapper_feature_div li",
        "#productDetails_detailBullets_sections1 tr",
        "#productDetails_techSpec_section_1 tr",
        "table.prodDetTable tr",
    ]
    for sel in row_selectors:
        rows = page.locator(sel)
        count = await rows.count()
        for i in range(count):
            try:
                row_text = (await rows.nth(i).inner_text(timeout=2000)).strip()
            except Exception:
                continue
            if not row_text:
                continue
            parts = re.split(r":\s*|\n", row_text, maxsplit=1)
            if len(parts) == 2:
                key = parts[0].strip().lower()
                val = parts[1].strip()
                detail_map[key] = val

    def find_key(*keywords):
        for k, v in detail_map.items():
            if any(kw in k for kw in keywords):
                return v
        return None

    data["upc"] = find_key("upc")
    data["style_number"] = find_key("item model number", "style name", "style number")
    if not data.get("asin"):
        data["asin"] = find_key("asin")

    return data


async def process_task(page: Page, task: SkuGroupTask, domain: str, timeout: int,
                        delay: float, retries: int) -> ScrapeResult:
    result = ScrapeResult(
        sku=task.base_sku, ean=task.search_ean, all_eans=task.all_eans,
        source_product_id=task.source_product_id, source_title=task.source_title,
    )

    attempt = 0
    while attempt <= retries:
        attempt += 1
        try:
            product_url = await find_first_result_url(page, domain, task.search_ean, timeout)
            if not product_url:
                result.status = "not_found"
                break

            scraped = await scrape_product_page(page, product_url, timeout)
            for k, v in scraped.items():
                setattr(result, k, v)
            result.status = "ok"
            break

        except RuntimeError as e:
            if str(e) == "CAPTCHA":
                result.status = "captcha"
                result.error = "Hit CAPTCHA/bot-check page"
                await asyncio.sleep(delay * 3)
                continue
            result.status = "error"
            result.error = str(e)
            break

        except Exception as e:
            result.status = "error"
            result.error = f"{type(e).__name__}: {e}"
            await asyncio.sleep(delay)
            continue

    return result


async def worker(name: int, page: Page, task_queue: "asyncio.Queue[SkuGroupTask]",
                  results: list, results_lock: asyncio.Lock, domain: str, timeout: int,
                  delay: float, retries: int, checkpoint_every: int, output_path: str):
    while True:
        try:
            task: SkuGroupTask = task_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        result = await process_task(page, task, domain, timeout, delay, retries)

        print(f"[tab {name}] {task.base_sku} (ean {task.search_ean}) -> {result.status}"
              f"{' (' + result.title[:60] + ')' if result.title else ''}")

        async with results_lock:
            results.append(result)
            if checkpoint_every and len(results) % checkpoint_every == 0:
                write_output(results, output_path)
                print(f"  -- checkpoint: dumped {len(results)} rows to {output_path}")

        await asyncio.sleep(delay)


async def run(args):
    input_path = Path(args.input)
    data = json.loads(input_path.read_text())
    all_tasks = extract_tasks(data)

    if args.limit:
        all_tasks = all_tasks[: args.limit]

    if not all_tasks:
        print("No SKU groups found in input file.", file=sys.stderr)
        return

    print(f"Found {len(all_tasks)} SKU group(s) to search on {args.domain} "
          f"using {args.tabs} concurrent tab(s) (one search per group)...")

    queue: "asyncio.Queue[SkuGroupTask]" = asyncio.Queue()
    for t in all_tasks:
        queue.put_nowait(t)

    results: list[ScrapeResult] = []
    results_lock = asyncio.Lock()

    async with async_playwright() as p:
        launch_kwargs = dict(
            headless=args.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        if args.user_data_dir:
            ctx = await p.chromium.launch_persistent_context(args.user_data_dir, **launch_kwargs)
            pages = [await ctx.new_page() for _ in range(args.tabs)]
            workers = [
                worker(i, pages[i], queue, results, results_lock, args.domain,
                       args.timeout, args.delay, args.retries, args.checkpoint_every, args.output)
                for i in range(args.tabs)
            ]
            await asyncio.gather(*workers)
            await ctx.close()
        else:
            browser = await p.chromium.launch(**launch_kwargs)
            contexts = [
                await browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    viewport={"width": 1366, "height": 900},
                    locale="en-IN" if args.domain.endswith(".in") else "en-US",
                )
                for _ in range(args.tabs)
            ]
            pages = [await ctx.new_page() for ctx in contexts]
            workers = [
                worker(i, pages[i], queue, results, results_lock, args.domain,
                       args.timeout, args.delay, args.retries, args.checkpoint_every, args.output)
                for i in range(args.tabs)
            ]
            await asyncio.gather(*workers)
            for ctx in contexts:
                await ctx.close()
            await browser.close()

    write_output(results, args.output)
    ok = sum(1 for r in results if r.status == "ok")
    print(f"\nDone. {ok}/{len(results)} scraped successfully. Wrote {args.output}")


def write_output(results: list[ScrapeResult], output_path: str):
    out = Path(output_path)
    rows = [asdict(r) for r in results]

    if out.suffix.lower() == ".csv":
        # CSV can't hold a list cell cleanly, so join all_eans with ";"
        csv_rows = []
        for r in rows:
            r = dict(r)
            r["all_eans"] = ";".join(r.get("all_eans") or [])
            csv_rows.append(r)
        fieldnames = list(csv_rows[0].keys()) if csv_rows else []
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_rows)
    else:
        out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_args():
    ap = argparse.ArgumentParser(description="Search Amazon by SKU-group barcode and scrape product data.")
    ap.add_argument("--input", required=True, help="Input JSON file (Columbia schema).")
    ap.add_argument("--output", required=True, help="Output file path (.json or .csv).")
    ap.add_argument("--tabs", type=int, default=4, help="Number of concurrent tabs. Default 4.")
    ap.add_argument("--domain", default="amazon.in", help="Amazon domain to search. Default amazon.in.")
    ap.add_argument("--headless", dest="headless", action="store_true", default=True)
    ap.add_argument("--no-headless", dest="headless", action="store_false")
    ap.add_argument("--timeout", type=int, default=30000, help="Nav timeout in ms. Default 30000.")
    ap.add_argument("--delay", type=float, default=1.5, help="Delay (s) between tasks per tab. Default 1.5.")
    ap.add_argument("--limit", type=int, default=None, help="Only process first N SKU groups.")
    ap.add_argument("--retries", type=int, default=2, help="Retries per group. Default 2.")
    ap.add_argument("--checkpoint-every", type=int, default=50,
                     help="Dump partial results to --output every N completed groups. Default 50. Use 0 to disable.")
    ap.add_argument("--user-data-dir", default=None,
                     help="Optional persistent Chrome profile dir to reduce repeat CAPTCHAs.")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(args))