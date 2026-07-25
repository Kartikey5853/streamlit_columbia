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
    --tabs              Number of concurrent tabs. Default: 4
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
from typing import Any, Optional

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

    base = f"{parts[0]}-{parts[1]}"
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
    ean: str = ""
    all_eans: list[str] = field(default_factory=list)
    source_product_id: str = ""
    source_title: str = ""
    status: str = "pending"          # ok | not_found | captcha | error
    amazon_url: Optional[str] = None
    asin: Optional[str] = None
    title: Optional[str] = None
    price: Optional[str] = None
    price_value: Optional[float] = None
    currency: Optional[str] = None
    upc: Optional[str] = None
    style_number: Optional[str] = None
    match_method: Optional[str] = None   # "ean" | "title" -- how the listing was found
    error: Optional[str] = None


def clean_price(raw: Any, currency_symbol: str = "\u20b9") -> Optional[str]:
    if raw in (None, ""):
        return None

    text = re.sub(r"\s+", " ", str(raw).replace("\xa0", " ")).strip()
    if not text:
        return None

    patterns = [
        r"(?:\u20b9|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)",
        r"([\d,]+(?:\.\d{1,2})?)\s*(?:\u20b9|Rs\.?|INR)",
        r"^\s*([\d,]+(?:\.\d{1,2})?)\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            amount = match.group(1).replace(",", "")
            try:
                value = float(amount)
            except ValueError:
                continue
            if value <= 0:
                continue
            return f"{currency_symbol}{value:,.2f}"
    return None


def price_value(raw: Any) -> Optional[float]:
    if raw in (None, ""):
        return None
    match = re.search(r"[\d,]+(?:\.\d+)?", str(raw))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _iter_json_values(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_values(child)


def _price_from_json_ld(value: Any) -> Optional[str]:
    for node in _iter_json_values(value):
        for key in ("price", "lowPrice", "highPrice"):
            price = clean_price(node.get(key))
            if price:
                return price
    return None


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

        all_eans: list[str] = []
        top_ean = prod.get("ean")
        if top_ean:
            all_eans.append(top_ean)
        for variant in prod.get("variant_mapping") or []:
            e = variant.get("ean")
            if e and e not in all_eans:
                all_eans.append(e)

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


def _title_search_query(title: str, max_words: int = 7) -> str:
    """Trim a product title down to a tight, high-signal search query
    (brand + key nouns) rather than throwing the whole sentence at Amazon."""
    words = re.sub(r"[^\w\s-]", " ", title).split()
    return " ".join(words[:max_words])


async def find_first_result_url(page: Page, domain: str, query: str, timeout: int) -> Optional[str]:
    from urllib.parse import quote_plus
    search_url = f"https://www.{domain}/s?k={quote_plus(query)}"
    await page.goto(search_url, timeout=timeout, wait_until="domcontentloaded")

    body_text = (await page.content()).lower()
    if "api-services-support@amazon.com" in body_text or "enter the characters you see below" in body_text:
        raise RuntimeError("CAPTCHA")

    result_selector = "div[data-component-type='s-search-result']"
    try:
        await page.wait_for_selector(result_selector, timeout=timeout)
    except PWTimeout:
        return None

    # Give lazily-rendered result tiles a brief moment to settle before we
    # grab the first link -- under high tab concurrency this can otherwise
    # race the render and produce false not_found/None hrefs.
    await page.wait_for_timeout(random.randint(200, 500))

    first = page.locator(result_selector).first
    link = first.locator("a.a-link-normal.s-no-outline, h2 a").first
    href = await link.get_attribute("href")
    if not href:
        return None
    if href.startswith("/"):
        href = f"https://www.{domain}{href}"
    return href


# JS extractor run inside the page context. Doing everything in ONE
# evaluate() call (instead of many separate Playwright locator round-trips)
# is what makes this reliable -- Amazon's price widgets are injected/mutated
# by client-side JS shortly after DOMContentLoaded, and a single synchronous
# querySelector pass taken *after* we've explicitly waited for that widget
# beats racing several locator().count()/text_content() calls against it.
_PAGE_EXTRACT_JS = """
() => {
    const clean = v => (v || "").replace(/\\s+/g, " ").trim();

    const priceSelectors = [
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePrice_feature_div .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#apex_desktop .a-offscreen",
        "#newBuyBoxPrice",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#price_inside_buybox",
        "span[data-a-color='price'] .a-offscreen",
        ".a-price .a-offscreen",
        ".a-price-whole",
    ];

    let price = null;
    for (const sel of priceSelectors) {
        const el = document.querySelector(sel);
        const text = clean(el?.textContent);
        if (text) { price = text; break; }
    }

    if (!price) {
        const metaSelectors = [
            "meta[property='product:price:amount']",
            "meta[property='og:price:amount']",
            "meta[name='twitter:data1']",
            "meta[name='price']",
        ];
        for (const sel of metaSelectors) {
            const val = clean(document.querySelector(sel)?.getAttribute("content"));
            if (val) { price = val; break; }
        }
    }

    const details = {};
    document.querySelectorAll(
        "#detailBulletsWrapper_feature_div li, " +
        "#productDetails_detailBullets_sections1 tr, " +
        "#productDetails_techSpec_section_1 tr, " +
        "table.prodDetTable tr"
    ).forEach(row => {
        const text = clean(row.textContent);
        if (!text) return;
        const parts = text.split(/:\\s*|\\n/);
        if (parts.length >= 2) {
            const key = clean(parts[0]).toLowerCase();
            const val = clean(parts.slice(1).join(" "));
            if (key && val) details[key] = val;
        }
    });

    const jsonLd = [...document.querySelectorAll("script[type='application/ld+json']")]
        .map(n => { try { return JSON.parse(n.textContent || "{}"); } catch (_) { return null; } })
        .filter(Boolean);

    return {
        title: clean(document.querySelector("#productTitle")?.textContent),
        price,
        details,
        jsonLd,
        pageText: clean(document.body?.textContent).slice(0, 20000),
    };
}
"""


async def scrape_product_page(page: Page, url: str, timeout: int) -> dict:
    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")

    # Give Amazon's client-side JS time to paint the price widget before we
    # read the DOM. This is the step v3 was missing/rushing before.
    try:
        await page.wait_for_selector(
            "#corePrice_feature_div, #apex_desktop, #corePriceDisplay_desktop_feature_div, .a-price",
            timeout=8000,
        )
    except PWTimeout:
        pass

    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 5000))
    except Exception:
        pass

    # small settle delay, same idea as v2's random wait before reading price
    await page.wait_for_timeout(random.randint(300, 700))

    data = {"amazon_url": url}

    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    if m:
        data["asin"] = m.group(1)

    try:
        extracted = await page.evaluate(_PAGE_EXTRACT_JS)
    except Exception:
        extracted = {}

    data["title"] = extracted.get("title") or None

    price = clean_price(extracted.get("price"))

    # Fallback #1: JSON-LD price (covers pages where the offscreen span
    # wasn't present but structured data is).
    if not price:
        price = _price_from_json_ld(extracted.get("jsonLd") or [])

    # Fallback #2: raw currency-symbol regex over the visible page text.
    if not price:
        page_text = extracted.get("pageText") or ""
        match = re.search(r"(?:\u20b9|Rs\.?|INR)\s*[\d,]+(?:\.\d{1,2})?", page_text, re.I)
        if match:
            price = clean_price(match.group(0))

    data["price"] = price
    data["price_value"] = price_value(price)
    data["currency"] = "INR" if price else None

    detail_map = extracted.get("details") or {}

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

    # Try the EAN first (highest confidence: barcode search only matches if
    # it's really that SKU), then fall back to a title search. Many Amazon.in
    # listings simply don't have the source barcode indexed as a searchable
    # keyword even though the product itself is live on the site -- a pure
    # EAN search then returns zero results (not_found) even though a title
    # search would find it immediately.
    queries = [("ean", task.search_ean)]
    if task.source_title:
        queries.append(("title", _title_search_query(task.source_title)))

    attempt = 0
    while attempt <= retries:
        attempt += 1
        hit = False

        for method, query in queries:
            try:
                product_url = await find_first_result_url(page, domain, query, timeout)
                if not product_url:
                    result.status = "not_found"
                    continue

                scraped = await scrape_product_page(page, product_url, timeout)
                for k, v in scraped.items():
                    setattr(result, k, v)
                result.status = "ok"
                result.match_method = method
                result.error = None
                hit = True
                break

            except RuntimeError as e:
                if str(e) == "CAPTCHA":
                    result.status = "captcha"
                    result.error = "Hit CAPTCHA/bot-check page"
                    await asyncio.sleep(delay * 3)
                    continue
                result.status = "error"
                result.error = str(e)

            except Exception as e:
                result.status = "error"
                result.error = f"{type(e).__name__}: {e}"
                await asyncio.sleep(delay)

        if hit or result.status in ("captcha", "error"):
            break
        # both ean and title queries came back not_found -- retry the loop
        # (network/render hiccups can cause transient false not_founds)

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

        price_text = f" price={result.price_value:g}" if result.price_value is not None else " price=N/A"
        print(f"[tab {name}] {task.base_sku} (ean {task.search_ean}) -> {result.status}{price_text}"
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