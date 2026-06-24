"""
Tata CLiQ Product Scraper
=========================
Scrapes products from a Tata CLiQ search URL and saves them to JSON.

Install:
    pip install playwright
    playwright install chromium

Run:
    python tata_scraper.py --limit 2900
    python tata_scraper.py --limit 2900 --headless
    python tata_scraper.py --url "https://www.tatacliq.com/search/?q=..." --output products.json
"""

import argparse
import asyncio
import json
import os
import re
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


DEFAULT_SEARCH_URL = (
    "https://www.tatacliq.com/search/"
    "?q=columbia%3Arelevance"
    "%3Abrand%3AMBH11B13628%3Abrand%3AMBH13B13628%3Abrand%3AMBH16B13628"
)

DEFAULT_LIMIT = 2900
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data" / "json" / "tatacliq"
LOG_DIR = BASE_DIR / "logs" / "tatacliq"
EMBEDDINGS_DIR = BASE_DIR / "data" / "embeddings"
DEFAULT_OUTPUT = os.environ.get("CPI_OUTPUT", str(DATA_DIR / "latest_tatacliq.json"))
LOG_FILE = Path(os.environ.get("CPI_LOG", LOG_DIR / "latest_tatacliq.log"))
INDEX_FILE = EMBEDDINGS_DIR / "embeddings_tatacliq.index"
METADATA_FILE = EMBEDDINGS_DIR / "metadata_tatacliq.pkl"
CHECKPOINT_SIZE = 25
BASE_URL = "https://www.tatacliq.com"
IMAGE_LOOKUP_CONCURRENCY = 4

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("tatacliq")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    return urljoin(BASE_URL, url)


def normalize_image_url(image_url: Any) -> str | None:
    image_url = clean_text(image_url)
    if not image_url:
        return None

    image_url = image_url.replace("\\/", "/").replace("&amp;", "&").strip("\"'")
    if image_url.startswith("//"):
        image_url = "https:" + image_url
    image_url = absolute_url(image_url)
    if not image_url:
        return None
    if image_url.startswith("http://"):
        image_url = "https://" + image_url.removeprefix("http://")

    lowered = image_url.lower()
    if (
        lowered.startswith("data:")
        or lowered.endswith(".svg")
        or any(token in lowered for token in ["placeholder", "blank", "transparent", "sprite", "logo"])
    ):
        return None

    if not any(token in lowered for token in ["tatacliq", "cliq", "/images/", "/image/", ".jpg", ".jpeg", ".png", ".webp"]):
        return None

    return image_url


def extract_product_id(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/p-([A-Z0-9]+)", url, re.IGNORECASE)
    return m.group(1) if m else None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def first_value(obj: dict, keys: list[str]) -> Any:
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def normalize_product_key(value: Any) -> str | None:
    value = clean_text(value)
    return value.casefold() if value else None


def product_key(product: dict) -> str | None:
    key = product.get("product_id") or extract_product_id(product.get("url")) or product.get("url") or product.get("name")
    return normalize_product_key(key)


def is_real_tatacliq_product(product: dict, require_image: bool = False) -> bool:
    product_id = clean_text(product.get("product_id")) or extract_product_id(product.get("url"))
    url = clean_text(product.get("url"))
    name = clean_text(product.get("name"))

    if not product_id or not re.match(r"^mp\d+$", product_id, re.IGNORECASE):
        return False
    if not url or "/p-" not in url.lower():
        return False
    if not name or name.casefold() in {"columbia", "new arrivals", "discounts", "popularity", "price low to high", "price high to low"}:
        return False
    if require_image and not has_good_image_url(product.get("image_url")):
        return False

    product["product_id"] = product_id.upper()
    product["url"] = absolute_url(url)
    product["image_url"] = normalize_image_url(product.get("image_url"))
    return True


def load_existing_products(output_path: Path) -> list[dict]:
    if output_path.exists():
        with open(output_path, encoding="utf-8") as f:
            return json.load(f)
    return []


def has_good_image_url(value: Any) -> bool:
    return normalize_image_url(value) is not None


def merge_product_record(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)

    for key, value in incoming.items():
        if key == "image_url":
            normalized = normalize_image_url(value)
            if normalized and not has_good_image_url(merged.get("image_url")):
                merged[key] = normalized
            continue

        if value not in (None, "") and merged.get(key) in (None, ""):
            merged[key] = value

    return merged


def merge_products(existing: list[dict], incoming: list[dict]) -> list[dict]:
    combined_by_key = {}
    order = []

    for product in existing + incoming:
        if not is_real_tatacliq_product(product):
            continue

        key = product_key(product)
        if not key:
            continue

        if key in combined_by_key:
            combined_by_key[key] = merge_product_record(combined_by_key[key], product)
            continue

        order.append(key)
        combined_by_key[key] = product

    return [combined_by_key[key] for key in order]


def checkpoint_save(output_path: Path, incoming: list[dict], pending: int, checkpoint_size: int, force: bool = False) -> int:
    if not force and pending < checkpoint_size:
        return pending

    existing = load_existing_products(output_path)
    merged = merge_products(existing, incoming)
    merged = [product for product in merged if is_real_tatacliq_product(product, require_image=True)]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Saved %s products to %s", len(merged), output_path)
    return 0


def start_indexer(products_path: Path, batch_size: int) -> subprocess.Popen | None:
    script_path = BASE_DIR / "build_index.py"
    if not script_path.exists():
        log.warning("Indexer not found at %s", script_path)
        return None

    log_file = LOG_DIR / "tatacliq_indexer.log"
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
        "--batch-size",
        str(batch_size),
        "--log",
        str(log_file),
    ]

    log.info("Starting indexer: %s", " ".join(args))
    return subprocess.Popen(args, cwd=str(BASE_DIR))


def extract_price(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return f"₹{value:g}"

    if isinstance(value, dict):
        for key in [
            "sellingPrice",
            "selling_price",
            "offerPrice",
            "offer_price",
            "price",
            "mrp",
            "value",
        ]:
            found = first_value(value, [key])
            if found is not None:
                price = extract_price(found)
                if price:
                    return price
        return None

    if isinstance(value, list):
        for item in value:
            price = extract_price(item)
            if price:
                return price
        return None

    text = clean_text(value)
    if not text:
        return None

    m = re.search(r"(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d+)?", text, re.IGNORECASE)
    if m:
        return m.group(0).replace("Rs.", "₹").replace("Rs", "₹").replace("INR", "₹").strip()

    m = re.search(r"\b[\d,]{3,}(?:\.\d+)?\b", text)
    if m:
        return f"₹{m.group(0)}"

    return None


def extract_image(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, str):
        if "," in value and re.search(r"\s+\d+(?:w|x)\b", value):
            parts = [item.strip().split()[0] for item in value.split(",")]
            for part in reversed(parts):
                image = normalize_image_url(part)
                if image:
                    return image
            return None

        return normalize_image_url(value)

    if isinstance(value, dict):
        for key in [
            "url",
            "src",
            "srcset",
            "img",
            "imageUrl",
            "imageURL",
            "image_url",
            "image",
            "thumbnail",
            "thumbnailUrl",
            "thumbnail_url",
            "baseUrl",
            "baseURL",
            "productImage",
            "productImageUrl",
            "product_image",
            "defaultImage",
            "mainImage",
            "largeImage",
            "mediumImage",
            "smallImage",
            "plpImage",
            "pdpImage",
            "galleryImage",
        ]:
            found = first_value(value, [key])
            image = extract_image(found)
            if image:
                return image
        return None

    if isinstance(value, list):
        for item in value:
            image = extract_image(item)
            if image:
                return image
        return None

    return None


def normalize_product(raw: dict) -> dict | None:
    url = first_value(
        raw,
        [
            "url",
            "productUrl",
            "productURL",
            "product_url",
            "pdpUrl",
            "pdpURL",
            "webURL",
            "webUrl",
            "link",
            "href",
        ],
    )

    url = absolute_url(clean_text(url))

    product_id = (
        clean_text(
            first_value(
                raw,
                [
                    "productId",
                    "productID",
                    "product_id",
                    "code",
                    "skuId",
                    "skuID",
                    "articleNumber",
                    "id",
                ],
            )
        )
        or extract_product_id(url)
    )

    name = clean_text(
        first_value(
            raw,
            [
                "productName",
                "product_name",
                "name",
                "title",
                "displayName",
                "brandName",
            ],
        )
    )

    price = extract_price(
        first_value(
            raw,
            [
                "sellingPrice",
                "selling_price",
                "offerPrice",
                "offer_price",
                "discountedPrice",
                "price",
                "priceInfo",
                "mrp",
            ],
        )
    )

    image_url = extract_image(
        first_value(
            raw,
            [
                "imageUrl",
                "imageURL",
                "image",
                "images",
                "galleryImages",
                "thumbnail",
                "thumbnailUrl",
            ],
        )
    )

    if not url and product_id:
        url = None

    looks_like_product = bool(
        product_id
        and re.match(r"^mp\d+$", product_id, re.IGNORECASE)
        and url
        and "/p-" in url.lower()
        and name
    )

    if not looks_like_product:
        return None

    return {
        "product_id": product_id,
        "name": name,
        "price": price,
        "url": url,
        "image_url": image_url,
        "scraped_at": now_iso(),
    }


def walk_json_for_products(data: Any) -> list[dict]:
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            normalized = normalize_product(node)
            if normalized:
                found.append(normalized)

            for value in node.values():
                walk(value)

        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


async def dismiss_overlays(page) -> None:
    selectors = [
        "button:has-text('Accept')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        "button:has-text('Allow')",
        "button:has-text('Not Now')",
        "[aria-label='Close']",
        "[aria-label='close']",
        "button[class*='close']",
        "div[class*='close']",
    ]

    for selector in selectors:
        try:
            element = page.locator(selector).first
            if await element.is_visible(timeout=700):
                await element.click(timeout=1500)
                await page.wait_for_timeout(400)
        except Exception:
            pass


async def count_product_links(page) -> int:
    return await page.evaluate(
        r"""() => {
            const links = [...document.querySelectorAll('a[href*="/p-"]')];
            return new Set(links.map(a => new URL(a.href, location.origin).href)).size;
        }"""
    )


async def click_load_more_if_real(page) -> bool:
    clicked_label = await page.evaluate(
        r"""() => {
            const textOf = (node) => (node?.innerText || node?.textContent || '').replace(/\s+/g, ' ').trim();
            const looksLikeLoadMore = (text) => /^(show|load|view)\s+more(?:\s+products?)?$/i.test(text) || /^more$/i.test(text);
            const candidates = [];

            for (const raw of document.querySelectorAll('button, a, [role="button"], div, span')) {
                const el = raw.closest('button, a, [role="button"]') || raw;
                const text = textOf(el);
                if (!looksLikeLoadMore(text)) continue;

                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true' || /disabled/i.test(el.className || '');
                const visible = rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';

                if (!visible || disabled) continue;

                // Avoid header, menu, or filter "show more" controls. The product-grid button sits lower on the page.
                if (rect.top + window.scrollY < 250) continue;

                candidates.push({ el, text, y: rect.top + window.scrollY });
            }

            const candidate = candidates.sort((a, b) => b.y - a.y)[0];
            if (!candidate) return null;

            candidate.el.scrollIntoView({ block: 'center', inline: 'center' });
            candidate.el.click();
            return candidate.text;
        }"""
    )

    if not clicked_label:
        return False

    await page.wait_for_timeout(2200)
    print(f"\n  Clicked '{clicked_label}'")
    return True


async def scroll_to_load(page) -> None:
    previous = -1
    stale_rounds = 0

    for round_no in range(240):
        count = await count_product_links(page)
        print(f"  Product links in DOM: {count}", end="\r", flush=True)

        if count > previous:
            previous = count
            stale_rounds = 0
        else:
            stale_rounds += 1

        clicked = await click_load_more_if_real(page)
        if clicked:
            try:
                await page.wait_for_function(
                    """(before) => {
                        const links = [...document.querySelectorAll('a[href*="/p-"]')];
                        return new Set(links.map(a => new URL(a.href, location.origin).href)).size > before;
                    }""",
                    arg=count,
                    timeout=12_000,
                )
            except PlaywrightTimeoutError:
                pass

            stale_rounds = 0
            continue

        if stale_rounds >= 10:
            break

        await page.evaluate(
            """() => {
                window.scrollBy(0, Math.max(window.innerHeight * 0.9, 800));
                if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 5) {
                    window.scrollTo(0, document.body.scrollHeight);
                }
            }"""
        )
        await page.wait_for_timeout(1200)

    print()


async def extract_products_from_dom(page) -> list[dict]:
    items = await page.evaluate(
        r"""() => {
            const moneyRe = /(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d+)?/i;

            function clean(s) {
                return (s || '').replace(/\s+/g, ' ').trim() || null;
            }

            function bestImage(root) {
                const normalizeImage = (value) => {
                    if (!value) return null;
                    const url = value.trim();
                    if (
                        !url ||
                        url.startsWith('data:') ||
                        url.endsWith('.svg') ||
                        /placeholder|blank|transparent/i.test(url)
                    ) {
                        return null;
                    }
                    return new URL(url, location.origin).href;
                };

                const imgs = [...root.querySelectorAll('img')];
                for (const img of imgs) {
                    const srcset =
                        img.getAttribute('srcset') ||
                        img.getAttribute('data-srcset') ||
                        img.getAttribute('data-lazy-srcset') ||
                        '';
                    if (srcset) {
                        const best = srcset
                            .split(',')
                            .map(s => s.trim().split(/\s+/))
                            .filter(p => p[0])
                            .sort((a, b) => (parseFloat(b[1]) || 0) - (parseFloat(a[1]) || 0))[0];

                        const normalized = best && normalizeImage(best[0]);
                        if (normalized) return normalized;
                    }

                    for (const attr of [
                        'currentSrc',
                        'data-src',
                        'data-original',
                        'data-lazy',
                        'data-lazy-src',
                        'data-image',
                        'data-img',
                        'src'
                    ]) {
                        const raw = attr === 'currentSrc' ? img.currentSrc : img.getAttribute(attr);
                        const normalized = normalizeImage(raw);
                        if (normalized) return normalized;
                    }
                }

                for (const node of [root, ...root.querySelectorAll('*')]) {
                    const background = window.getComputedStyle(node).backgroundImage || '';
                    const match = background.match(/url\(["']?([^"')]+)["']?\)/i);
                    const normalized = match && normalizeImage(match[1]);
                    if (normalized) return normalized;
                }

                return null;
            }

            function cardRoot(link) {
                let node = link;
                for (let i = 0; i < 8 && node && node !== document.body; i++) {
                    const text = clean(node.innerText) || '';
                    const hasPrice = moneyRe.test(text);
                    const hasImage = node.querySelector && node.querySelector('img');
                    if (hasPrice && hasImage) return node;
                    node = node.parentElement;
                }
                return link;
            }

            function inferName(root, link) {
                const aria = clean(link.getAttribute('aria-label') || link.getAttribute('title'));
                if (aria && !moneyRe.test(aria)) return aria;

                const imgAlt = clean(root.querySelector('img')?.getAttribute('alt'));
                if (imgAlt && !moneyRe.test(imgAlt)) return imgAlt;

                const preferred = root.querySelector(
                    '[class*="ProductName"], [class*="product-name"], [class*="ProductTitle"], [class*="product-title"], ' +
                    '[class*="BrandName"], [class*="brand-name"], h2, h3, h4'
                );
                const preferredText = clean(preferred?.innerText);
                if (preferredText && !moneyRe.test(preferredText)) return preferredText;

                const lines = (root.innerText || '')
                    .split('\n')
                    .map(clean)
                    .filter(Boolean)
                    .filter(line => !moneyRe.test(line))
                    .filter(line => !/% off/i.test(line))
                    .filter(line => !/wishlist|rating|review|add to/i.test(line));

                return lines[0] || null;
            }

            function inferPrice(root) {
                const priceNode = root.querySelector(
                    '[class*="Price"], [class*="price"], [class*="OfferPrice"], [class*="selling"], [class*="MRP"]'
                );
                const direct = clean(priceNode?.innerText);
                const directMatch = direct && direct.match(moneyRe);
                if (directMatch) return directMatch[0];

                const text = clean(root.innerText);
                const match = text && text.match(moneyRe);
                return match ? match[0] : null;
            }

            const seen = new Set();
            const results = [];

            for (const link of document.querySelectorAll('a[href*="/p-"]')) {
                const url = new URL(link.href, location.origin).href;
                if (seen.has(url)) continue;
                seen.add(url);

                const root = cardRoot(link);

                results.push({
                    url,
                    name: inferName(root, link),
                    price: inferPrice(root),
                    image_url: bestImage(root)
                });
            }

            return results;
        }"""
    )

    products = []
    for item in items:
        url = item.get("url")
        products.append(
            {
                "product_id": extract_product_id(url),
                "name": clean_text(item.get("name")),
                "price": clean_text(item.get("price")),
                "url": url,
                "image_url": item.get("image_url"),
                "scraped_at": now_iso(),
            }
        )

    return products


async def extract_products_from_page_json(page) -> list[dict]:
    payloads = await page.evaluate(
        r"""() => {
            const out = [];

            for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
                try { out.push(JSON.parse(script.textContent)); } catch (e) {}
            }

            const next = document.querySelector('#__NEXT_DATA__');
            if (next && next.textContent) {
                try { out.push(JSON.parse(next.textContent)); } catch (e) {}
            }

            for (const script of document.querySelectorAll('script')) {
                const text = script.textContent || '';
                if (!text.includes('/p-') && !text.includes('product')) continue;

                const match = text.match(/\{[\s\S]{500,}\}/);
                if (match) {
                    try { out.push(JSON.parse(match[0])); } catch (e) {}
                }
            }

            return out;
        }"""
    )

    products = []
    for payload in payloads:
        products.extend(walk_json_for_products(payload))

    return products


async def detail_image_lookup(context, product: dict, index: int, total: int) -> str | None:
    url = product.get("url")
    if not url:
        return None

    detail_page = await context.new_page()
    try:
        await detail_page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await detail_page.wait_for_load_state("networkidle", timeout=12_000)
        except PlaywrightTimeoutError:
            pass

        await detail_page.wait_for_timeout(1000)
        await detail_page.evaluate("() => window.scrollTo(0, Math.min(1200, document.body.scrollHeight))")
        await detail_page.wait_for_timeout(500)

        image_url = await detail_page.evaluate(
            r"""() => {
                const candidates = [];
                const add = (value) => {
                    if (!value) return;
                    const raw = String(value).trim().replace(/\\\//g, '/').replace(/&amp;/g, '&');
                    if (!raw || raw.startsWith('data:') || /\.svg(?:$|\?)/i.test(raw)) return;
                    try {
                        candidates.push(new URL(raw, location.origin).href);
                    } catch {}
                };

                const addSrcset = (srcset) => {
                    (srcset || '')
                        .split(',')
                        .map((item) => item.trim().split(/\s+/)[0])
                        .filter(Boolean)
                        .forEach(add);
                };

                const crawl = (value, key = '') => {
                    if (!value) return;
                    if (typeof value === 'string') {
                        if (/image|img|src|url|media|thumbnail|gallery/i.test(key) || /tatacliq|\/images?\//i.test(value)) {
                            add(value);
                        }
                        return;
                    }
                    if (Array.isArray(value)) {
                        value.forEach((item) => crawl(item, key));
                        return;
                    }
                    if (typeof value === 'object') {
                        for (const [childKey, child] of Object.entries(value)) {
                            if (/image|img|src|url|media|thumbnail|gallery|base/i.test(childKey) || typeof child === 'object') {
                                crawl(child, childKey);
                            }
                        }
                    }
                };

                for (const script of document.querySelectorAll("script[type='application/ld+json'], script#__NEXT_DATA__")) {
                    try { crawl(JSON.parse(script.textContent || 'null')); } catch {}
                }

                for (const script of document.querySelectorAll('script')) {
                    const text = script.textContent || '';
                    if (!/tatacliq|img|image|media/i.test(text)) continue;

                    for (const match of text.matchAll(/https?:\\?\/\\?\/[^"'\\\s<>]+(?:tatacliq|cliq|\/images?\/)[^"'\\\s<>]*/gi)) {
                        add(match[0]);
                    }
                    for (const match of text.matchAll(/\/\/[^"'\\\s<>]+(?:tatacliq|cliq|\/images?\/)[^"'\\\s<>]*/gi)) {
                        add('https:' + match[0]);
                    }
                    for (const match of text.matchAll(/(?:imageUrl|imageURL|image|src|thumbnail|baseUrl)"?\s*:\s*"([^"]+)"/gi)) {
                        add(match[1]);
                    }
                }

                for (const meta of document.querySelectorAll(
                    "meta[property='og:image'], meta[property='og:image:secure_url'], meta[name='twitter:image'], meta[itemprop='image']"
                )) {
                    add(meta.getAttribute('content'));
                }

                for (const source of document.querySelectorAll('picture source')) {
                    addSrcset(source.getAttribute('srcset'));
                    addSrcset(source.getAttribute('data-srcset'));
                }

                for (const img of document.querySelectorAll('img')) {
                    add(img.currentSrc);
                    add(img.src);
                    add(img.getAttribute('data-src'));
                    add(img.getAttribute('data-original'));
                    add(img.getAttribute('data-lazy'));
                    add(img.getAttribute('data-lazy-src'));
                    add(img.getAttribute('data-image'));
                    add(img.getAttribute('data-img'));
                    addSrcset(img.srcset);
                    addSrcset(img.getAttribute('srcset'));
                    addSrcset(img.getAttribute('data-srcset'));
                    addSrcset(img.getAttribute('data-lazy-srcset'));
                }

                for (const node of document.querySelectorAll('*')) {
                    const background = window.getComputedStyle(node).backgroundImage || '';
                    for (const match of background.matchAll(/url\(["']?([^"')]+)["']?\)/gi)) {
                        add(match[1]);
                    }
                }

                const cleaned = [...new Set(candidates)]
                    .map((url) => url.replace(/\\\//g, '/').replace(/&amp;/g, '&'))
                    .filter((url) => /^https:\/\//i.test(url))
                    .filter((url) => /tatacliq|cliq|\/images?\//i.test(url))
                    .filter((url) => !/sprite|logo|placeholder|transparent|blank|icon|badge/i.test(url));

                const score = (url) => {
                    let value = 0;
                    const lower = url.toLowerCase();
                    if (/img\.tatacliq\.com/i.test(url)) value += 30;
                    if (/\/images\/i/i.test(url)) value += 20;
                    if (/\/image/i.test(url)) value += 12;
                    if (/product|pdp|plp|catalog|large|zoom/i.test(url)) value += 8;
                    if (/\.(jpg|jpeg|png|webp)(?:$|\?)/i.test(url)) value += 5;
                    if (/w[_=-]?\d{3,4}|width[=:_-]?\d{3,4}/i.test(url)) value += 3;
                    if (/h[_=-]?\d{3,4}|height[=:_-]?\d{3,4}/i.test(url)) value += 3;
                    if (/thumb|small|swatch|brand|seller|banner|desktop|mobile/.test(lower)) value -= 8;
                    return value;
                };

                cleaned.sort((a, b) => score(b) - score(a) || b.length - a.length);
                return cleaned[0] || '';
            }"""
        )

        image_url = normalize_image_url(image_url)
        if image_url:
            print(f"      Image recovered {index}/{total}: {product.get('product_id') or product.get('name')}")
        return image_url

    except Exception as exc:
        log.warning(
            "Image lookup failed for %s: %s",
            product.get("product_id") or product.get("url") or product.get("name"),
            exc,
        )
        return None
    finally:
        await detail_page.close()


async def recover_missing_image_urls(context, products: list[dict]) -> None:
    missing = [product for product in products if not has_good_image_url(product.get("image_url")) and product.get("url")]
    if not missing:
        return

    print(f"      Recovering missing image URLs from detail pages: {len(missing)}")
    semaphore = asyncio.Semaphore(IMAGE_LOOKUP_CONCURRENCY)

    async def recover_one(index: int, product: dict) -> None:
        async with semaphore:
            image_url = await detail_image_lookup(context, product, index, len(missing))
            if image_url:
                product["image_url"] = image_url

    await asyncio.gather(*(recover_one(index, product) for index, product in enumerate(missing, start=1)))


def dedupe_products(products: list[dict], limit: int | None = None) -> list[dict]:
    out_by_key = {}
    order = []

    for product in products:
        if not is_real_tatacliq_product(product):
            continue

        key = product_key(product)
        if not key:
            continue

        if key in out_by_key:
            out_by_key[key] = merge_product_record(out_by_key[key], product)
        else:
            order.append(key)
            out_by_key[key] = product

    if limit is None:
        return [out_by_key[key] for key in order]

    return [out_by_key[key] for key in order[:limit]]


async def scrape(
    search_url: str,
    limit: int,
    output: str,
    headless: bool,
    checkpoint_size: int,
    use_indexer: bool,
) -> list[dict]:
    captured_api_products: list[dict] = []
    pending_since_save = 0
    output_path = Path(output)

    indexer_proc = None
    if use_indexer:
        indexer_proc = start_indexer(output_path, checkpoint_size)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """
        )

        page = await context.new_page()

        async def capture_response(response) -> None:
            nonlocal pending_since_save
            try:
                url = response.url.lower()
                content_type = (response.headers.get("content-type") or "").lower()

                if "json" not in content_type:
                    return

                if not any(token in url for token in ["search", "product", "plp", "catalog", "listing"]):
                    return

                data = await response.json()
                found = walk_json_for_products(data)
                if found:
                    captured_api_products.extend(found)
                    pending_since_save += len(found)
                    pending_since_save = checkpoint_save(
                        output_path,
                        captured_api_products,
                        pending_since_save,
                        checkpoint_size,
                    )

            except Exception:
                pass

        page.on("response", lambda response: asyncio.create_task(capture_response(response)))

        print(f"[1/5] Opening: {search_url}")
        await page.goto(search_url, timeout=90_000, wait_until="domcontentloaded")

        print("[2/5] Waiting for initial render ...")
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeoutError:
            pass

        await page.wait_for_timeout(4000)

        print("[3/5] Dismissing overlays ...")
        await dismiss_overlays(page)

        product_link_count = await count_product_links(page)
        print(f"[i] Initial product links found: {product_link_count}")

        print("[4/5] Scrolling and clicking show more until all products are loaded ...")
        await scroll_to_load(page)

        print("[5/5] Extracting product data ...")
        dom_products = await extract_products_from_dom(page)
        json_products = await extract_products_from_page_json(page)

        print(f"      DOM products: {len(dom_products)}")
        print(f"      Page JSON products: {len(json_products)}")
        print(f"      Captured API products: {len(captured_api_products)}")

        raw_products = dom_products + captured_api_products + json_products
        real_candidates = [product for product in raw_products if is_real_tatacliq_product(product)]
        print(f"      Real product candidates before dedupe: {len(real_candidates)}")

        products = dedupe_products(
            real_candidates,
            limit,
        )
        print(f"      Unique real products before image recovery: {len(products)}")
        for product in products:
            product["image_url"] = normalize_image_url(product.get("image_url"))

        await recover_missing_image_urls(context, products)
        before_image_filter = len(products)
        products = [product for product in products if is_real_tatacliq_product(product, require_image=True)]
        print(f"      Unique real products with image_url: {len(products)}")
        if len(products) < before_image_filter:
            print(f"      Dropped after image recovery: {before_image_filter - len(products)}")
        missing_images = sum(1 for product in products if not has_good_image_url(product.get("image_url")))
        if missing_images:
            log.warning("%s products still have no image_url after detail-page recovery.", missing_images)

        if not products:
            Path("tatacliq_debug.html").write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path="tatacliq_debug.png", full_page=True)
            print("      Saved debug files: tatacliq_debug.html, tatacliq_debug.png")

        await browser.close()

    pending_since_save = checkpoint_save(
        output_path,
        products,
        pending_since_save,
        checkpoint_size,
        force=True,
    )

    if indexer_proc and indexer_proc.poll() is None:
        log.info("Indexer still running in background (PID %s).", indexer_proc.pid)

    return products


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Tata CLiQ search products.")
    parser.add_argument("--url", default=DEFAULT_SEARCH_URL, help="Tata CLiQ search URL")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum products to scrape")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output JSON path")
    parser.add_argument("--headless", action="store_true", help="Run browser invisibly")
    parser.add_argument("--checkpoint", type=int, default=CHECKPOINT_SIZE,
                        help="Save JSON after N new products")
    parser.add_argument("--no-indexer", action="store_true",
                        help="Disable parallel embedding indexer")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    products = await scrape(
        search_url=args.url,
        limit=args.limit,
        output=args.output,
        headless=args.headless,
        checkpoint_size=args.checkpoint,
        use_indexer=not args.no_indexer,
    )

    print(f"\n[OK] {len(products)} products -> '{args.output}'")

    if products:
        print("\nSample:")
        print(json.dumps(products[0], ensure_ascii=False, indent=4))
    else:
        print("\n[WARN] 0 products scraped.")
        print("  Open tatacliq_debug.png/html to check for CAPTCHA, blocked page, or changed markup.")
        print("  Try visible mode first: python tata_scraper.py --limit 50")


if __name__ == "__main__":
    asyncio.run(main())
