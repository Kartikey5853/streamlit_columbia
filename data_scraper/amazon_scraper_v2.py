"""
amazon_scraper.py  —  Multi-query scraper modeled after Myntra approach
────────────────────────────────────────────────────────────────────────
Each query gets its own fresh browser session and pages through Amazon's
7-page limit. Global ASIN dedup prevents duplicates across queries.
Empty-streak logic stops a query early if pages keep returning nothing new.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import random
import logging
import subprocess
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────
# SEARCH QUERIES
# Amazon caps browsable pages per search at ~7 (~420 products).
# Each query below gets its own fresh session and window of pages.
# Global ASIN dedup prevents duplicates across queries.
#
# Format: (keyword, department_param, display_label)
# URL built as: amazon.in/s?k=<keyword>&rh=p_123:232621<dept>&page=<n>
# ─────────────────────────────────────────

SEARCH_QUERIES = [
    # keyword                   department param            label
    ("columbia",                "",                         "Columbia All"),
    ("columbia jacket",         "&i=apparel-store",         "Columbia Jackets"),
    ("columbia jacket",         "&i=sporting-store",        "Columbia Jackets Sport"),
    ("columbia shoes",          "&i=shoes",                 "Columbia Shoes"),
    ("columbia shoes",          "&i=sporting-store",        "Columbia Shoes Sport"),
    ("columbia shirt",          "&i=apparel-store",         "Columbia Shirts"),
    ("columbia pants",          "&i=apparel-store",         "Columbia Pants"),
    ("columbia fleece",         "&i=apparel-store",         "Columbia Fleece"),
    ("columbia fleece",         "&i=sporting-store",        "Columbia Fleece Sport"),
    ("columbia shorts",         "&i=apparel-store",         "Columbia Shorts"),
    ("columbia cap",            "&i=apparel-store",         "Columbia Caps"),
    ("columbia cap",            "&i=sporting-store",        "Columbia Caps Sport"),
    ("columbia hoodie",         "&i=apparel-store",         "Columbia Hoodies"),
    ("columbia vest",           "&i=apparel-store",         "Columbia Vests"),
    ("columbia boots",          "&i=shoes",                 "Columbia Boots"),
    ("columbia gloves",         "&i=sporting-store",        "Columbia Gloves"),
    ("columbia socks",          "&i=apparel-store",         "Columbia Socks"),
    ("columbia rain jacket",    "&i=apparel-store",         "Columbia Rain Jackets"),
    ("columbia rain jacket",    "&i=sporting-store",        "Columbia Rain Jackets Sport"),
    ("columbia hiking",         "&i=sporting-store",        "Columbia Hiking"),
    ("columbia trekking",       "&i=sporting-store",        "Columbia Trekking"),
    ("columbia men",            "&i=apparel-store",         "Columbia Men Apparel"),
    ("columbia men",            "&i=shoes",                 "Columbia Men Shoes"),
    ("columbia women",          "&i=apparel-store",         "Columbia Women Apparel"),
    ("columbia women",          "&i=shoes",                 "Columbia Women Shoes"),
    ("columbia omni heat",      "",                         "Columbia Omni-Heat"),
    ("columbia omni shade",     "",                         "Columbia Omni-Shade"),
    ("columbia omni tech",      "",                         "Columbia Omni-Tech"),
    ("columbia outdry",         "",                         "Columbia OutDry"),
    ("columbia pfg",            "",                         "Columbia PFG"),
    ("columbia backpack",       "",                         "Columbia Backpacks"),
    ("columbia beanie",         "&i=apparel-store",         "Columbia Beanies"),
    ("columbia thermal",        "&i=apparel-store",         "Columbia Thermals"),
    ("columbia softshell",      "&i=sporting-store",        "Columbia Softshell"),
    ("columbia waterproof",     "&i=sporting-store",        "Columbia Waterproof"),
    ("columbia insulated",      "&i=apparel-store",         "Columbia Insulated"),
]

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

BASE_DIR         = Path(__file__).resolve().parents[1]
DATA_DIR         = BASE_DIR / "data" / "json" / "amazon"
LOG_DIR          = BASE_DIR / "logs" / "amazon"
EMBEDDINGS_DIR   = BASE_DIR / "data" / "embeddings"

JSON_FILE        = Path(os.environ.get("CPI_OUTPUT", DATA_DIR / "latest_amazon.json"))
LOG_FILE         = Path(os.environ.get("CPI_LOG", LOG_DIR / "latest_amazon.log"))
INDEX_LOG_FILE   = LOG_DIR / "amazon_indexer.log"
INDEX_FILE       = EMBEDDINGS_DIR / "embeddings_amazon.index"
METADATA_FILE    = EMBEDDINGS_DIR / "metadata_amazon.pkl"
TEXT_INDEX_FILE  = EMBEDDINGS_DIR / "embeddings_amazon_text.index"
TEXT_METADATA_FILE = EMBEDDINGS_DIR / "metadata_amazon_text.pkl"
CHECKPOINT_SIZE  = 1
HEADLESS         = False

BRAND_FILTER     = "&rh=p_123%3A232621"   # locks results to Columbia brand only
PAGES_PER_QUERY  = 7                      # Amazon's hard cap
MAX_EMPTY_PAGES  = 2                      # consecutive pages with 0 new → skip to next query
MAX_PAGE_RETRIES = 2                      # retries per page on timeout/block
DETAIL_TIMEOUT   = 20000

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
)
log = logging.getLogger("columbia")

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def load_existing() -> dict:
    if JSON_FILE.exists():
        with open(JSON_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("products"), dict):
            products = data["products"].values()
        elif isinstance(data, list):
            products = data
        else:
            products = []
        return {
            clean_upc(product.get("upc")): product
            for product in products
            if clean_upc(product.get("upc")) and has_required_fields(product)
        }
    return {}


def save_products(products: dict, pending_count: int, checkpoint_size: int, force: bool = False) -> int:
    if not force and pending_count < checkpoint_size:
        return pending_count

    payload = {
        "schema_version": 2,
        "primary_key": "upc",
        "updated_at": datetime.now().isoformat(),
        "products": products,
    }
    temp_file = JSON_FILE.with_suffix(".json.tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    temp_file.replace(JSON_FILE)

    log.info("Saved %s total products to %s", len(products), JSON_FILE)
    return 0


def start_indexer(products_path: Path, batch_size: int) -> subprocess.Popen | None:
    script_path = BASE_DIR / "build_index.py"
    if not script_path.exists():
        log.warning("Indexer not found at %s", script_path)
        return None

    args = [
        sys.executable,
        str(script_path),
        "--watch",
        "--products",
        str(products_path),
        "--index",
        str(INDEX_FILE),
        "--metadata",
        str(METADATA_FILE),
        "--text-index",
        str(TEXT_INDEX_FILE),
        "--text-metadata",
        str(TEXT_METADATA_FILE),
        "--batch-size",
        str(batch_size),
        "--log",
        str(INDEX_LOG_FILE),
    ]

    log.info("Starting indexer: %s", " ".join(args))
    return subprocess.Popen(args, cwd=str(BASE_DIR))


def clean_price(raw: str) -> str | None:
    if not raw:
        return None
    m = re.search(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)", raw, re.I)
    return f"₹{m.group(1)}" if m else None


def price_value(raw: str | None) -> float | None:
    if not raw:
        return None
    match = re.search(r"[\d,]+(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        return float(Decimal(match.group(0).replace(",", "")))
    except InvalidOperation:
        return None


def clean_upc(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12:
        return digits
    if len(digits) == 13 and digits.startswith("0"):
        return digits[1:]
    return None


def has_required_fields(product: dict) -> bool:
    return all([
        clean_upc(product.get("upc")),
        product.get("name"),
        product.get("price"),
        product.get("material") or product.get("material_composition"),
    ])


def compact_product(product: dict) -> dict:
    material = product.get("material") or product.get("material_composition")
    return {
        "upc": clean_upc(product.get("upc")),
        "title": product.get("name") or product.get("title"),
        "price": product.get("price"),
        "price_value": price_value(product.get("price")),
        "currency": "INR",
        "material_composition": material,
        "material_normalized": normalize_material(material),
        "image_url": product.get("image_url"),
        "url": product.get("url"),
        "scraped_at": product.get("detail_scraped_at") or datetime.now().isoformat(),
    }


def first_non_empty(*values):
    for value in values:
        if isinstance(value, str):
            value = re.sub(r"\s+", " ", value).strip()
        if value:
            return value
    return None


def find_detail_value(details: dict, labels: tuple[str, ...]) -> str | None:
    for key, value in details.items():
        normalized_key = re.sub(r"[^a-z0-9]+", " ", key.casefold()).strip()
        if any(label in normalized_key for label in labels):
            return value
    return None


def flatten_json_ld(value) -> list[dict]:
    found = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(flatten_json_ld(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(flatten_json_ld(child))
    return found


def extract_upc_from_details(details: dict, page_text: str, json_ld: list[dict]) -> str | None:
    candidates = [find_detail_value(details, ("upc", "gtin 12", "gtin12"))]
    for item in json_ld:
        candidates.extend([item.get("gtin12"), item.get("gtin13"), item.get("gtin")])
    match = re.search(r"\b(?:UPC|GTIN[- ]?12)\s*[:\-]?\s*(\d{12,13})\b", page_text, re.I)
    if match:
        candidates.append(match.group(1))
    for candidate in candidates:
        upc = clean_upc(str(candidate)) if candidate else None
        if upc:
            return upc
    return None


def extract_material(details: dict, bullets: list[str], description: str | None) -> str | None:
    material = find_detail_value(
        details,
        ("material", "fabric type", "fabric composition", "outer material"),
    )
    if material:
        return material
    text = " ".join([*bullets, description or ""])
    match = re.search(
        r"(?:material composition|fabric composition|fabric type|material type|outer material)"
        r"\s*[:\-]?\s*(.{2,120}?)(?=\s+(?:pattern|fit type|sleeve type|collar style|"
        r"care instructions|product care|fabric stretchability|apparel fabric|about this item)\b|[.;|]|$)",
        text,
        re.I | re.S,
    )
    return match.group(1).strip() if match else None


def normalize_material(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.casefold()
    replacements = {
        "polyamide": "nylon",
        "elastane": "spandex",
        "poly urethane": "polyurethane",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    value = re.sub(r"[^a-z0-9%]+", " ", value)
    return re.sub(r"\s+", " ", value).strip() or None


def build_url(keyword: str, dept_param: str, page: int) -> str:
    k = keyword.replace(" ", "+")
    return (
        f"https://www.amazon.in/s?k={k}"
        f"{BRAND_FILTER}"
        f"{dept_param}"
        f"&page={page}"
    )


async def full_scroll(page):
    for _ in range(12):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.75)")
        await page.wait_for_timeout(200)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(600)

# ─────────────────────────────────────────
# EXTRACT TILES FROM CURRENT PAGE
# ─────────────────────────────────────────

async def extract_tiles(page, label: str, seen_asins: set, seen_lock: asyncio.Lock) -> list:
    tiles = await page.query_selector_all(
        "div[data-asin][data-component-type='s-search-result']"
    )

    products = []
    for tile in tiles:
        asin = await tile.get_attribute("data-asin")
        if not asin:
            continue
        async with seen_lock:
            if asin in seen_asins:
                continue
            seen_asins.add(asin)

        # ── Name ──
        name = None
        candidates = await tile.eval_on_selector_all(
            "h2 span, [data-cy='title-recipe'] span, h2 a span",
            "els => els.map(e => e.textContent.trim()).filter(t => t.length > 15)"
        )
        if candidates:
            name = candidates[0]
        if not name:
            continue

        # ── Price ──
        price = None
        for sel in [".a-price .a-offscreen", ".a-price-whole",
                    "span[data-a-color='price'] .a-offscreen"]:
            el = await tile.query_selector(sel)
            if el:
                price = clean_price((await el.inner_text()).strip())
                if price:
                    break

        # ── Image ──
        img_url = None
        img_el = await tile.query_selector("img.s-image")
        if img_el:
            img_url = (
                await img_el.get_attribute("src") or
                await img_el.get_attribute("data-src")
            )

        products.append({
            "asin":       asin,
            "upc":        None,
            "name":       name,
            "price":      price,
            "price_value": price_value(price),
            "currency":   "INR" if price else None,
            "material":   None,
            "material_normalized": None,
            "description": None,
            "features":   [],
            "url":        f"https://www.amazon.in/dp/{asin}",
            "image_url":  img_url,
            "source":     "amazon.in",
            "extraction_status": {
                "upc": "pending",
                "material": "pending",
                "price": "search_tile" if price else "pending",
            },
            "scraped_at": datetime.now().isoformat(),
        })
        log.info(f"  [{label}] OK  {name[:60]:<60}  |  {price or 'N/A'}")

    return products


async def enrich_product(context, product: dict, detail_semaphore: asyncio.Semaphore) -> dict:
    async with detail_semaphore:
        page = await context.new_page()
        try:
            await page.goto(
                product["url"],
                wait_until="domcontentloaded",
                timeout=DETAIL_TIMEOUT,
            )
            await page.wait_for_timeout(random.randint(300, 700))
            extracted = await page.evaluate(
                """
                () => {
                    const clean = value => (value || "").replace(/\\s+/g, " ").trim();
                    const details = {};
                    document.querySelectorAll(
                        "#productDetails_detailBullets_sections1 tr, " +
                        "#productDetails_techSpec_section_1 tr, " +
                        "#productDetails_techSpec_section_2 tr, " +
                        "#productOverview_feature_div tr, " +
                        "#technicalSpecifications_section_1 tr, " +
                        "#productDetails_db_sections tr"
                    ).forEach(row => {
                        const cells = [...row.querySelectorAll("td")];
                        const key = clean(
                            row.querySelector("th")?.textContent ||
                            row.querySelector("td.a-span3")?.textContent ||
                            cells.at(0)?.textContent
                        );
                        const value = clean(
                            row.querySelector("td:not(.a-span3)")?.textContent ||
                            cells.at(-1)?.textContent
                        );
                        if (key && value) details[key] = value;
                    });
                    document.querySelectorAll(
                        "#productFactsDesktopExpander .product-facts-detail, " +
                        "#productFactsDesktopExpander .a-fixed-left-grid"
                    ).forEach(row => {
                        const key = clean(
                            row.querySelector(".a-col-left")?.textContent ||
                            row.querySelector("span.a-color-base")?.textContent
                        );
                        const value = clean(
                            row.querySelector(".a-col-right")?.textContent
                        );
                        if (key && value && key !== value) details[key] = value;
                    });
                    document.querySelectorAll(
                        "#detailBullets_feature_div li, #detailBulletsWrapper_feature_div li"
                    ).forEach(row => {
                        const text = clean(row.textContent);
                        const split = text.split(":");
                        if (split.length > 1) {
                            const key = clean(split.shift());
                            const value = clean(split.join(":"));
                            if (key && value) details[key] = value;
                        }
                    });
                    const features = [...document.querySelectorAll("#feature-bullets li span.a-list-item")]
                        .map(node => clean(node.textContent))
                        .filter(Boolean);
                    const jsonLd = [...document.querySelectorAll('script[type="application/ld+json"]')]
                        .map(node => {
                            try { return JSON.parse(node.textContent); }
                            catch (_) { return null; }
                        })
                        .filter(Boolean);
                    return {
                        title: clean(document.querySelector("#productTitle")?.textContent),
                        price: clean(
                            document.querySelector("#corePrice_feature_div .a-offscreen")?.textContent ||
                            document.querySelector("#apex_desktop .a-offscreen")?.textContent ||
                            document.querySelector(".a-price .a-offscreen")?.textContent
                        ),
                        imageUrl:
                            document.querySelector("#landingImage")?.getAttribute("data-old-hires") ||
                            document.querySelector("#landingImage")?.getAttribute("src"),
                        description: clean(document.querySelector("#productDescription")?.textContent),
                        features,
                        details,
                        jsonLd,
                        pageText: clean(document.body?.textContent),
                    };
                }
                """
            )

            json_ld = []
            for value in extracted.get("jsonLd", []):
                json_ld.extend(flatten_json_ld(value))
            details = extracted.get("details", {})
            features = extracted.get("features", [])
            description = first_non_empty(
                extracted.get("description"),
                " ".join(features),
            )
            detail_price = clean_price(extracted.get("price", ""))
            upc = extract_upc_from_details(
                details,
                extracted.get("pageText", ""),
                json_ld,
            )
            material = extract_material(
                details,
                features,
                first_non_empty(extracted.get("pageText"), description),
            )

            product.update({
                "upc": upc,
                "name": first_non_empty(extracted.get("title"), product.get("name")),
                "price": first_non_empty(detail_price, product.get("price")),
                "material": material,
                "material_normalized": normalize_material(material),
                "description": description,
                "features": features,
                "details": details,
                "image_url": first_non_empty(extracted.get("imageUrl"), product.get("image_url")),
                "detail_scraped_at": datetime.now().isoformat(),
            })
            product["price_value"] = price_value(product.get("price"))
            product["currency"] = "INR" if product.get("price_value") is not None else None
            product["extraction_status"] = {
                "upc": "detail_page" if upc else "missing",
                "material": "detail_page" if material else "missing",
                "price": "detail_page" if detail_price else (
                    "search_tile" if product.get("price") else "missing"
                ),
            }
        except Exception as exc:
            product["detail_error"] = str(exc)
            product["extraction_status"] = {
                "upc": "error",
                "material": "error",
                "price": "search_tile" if product.get("price") else "error",
            }
            log.warning("  Detail failed for %s: %s", product["asin"], exc)
        finally:
            await page.close()
    return product

# ─────────────────────────────────────────
# SCRAPE ONE QUERY — pages through all 7 pages
# ─────────────────────────────────────────

async def scrape_query(
    browser,
    keyword: str,
    dept_param: str,
    label: str,
    all_products: dict,
    seen_asins: set,
    save_state: dict,
    seen_lock: asyncio.Lock,
    save_lock: asyncio.Lock,
    detail_semaphore: asyncio.Semaphore,
    pages_per_query: int,
) -> int:
    new_count   = 0
    empty_pages = 0   # consecutive pages with 0 new products

    # fresh context per query — new fingerprint, new session
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={"width": random.randint(1366, 1920), "height": random.randint(768, 1080)},
        locale="en-IN",
        extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
    )
    page = await context.new_page()

    try:
        for pg in range(1, pages_per_query + 1):
            url = build_url(keyword, dept_param, pg)
            log.info(f"  [{label}] Page {pg}/{pages_per_query}")

            # retry loop per page
            page_success = False
            for attempt in range(1, MAX_PAGE_RETRIES + 1):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                    # check if results exist
                    try:
                        await page.wait_for_selector(
                            "div[data-asin][data-component-type='s-search-result']",
                            timeout=8000
                        )
                    except PlaywrightTimeout:
                        log.info(f"  [{label}] No results on page {pg}.")
                        empty_pages += 1
                        page_success = True   # not a timeout, just no results
                        break

                    await page.wait_for_timeout(random.randint(800, 1500))
                    await full_scroll(page)

                    tile_products = await extract_tiles(page, label, seen_asins, seen_lock)
                    page_added = 0
                    if tile_products:
                        detail_tasks = [
                            enrich_product(context, product, detail_semaphore)
                            for product in tile_products
                        ]
                        for completed in asyncio.as_completed(detail_tasks):
                            product = await completed
                            if not has_required_fields(product):
                                missing = [
                                    field for field, value in {
                                        "upc": product.get("upc"),
                                        "title": product.get("name"),
                                        "price": product.get("price"),
                                        "material_composition": product.get("material"),
                                    }.items()
                                    if not value
                                ]
                                log.warning(
                                    "  [%s] SKIP %s - missing required: %s",
                                    label,
                                    product.get("asin"),
                                    ", ".join(missing),
                                )
                                continue
                            stored_product = compact_product(product)
                            async with save_lock:
                                all_products[stored_product["upc"]] = stored_product
                                new_count += 1
                                page_added += 1
                                save_state["pending"] += 1
                                save_state["pending"] = save_products(
                                    all_products,
                                    save_state["pending"],
                                    save_state["checkpoint"],
                                )

                    if not page_added:
                        empty_pages += 1
                        log.info(f"  [{label}] Page {pg} - 0 new products (empty streak: {empty_pages}/{MAX_EMPTY_PAGES})")
                    else:
                        empty_pages = 0   # reset streak on success
                        log.info(f"  [{label}] Page {pg} - +{page_added} new  |  {len(all_products)} total unique")

                    page_success = True
                    break

                except PlaywrightTimeout:
                    log.warning(f"  [{label}] Page {pg} attempt {attempt} timed out")
                    if attempt < MAX_PAGE_RETRIES:
                        await asyncio.sleep(3)
                except Exception as e:
                    log.error(f"  [{label}] Page {pg} attempt {attempt} error: {e}")
                    if attempt < MAX_PAGE_RETRIES:
                        await asyncio.sleep(3)

            if not page_success:
                log.warning(f"  [{label}] Page {pg} failed all retries - stopping query.")
                break

            if empty_pages >= MAX_EMPTY_PAGES:
                log.info(f"  [{label}] {MAX_EMPTY_PAGES} consecutive empty pages - moving to next query.")
                break

            # human delay between pages
            await asyncio.sleep(random.uniform(1.5, 3.0))

    finally:
        await context.close()

    return new_count

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Columbia products from Amazon.")
    parser.add_argument("--headless", action="store_true", help="Run browser invisibly")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="Number of queries to run in parallel")
    parser.add_argument("--checkpoint", type=int, default=CHECKPOINT_SIZE,
                        help="Save JSON after N new products")
    parser.add_argument("--detail-concurrency", type=int, default=4,
                        help="Maximum Amazon product detail pages open at once")
    parser.add_argument("--query-limit", type=int, default=None,
                        help="Only run the first N search queries")
    parser.add_argument("--pages-per-query", type=int, default=PAGES_PER_QUERY,
                        help="Maximum result pages to scrape for each query")
    parser.add_argument("--no-indexer", action="store_true",
                        help="Disable parallel embedding indexer")
    return parser.parse_args()


async def main():
    args = parse_args()
    start        = time.time()
    all_products = load_existing()
    seen_asins   = {
        product.get("asin")
        for product in all_products.values()
        if product.get("asin")
    }
    save_state   = {"pending": 0, "checkpoint": args.checkpoint}
    seen_lock    = asyncio.Lock()
    save_lock    = asyncio.Lock()
    detail_semaphore = asyncio.Semaphore(max(1, args.detail_concurrency))

    indexer_proc = None
    if not args.no_indexer:
        indexer_proc = start_indexer(JSON_FILE, args.checkpoint)

    if all_products:
        log.info(f"Loaded {len(all_products)} existing products - skipping duplicates.")

    search_queries = SEARCH_QUERIES[:args.query_limit] if args.query_limit else SEARCH_QUERIES
    pages_per_query = max(1, min(args.pages_per_query, PAGES_PER_QUERY))
    total_queries = len(search_queries)
    log.info("=" * 62)
    log.info("  Columbia Amazon Scraper - Multi-query mode")
    log.info(f"  Queries       : {total_queries}")
    log.info(f"  Pages/query   : {pages_per_query}")
    log.info(f"  Max slots     : {total_queries * pages_per_query * 60} (before dedup)")
    log.info("=" * 62)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=args.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        semaphore = asyncio.Semaphore(max(1, args.concurrency))

        async def run_query(index, query):
            keyword, dept_param, label = query
            async with semaphore:
                log.info(f"\n{'='*62}")
                log.info(f"  Query {index}/{total_queries}: {label}")
                log.info(f"  Unique products so far: {len(all_products)}")
                log.info(f"{'='*62}")

                new = await scrape_query(
                    browser, keyword, dept_param, label,
                    all_products, seen_asins, save_state,
                    seen_lock, save_lock, detail_semaphore, pages_per_query
                )

                log.info(f"  OK '{label}' done - {new} new products")
                await asyncio.sleep(random.uniform(2.0, 5.0))

        tasks = [
            asyncio.create_task(run_query(i, query))
            for i, query in enumerate(search_queries, 1)
        ]
        await asyncio.gather(*tasks)

        await browser.close()

    save_products(all_products, save_state["pending"], save_state["checkpoint"], force=True)
    elapsed = time.time() - start
    log.info("\n" + "=" * 62)
    log.info("OK  All done!")
    log.info(f"   Unique products  : {len(all_products)}")
    log.info(f"   Time             : {elapsed:.1f}s  ({elapsed/60:.1f} min)")
    log.info(f"   JSON             : {JSON_FILE}")
    log.info("=" * 62)

    if indexer_proc and indexer_proc.poll() is None:
        log.info("Indexer still running in background (PID %s).", indexer_proc.pid)


if __name__ == "__main__":
    asyncio.run(main())
