import argparse
import json
import os
import random
import re
import time
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests

try:
    from .marketplace_common import MarketplaceConfig, MarketplaceScraper
except ImportError:
    from marketplace_common import MarketplaceConfig, MarketplaceScraper


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "columbia_data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


CONFIG = {
    "BASE_URL": "https://www.columbiasportswear.co.in",
    "OUTPUT_FILE": str(DATA_DIR / "columbia_official_products.json"),
    "LOG_FILE": str(LOG_DIR / "columbia_scraper.log"),
    "PRODUCTS_PER_PAGE": 250,
    "MAX_PRODUCT_PAGES": 120,
    "CHECKPOINT_EVERY_UNIQUE_ITEMS": 25,
    "RETRY_LIMIT": 4,
    "REQUEST_TIMEOUT_SECONDS": 30,
    "PAGE_DELAY_SECONDS": 1.5,
    "DETAIL_DELAY_SECONDS": 1.0,
    "RETRY_BACKOFF_SECONDS": 8,
    "DETAIL_LOOKUP_FOR_MATERIAL": True,
    "REPAIR_EXISTING_MISSING_MATERIALS": True,
    "MAX_DETAIL_LOOKUPS": 0,
}


SEARCH_QUERIES = [
    "jackets",
    "shoes",
    "shirts",
    "pants",
    "shorts",
    "fleece",
    "hoodie",
    "vest",
    "boots",
    "caps",
    "gloves",
    "socks",
    "rain jacket",
    "hiking",
    "trekking",
    "trail running",
    "omni heat",
    "omni shade",
    "omni tech",
    "outdry",
    "pfg",
    "backpack",
    "thermal",
    "waterproof",
    "insulated",
]


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
]


COLUMBIA = MarketplaceConfig(
    name="columbia",
    base_url="https://www.columbiasportswear.co.in",
    search_urls=(
        "https://www.columbiasportswear.co.in/search?q={query}&type=product",
        "https://www.columbiasportswear.co.in/search?type=product&q={query}",
    ),
    search_input_selectors=(
        "input[type='search']",
        "input[name='q']",
        "input[placeholder*='Search']",
    ),
    search_open_selectors=(
        "button[aria-label*='Search']",
        "summary[aria-label*='Search']",
        "details-modal summary",
    ),
    search_submit_selectors=(
        "button[type='submit']",
        "form[action*='search'] button",
    ),
    first_product_selectors=(
        "a[href*='/products/']",
        ".card__heading a",
        ".product-item a",
    ),
    title_selectors=(
        "h1.product__title",
        "h1.product-single__title",
        "h1",
    ),
    price_selectors=(
        ".price-item--regular",
        ".price__regular .price-item",
        ".price-item",
        "[class*='price']",
    ),
    image_selectors=(
        ("meta[property='og:image']", "content"),
        ("img[loading='eager']", "src"),
        ("img", "src"),
    ),
    no_result_markers=(
        "No results found",
        "0 results",
    ),
    blocked_markers=(
        "Access denied",
        "captcha",
        "verify you are human",
    ),
)


class ColumbiaScraper(MarketplaceScraper):
    def __init__(self):
        super().__init__(COLUMBIA)


products_by_id = {}
last_checkpoint_unique_count = 0
stats = {
    "pages_visited": 0,
    "products_seen": 0,
    "unique_products": 0,
    "duplicates_skipped": 0,
    "search_pages_visited": 0,
    "search_links_found": 0,
    "detail_lookups": 0,
    "material_found": 0,
    "existing_materials_repaired": 0,
    "request_retries": 0,
    "pages_failed": 0,
    "checkpoints": 0,
}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        text = re.sub(r"\s+", " ", data or "").strip()
        if text:
            self.parts.append(text)

    def text(self):
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def timestamp():
    return datetime.now().replace(microsecond=0).isoformat()


def compact(value, max_length=100):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value if len(value) <= max_length else value[: max_length - 3] + "..."


def log(message, **details):
    now = datetime.now().strftime("%H:%M:%S")
    important_keys = [
        "page",
        "query",
        "products_found",
        "unique_added",
        "total_unique_products",
        "detail_lookups",
        "material_found",
        "existing_materials_repaired",
        "pages_visited",
        "pages_failed",
        "request_retries",
        "checkpoints",
        "reason",
        "url",
        "error",
    ]
    parts = []
    for key in important_keys:
        if key in details and details[key] not in (None, ""):
            parts.append(f"{key}={compact(details.pop(key))}")
    for key, value in details.items():
        if value not in (None, ""):
            parts.append(f"{key}={compact(value)}")
    line = f"{now} INFO  {message}"
    if parts:
        line += " | " + " ".join(parts)
    with Path(CONFIG["LOG_FILE"]).open("a", encoding="utf-8") as file:
        file.write(line + "\n")
    print(line, flush=True)


def strip_html(value):
    parser = TextExtractor()
    parser.feed(unescape(str(value or "")))
    return parser.text()


def normalize_price(value):
    if value in (None, ""):
        return ""
    try:
        number = float(str(value).replace(",", ""))
    except ValueError:
        return str(value).strip()
    return f"₹{number:,.2f}"


def price_value(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        match = re.search(r"[\d,]+(?:\.\d+)?", str(value))
        return float(match.group(0).replace(",", "")) if match else None


def normalize_material(value):
    value = re.sub(r"[^\w.%]+", " ", str(value or "").casefold())
    return re.sub(r"\s+", " ", value).strip()


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def product_url(handle):
    return f"{CONFIG['BASE_URL']}/products/{handle}"


def best_image(product):
    images = product.get("images") or []
    if images:
        return images[0].get("src") or ""
    image = product.get("image") or {}
    return image.get("src") or ""


def choose_variant(product):
    variants = product.get("variants") or []
    available = [variant for variant in variants if variant.get("available")]
    return (available or variants or [{}])[0]


def product_key(product):
    return str(product.get("id") or product.get("product_id") or "").strip()


def material_from_text(text):
    text = clean_text(unescape(text or ""))
    if not text:
        return ""

    snippets = []
    patterns = [
        r"(?:Material|Fabric|Composition)\s*:\s*([^|.]{3,260})",
        r"((?:Upper|Lining|Outsole|Shell|Filling)\s*:\s*[^|.]{3,260})",
        r"((?:\d+%\s*)+(?:Cotton|Polyester|Nylon|Rubber|Elastane|Spandex|Leather|Synthetic|Wool|Acrylic|Polyamide|Lycra|Modal|Linen|Rayon|Down|Mesh)[^|.]{0,220})",
        r"((?:breathable\s+)?(?:mesh|leather|synthetic|textile)\s+upper[^|.]{0,160})",
        r"((?:rubber|synthetic|adapt\s+trax[™]?)\s+outsole[^|.]{0,160})",
        r"((?:made|crafted)\s+(?:from|with)\s+[^|.]{0,220}(?:cotton|polyester|nylon|rubber|elastane|spandex|leather|synthetic|wool|acrylic|polyamide|lycra|modal|linen|rayon|down|mesh)[^|.]{0,80})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            candidate = clean_text(match.group(1))
            if not candidate:
                continue
            if re.search(r"review|rating|coupon|delivery|return|seller|wishlist|cart", candidate, re.IGNORECASE):
                continue
            if not re.search(
                r"material|fabric|composition|upper|lining|outsole|shell|filling|\d+%|cotton|polyester|nylon|rubber|elastane|spandex|leather|synthetic|wool|acrylic|polyamide|lycra|modal|linen|rayon|down|mesh",
                candidate,
                re.IGNORECASE,
            ):
                continue
            snippets.append(candidate.strip(" :-;,."))

    unique = []
    seen = set()
    for snippet in snippets:
        key = snippet.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(snippet)
    return "; ".join(unique[:5])


def material_from_html(html):
    html = unescape(html or "")
    candidates = []

    for match in re.finditer(
        r"<(?:th|dt|strong|b|span|div)[^>]*>\s*(Material|Fabric|Composition|Upper|Lining|Outsole|Shell|Filling)\s*</[^>]+>\s*<(?:td|dd|span|div|p|ul)[^>]*>(.*?)</(?:td|dd|span|div|p|ul)>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        label = strip_html(match.group(1))
        value = strip_html(match.group(2))
        if value:
            candidates.append(f"{label}: {value}")

    # Product features are commonly stored as separate list items. Parse each
    # item before flattening the page so adjacent features cannot run together.
    for match in re.finditer(r"<li\b[^>]*>(.*?)</li>", html, re.IGNORECASE | re.DOTALL):
        extracted = material_from_text(strip_html(match.group(1)))
        if extracted:
            candidates.append(extracted)

    if not candidates:
        extracted = material_from_text(strip_html(html))
        if extracted:
            candidates.append(extracted)

    unique = []
    seen = set()
    for candidate in candidates:
        candidate = clean_text(candidate).strip(" :-;,.")
        key = candidate.casefold()
        if candidate and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return "; ".join(unique[:5])


def make_session():
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "en-IN,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )
    return session


def fetch(session, url, expect_json=False):
    last_error = None
    for attempt in range(1, CONFIG["RETRY_LIMIT"] + 1):
        try:
            response = session.get(url, timeout=CONFIG["REQUEST_TIMEOUT_SECONDS"])
            if response.status_code in {403, 429, 500, 502, 503, 504}:
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            return response.json() if expect_json else response.text
        except Exception as error:
            last_error = error
            stats["request_retries"] += 1
            sleep_for = CONFIG["RETRY_BACKOFF_SECONDS"] * attempt + random.uniform(0.5, 2.5)
            log("Request failed; backing off", url=url, error=str(error), attempt=attempt, sleep_seconds=round(sleep_for, 2))
            time.sleep(sleep_for)
    stats["pages_failed"] += 1
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def product_from_shopify(product, detail_html=""):
    product_id = product_key(product)
    variant = choose_variant(product)
    price_raw = variant.get("price") or ""
    compare_raw = variant.get("compare_at_price") or ""
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",") if tag.strip()]

    body_text = strip_html(product.get("body_html", ""))
    material = material_from_html(detail_html) if detail_html else ""
    if not material:
        material = material_from_text(body_text)

    variants = product.get("variants") or []
    sizes = [variant.get("title") for variant in variants if variant.get("title")]
    skus = [variant.get("sku") for variant in variants if variant.get("sku")]
    barcodes = [variant.get("barcode") for variant in variants if variant.get("barcode")]

    return {
        "product_id": product_id,
        "brand": product.get("vendor") or "Columbia",
        "title": product.get("title") or "",
        "name": product.get("title") or "",
        "price": normalize_price(price_raw),
        "price_value": price_value(price_raw),
        "compare_at_price": normalize_price(compare_raw),
        "currency": variant.get("price_currency") or "INR",
        "sku": skus[0] if skus else "",
        "skus": skus,
        "barcodes": barcodes,
        "product_type": product.get("product_type") or "",
        "tags": tags,
        "sizes": sizes,
        "available": any(variant.get("available") for variant in variants),
        "description": body_text,
        "material_composition": material,
        "material_normalized": normalize_material(material),
        "image_url": best_image(product),
        "image_urls": [image.get("src") for image in product.get("images", []) if image.get("src")],
        "url": product_url(product.get("handle", "")),
        "handle": product.get("handle") or "",
        "published_at": product.get("published_at") or "",
        "updated_at": product.get("updated_at") or "",
        "scraped_at": timestamp(),
    }


def load_existing():
    path = Path(CONFIG["OUTPUT_FILE"])
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for product in data:
            key = product_key(product)
            if key:
                products_by_id[key] = product
        stats["unique_products"] = len(products_by_id)
        log("Loaded existing output file", total_unique_products=len(products_by_id))
    except Exception as error:
        log("Could not load existing output file; starting fresh", error=str(error))


def write_checkpoint(reason):
    path = Path(CONFIG["OUTPUT_FILE"])
    products = sorted(products_by_id.values(), key=lambda item: int(str(item.get("product_id") or "0")))
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(products, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp_path, path)
    stats["checkpoints"] += 1
    stats["unique_products"] = len(products_by_id)
    log("Wrote product checkpoint", reason=reason, total_unique_products=len(products_by_id), checkpoints=stats["checkpoints"])


def maybe_checkpoint():
    global last_checkpoint_unique_count
    if len(products_by_id) - last_checkpoint_unique_count >= CONFIG["CHECKPOINT_EVERY_UNIQUE_ITEMS"]:
        write_checkpoint(f"Reached {CONFIG['CHECKPOINT_EVERY_UNIQUE_ITEMS']} new unique products")
        last_checkpoint_unique_count = len(products_by_id)


def merge_product(incoming):
    key = product_key(incoming)
    if not key:
        return False
    existing = products_by_id.get(key)
    if not existing:
        products_by_id[key] = incoming
        return True

    stats["duplicates_skipped"] += 1
    for field, value in incoming.items():
        if existing.get(field) in (None, "", [], {}) and value not in (None, "", [], {}):
            existing[field] = value
    return False


def fetch_detail_product(session, handle):
    url = f"{CONFIG['BASE_URL']}/products/{handle}.json"
    data = fetch(session, url, expect_json=True)
    return data.get("product") or {}


def fetch_detail_html(session, handle):
    stats["detail_lookups"] += 1
    return fetch(session, product_url(handle), expect_json=False)


def scrape_products_json(session, max_pages):
    for page in range(1, max_pages + 1):
        url = f"{CONFIG['BASE_URL']}/products.json?limit={CONFIG['PRODUCTS_PER_PAGE']}&page={page}"
        data = fetch(session, url, expect_json=True)
        products = data.get("products") or []
        stats["pages_visited"] += 1
        stats["products_seen"] += len(products)
        if not products:
            log("Stopping product pagination because page returned no products", page=page, total_unique_products=len(products_by_id))
            break

        added = 0
        for product in products:
            handle = product.get("handle") or ""
            detail_html = ""
            if CONFIG["DETAIL_LOOKUP_FOR_MATERIAL"]:
                time.sleep(CONFIG["DETAIL_DELAY_SECONDS"] + random.uniform(0.1, 0.8))
                try:
                    detail_html = fetch_detail_html(session, handle)
                except Exception as error:
                    log("Detail page lookup failed", url=product_url(handle), error=str(error))

            normalized = product_from_shopify(product, detail_html=detail_html)
            if normalized["material_composition"]:
                stats["material_found"] += 1
            if merge_product(normalized):
                added += 1
            maybe_checkpoint()

        log("Finished products page", page=page, products_found=len(products), unique_added=added, total_unique_products=len(products_by_id))
        time.sleep(CONFIG["PAGE_DELAY_SECONDS"] + random.uniform(0.2, 1.0))


def discover_search_links(session):
    handles = set()
    for query in SEARCH_QUERIES:
        url = f"{CONFIG['BASE_URL']}/search?q={quote_plus(query)}"
        try:
            html = fetch(session, url, expect_json=False)
        except Exception as error:
            log("Search page lookup failed", query=query, url=url, error=str(error))
            continue
        stats["search_pages_visited"] += 1
        found = set(re.findall(r'href=["\'](?:https://www\.columbiasportswear\.co\.in)?/products/([^"\'?#]+)', html))
        handles.update(found)
        stats["search_links_found"] += len(found)
        log("Finished search discovery page", query=query, products_found=len(found), total_handles=len(handles))
        time.sleep(CONFIG["PAGE_DELAY_SECONDS"] + random.uniform(0.2, 1.0))
    return handles


def scrape_discovered_handles(session, handles):
    for index, handle in enumerate(sorted(handles), start=1):
        if any(product.get("handle") == handle for product in products_by_id.values()):
            continue
        try:
            product = fetch_detail_product(session, handle)
            detail_html = fetch_detail_html(session, handle) if CONFIG["DETAIL_LOOKUP_FOR_MATERIAL"] else ""
        except Exception as error:
            log("Discovered product lookup failed", url=product_url(handle), error=str(error))
            continue

        normalized = product_from_shopify(product, detail_html=detail_html)
        if normalized["material_composition"]:
            stats["material_found"] += 1
        merge_product(normalized)
        maybe_checkpoint()
        if index % 25 == 0:
            log("Processed discovered product handles", processed=index, total_handles=len(handles), total_unique_products=len(products_by_id))
        time.sleep(CONFIG["DETAIL_DELAY_SECONDS"] + random.uniform(0.2, 1.0))


def repair_existing_missing_materials(session):
    if not CONFIG["REPAIR_EXISTING_MISSING_MATERIALS"] or not CONFIG["DETAIL_LOOKUP_FOR_MATERIAL"]:
        return

    repaired = 0
    detail_limit = CONFIG["MAX_DETAIL_LOOKUPS"]
    missing = [
        product
        for product in products_by_id.values()
        if not product.get("material_composition") and product.get("handle")
    ]
    if detail_limit > 0:
        missing = missing[:detail_limit]

    log("Queued existing missing material repairs", missing_materials=len(missing))
    for index, product in enumerate(missing, start=1):
        try:
            detail_html = fetch_detail_html(session, product["handle"])
        except Exception as error:
            log("Material repair detail lookup failed", url=product.get("url", ""), error=str(error))
            continue

        material = material_from_html(detail_html)
        if material:
            product["material_composition"] = material
            product["material_normalized"] = normalize_material(material)
            product["scraped_at"] = timestamp()
            stats["existing_materials_repaired"] += 1
            stats["material_found"] += 1
            repaired += 1
        if repaired and repaired % CONFIG["CHECKPOINT_EVERY_UNIQUE_ITEMS"] == 0:
            write_checkpoint("Repaired missing material fields")
        if index % 50 == 0:
            log("Processed material repair batch", processed=index, existing_materials_repaired=repaired)
        time.sleep(CONFIG["DETAIL_DELAY_SECONDS"] + random.uniform(0.2, 1.0))


def parse_args():
    parser = argparse.ArgumentParser(description="Scrape official Columbia India Shopify products.")
    parser.add_argument("--max-pages", type=int, default=CONFIG["MAX_PRODUCT_PAGES"], help="Max /products.json pages to scan")
    parser.add_argument("--output", default=CONFIG["OUTPUT_FILE"], help="Output JSON path")
    parser.add_argument("--no-detail", action="store_true", help="Skip PDP HTML material enrichment")
    parser.add_argument("--no-search-discovery", action="store_true", help="Skip search-query discovery pages")
    parser.add_argument("--max-detail-lookups", type=int, default=CONFIG["MAX_DETAIL_LOOKUPS"], help="0 means no cap")
    return parser.parse_args()


def main():
    args = parse_args()
    CONFIG["OUTPUT_FILE"] = args.output
    CONFIG["DETAIL_LOOKUP_FOR_MATERIAL"] = not args.no_detail
    if args.no_detail:
        CONFIG["REPAIR_EXISTING_MISSING_MATERIALS"] = False
    CONFIG["MAX_DETAIL_LOOKUPS"] = args.max_detail_lookups
    Path(CONFIG["LOG_FILE"]).write_text("", encoding="utf-8")

    session = make_session()
    load_existing()
    repair_existing_missing_materials(session)
    scrape_products_json(session, max_pages=args.max_pages)
    if not args.no_search_discovery:
        handles = discover_search_links(session)
        scrape_discovered_handles(session, handles)
    write_checkpoint("Final save")
    log("Scraper finished", **stats)


if __name__ == "__main__":
    main()