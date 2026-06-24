import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data" / "json" / "myntra"
LOG_DIR = BASE_DIR / "logs" / "myntra"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


CONFIG = {
    "CONCURRENT_TABS": 4,
    # "browsers" = one Chromium browser per worker. Faster isolation, more RAM.
    # "tabs" = one Chromium browser with many pages. Lighter, sometimes less parallel.
    "CONCURRENCY_MODE": "browsers",
    "HEADLESS": False,
    "CHECKPOINT_EVERY_UNIQUE_ITEMS": 25,
    "FIRST_QUERY_MAX_PAGES": 49,
    "DEFAULT_QUERY_MAX_PAGES": 60,
    "RETRY_LIMIT": 3,
    "NAVIGATION_TIMEOUT_MS": 60000,
    "PRODUCT_WAIT_TIMEOUT_MS": 20000,
    "PAGE_DELAY_SECONDS": 1.2,
    "RETRY_BACKOFF_SECONDS": 5,
    "SCROLL_PAUSE_MS": 450,
    "NEXT_PAGE_WAIT_MS": 20000,
    "USE_URL_PAGINATION_FALLBACK": False,
    "LOG_EVERY_NEW_PRODUCT": False,
    "LOG_PAGINATION_DEBUG": True,
    "REQUIRED_BRAND": "Columbia",
    "DETAIL_LOOKUP_WHEN_IMAGE_MISSING": True,
    "REQUIRE_IMAGE_URL": True,
    "REPAIR_EXISTING_MISSING_IMAGES": True,
    # 0 means repair all existing records missing image_url before normal scraping.
    "MAX_EXISTING_IMAGE_REPAIRS": 0,
    "OUTPUT_FILE": os.environ.get("CPI_OUTPUT", str(DATA_DIR / "latest_myntra.json")),
    "LOG_FILE": os.environ.get("CPI_LOG", str(LOG_DIR / "latest_myntra.log")),
    "BASE_URL": "https://www.myntra.com",
}


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


OUTPUT_PATH = Path(CONFIG["OUTPUT_FILE"])
LOG_PATH = Path(CONFIG["LOG_FILE"])

products_by_id = {}
last_checkpoint_unique_count = 0
checkpoint_lock = asyncio.Lock()
product_lock = asyncio.Lock()
log_lock = asyncio.Lock()

stats = {
    "pages_visited": 0,
    "products_seen": 0,
    "unique_products": 0,
    "duplicates_skipped": 0,
    "non_columbia_filtered": 0,
    "missing_images_seen": 0,
    "image_detail_lookups": 0,
    "image_detail_found": 0,
    "existing_images_repaired": 0,
    "pages_failed": 0,
    "checkpoints": 0,
}


def timestamp():
    return datetime.now().replace(microsecond=0).isoformat()


def compact(value, max_length=90):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) <= max_length:
        return value
    return value[: max_length - 3] + "..."


def format_log_line(message, details):
    now = datetime.now().strftime("%H:%M:%S")
    level = details.pop("level", "INFO")
    worker = details.pop("worker_id", None)
    label = details.pop("label", None)
    page_number = details.pop("page_number", None)

    left = [now, f"{level:<5}"]
    if worker is not None:
        left.append(f"W{worker}")
    if label:
        left.append(f"{compact(label, 24):<24}")
    if page_number is not None:
        left.append(f"p{page_number:<2}")

    important_keys = [
        "query_index",
        "max_pages",
        "products_found_on_page",
        "unique_added_on_page",
        "duplicates_on_page",
        "non_columbia_filtered_on_page",
        "missing_images_on_page",
        "image_detail_found_on_page",
        "total_unique_products",
        "unique_products",
        "products_seen",
        "non_columbia_filtered",
        "missing_images_seen",
        "image_detail_lookups",
        "image_detail_found",
        "existing_images_repaired",
        "pages_visited",
        "pages_failed",
        "checkpoints",
        "elapsed_seconds",
        "reason",
        "url",
        "new_url",
        "pagination",
        "error",
    ]
    parts = []
    for key in important_keys:
        if key in details and details[key] not in (None, ""):
            parts.append(f"{key}={compact(details.pop(key))}")

    for key, value in details.items():
        if value not in (None, ""):
            parts.append(f"{key}={compact(value)}")

    suffix = f" | {' '.join(parts)}" if parts else ""
    return f"{' '.join(left):<44} {message}{suffix}"


async def log(message, **details):
    line = format_log_line(message, dict(details))
    async with log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    print(line, flush=True)


async def log_separator():
    line = "=" * 88
    async with log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    print(line, flush=True)


def parse_department_param(department_param):
    if not department_param:
        return {}

    params = {}
    for chunk in department_param.lstrip("&").split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        params[key] = value
    return params


def build_search_url(keyword, department_param, page_number=1):
    encoded_path = keyword.replace(" ", "-")
    params = {"rawQuery": keyword}

    if CONFIG["USE_URL_PAGINATION_FALLBACK"] and page_number > 1:
        params["p"] = str(page_number)

    params.update(parse_department_param(department_param))
    return f"{CONFIG['BASE_URL']}/{encoded_path}?{urlencode(params)}"


def product_id_from_url(url):
    match = re.search(r"/(\d+)/buy(?:\?|$)", url or "")
    return match.group(1) if match else ""


def normalize_price(price):
    price = re.sub(r"\s+", " ", price or "").strip()
    return re.sub(r"^Rs\.\s*", "Rs. ", price)


def normalize_image_url(image_url):
    image_url = re.sub(r"\s+", "", str(image_url or "")).strip()
    if not image_url:
        return ""
    image_url = image_url.replace("\\/", "/").replace("&amp;", "&").strip("\"'")
    if image_url.startswith("//"):
        image_url = "https:" + image_url
    if image_url.startswith("http://"):
        image_url = "https://" + image_url.removeprefix("http://")
    if not image_url.startswith("https://"):
        return ""
    if "assets.myntassets.com" not in image_url and "myntra.com" not in image_url:
        return ""
    if re.search(r"sprite|logo|placeholder|transparent|blank|icon|badge", image_url, re.IGNORECASE):
        return ""
    return image_url


def normalize_brand(brand):
    return re.sub(r"\s+", " ", str(brand or "")).strip()


def is_required_brand(brand):
    return normalize_brand(brand).casefold() == CONFIG["REQUIRED_BRAND"].casefold()


async def load_existing_products():
    global last_checkpoint_unique_count

    if not OUTPUT_PATH.exists():
        return

    try:
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        if not isinstance(existing, list):
            raise ValueError("Existing output is not a JSON array")

        dropped_non_brand = 0
        dropped_duplicates = 0
        missing_images_loaded = 0

        for product in existing:
            product_id = str(product.get("product_id", ""))
            if not product_id:
                continue

            if not is_required_brand(product.get("brand", "")):
                dropped_non_brand += 1
                continue

            if product_id in products_by_id:
                dropped_duplicates += 1
                continue

            product["brand"] = CONFIG["REQUIRED_BRAND"]
            product["image_url"] = normalize_image_url(product.get("image_url", ""))
            if not product["image_url"]:
                missing_images_loaded += 1
            products_by_id[product_id] = product

        stats["unique_products"] = len(products_by_id)
        stats["missing_images_seen"] += missing_images_loaded
        last_checkpoint_unique_count = len(products_by_id)
        stats["non_columbia_filtered"] += dropped_non_brand
        await log(
            "Loaded existing output file",
            unique_products=len(products_by_id),
            missing_images_seen=missing_images_loaded,
            non_columbia_filtered=dropped_non_brand,
            duplicates_skipped=dropped_duplicates,
        )
    except Exception as error:
        await log("Could not load existing output file; starting fresh", error=str(error))


async def write_checkpoint(reason):
    global last_checkpoint_unique_count

    async with checkpoint_lock:
        async with product_lock:
            products = sorted(products_by_id.values(), key=lambda item: int(item["product_id"]))
            if CONFIG["REQUIRE_IMAGE_URL"]:
                products = [product for product in products if normalize_image_url(product.get("image_url", ""))]

        temp_path = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".tmp")
        temp_path.write_text(json.dumps(products, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp_path, OUTPUT_PATH)

        last_checkpoint_unique_count = len(products)
        stats["checkpoints"] += 1
        await log(
            "Wrote product checkpoint",
            reason=reason,
            unique_products=len(products),
            checkpoints=stats["checkpoints"],
        )


async def maybe_checkpoint():
    if len(products_by_id) - last_checkpoint_unique_count >= CONFIG["CHECKPOINT_EVERY_UNIQUE_ITEMS"]:
        await write_checkpoint(f"Reached {CONFIG['CHECKPOINT_EVERY_UNIQUE_ITEMS']} new unique products")


async def auto_scroll(page):
    previous_height = 0
    for index in range(8):
        current_height = await page.evaluate("() => document.body.scrollHeight")
        if index > 1 and current_height == previous_height:
            break

        previous_height = current_height
        await page.mouse.wheel(0, 2200)
        await page.wait_for_timeout(CONFIG["SCROLL_PAUSE_MS"])


async def scroll_to_bottom(page):
    for _ in range(4):
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(CONFIG["SCROLL_PAUSE_MS"])


async def extract_products(page):
    return await page.evaluate(
        """
        () => {
          const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const bestFromSrcset = (srcset) => {
            return (srcset || "")
              .split(",")
              .map((item) => item.trim().split(/\\s+/)[0])
              .filter(Boolean)
              .pop() || "";
          };
          const backgroundImage = (el) => {
            const value = el ? getComputedStyle(el).backgroundImage || "" : "";
            return (value.match(/url\\(["']?([^"')]+)["']?\\)/) || [])[1] || "";
          };
          const absoluteUrl = (href) => {
            try {
              return new URL(href, window.location.origin).href;
            } catch {
              return "";
            }
          };

          return [...document.querySelectorAll("li.product-base, .product-base")]
            .map((card) => {
              const anchor = card.querySelector("a[href*='/buy']");
              const href = anchor?.getAttribute("href") || "";
              const url = absoluteUrl(href);
              const productId = url.match(/\\/(\\d+)\\/buy(?:\\?|$)/)?.[1] || "";
              const brand = clean(card.querySelector(".product-brand")?.textContent);
              const productName = clean(card.querySelector(".product-product")?.textContent);
              const name = [brand, productName].filter(Boolean).join(" ");
              const discountedPrice = clean(card.querySelector(".product-discountedPrice")?.textContent);
              const regularPrice = clean(card.querySelector(".product-price")?.textContent);
              const img = card.querySelector("img");
              const imageUrl =
                img?.currentSrc ||
                img?.src ||
                bestFromSrcset(img?.srcset) ||
                bestFromSrcset(img?.getAttribute("data-srcset")) ||
                img?.getAttribute("data-original") ||
                img?.getAttribute("data-image") ||
                img?.getAttribute("data-src") ||
                img?.getAttribute("src") ||
                backgroundImage(card.querySelector("[style*='background']")) ||
                backgroundImage(card) ||
                "";

              return {
                product_id: productId,
                brand,
                name,
                price: discountedPrice || regularPrice,
                url,
                image_url: imageUrl,
              };
            })
            .filter((product) => product.product_id && product.url);
        }
        """
    )


async def detail_image_lookup(context, product, worker_id, label="", page_number=None):
    stats["image_detail_lookups"] += 1
    detail_page = await context.new_page()
    try:
        await detail_page.goto(
            product["url"],
            wait_until="domcontentloaded",
            timeout=CONFIG["NAVIGATION_TIMEOUT_MS"],
        )
        await detail_page.wait_for_timeout(1200)
        await detail_page.evaluate("() => window.scrollTo(0, Math.min(1000, document.body.scrollHeight))")
        image_url = await detail_page.evaluate(
            """
            () => {
              const candidates = [];
              const add = (value) => {
                if (!value) return;
                const raw = String(value).trim().replace(/\\\\\\//g, "/").replace(/&amp;/g, "&");
                if (!raw || raw.startsWith("data:") || /\\.svg(?:$|\\?)/i.test(raw)) return;
                try {
                  candidates.push(new URL(raw, location.origin).href);
                } catch {}
              };
              const addSrcset = (srcset) => {
                (srcset || "")
                  .split(",")
                  .map((item) => item.trim().split(/\\s+/)[0])
                  .filter(Boolean)
                  .forEach(add);
              };
              const crawl = (value, key = "") => {
                if (!value) return;
                if (typeof value === "string") {
                  if (/assets\\.myntassets\\.com/i.test(value) || /image|img|src|url|media|thumbnail|gallery/i.test(key)) {
                    add(value);
                  }
                  return;
                }
                if (Array.isArray(value)) {
                  value.forEach((item) => crawl(item, key));
                  return;
                }
                if (typeof value === "object") {
                  for (const [key, child] of Object.entries(value)) {
                    if (/image|img|src|url|media|thumbnail|gallery|base/i.test(key) || typeof child === "object") {
                      crawl(child, key);
                    }
                  }
                }
              };

              for (const script of document.querySelectorAll("script[type='application/ld+json'], script#__NEXT_DATA__")) {
                try { crawl(JSON.parse(script.textContent || "null")); } catch {}
              }

              for (const script of document.querySelectorAll("script")) {
                const text = script.textContent || "";
                if (!/assets\\.myntassets\\.com|image|img|media/i.test(text)) continue;
                for (const match of text.matchAll(/https?:\\\\?\\/\\\\?\\/assets\\.myntassets\\.com[^"'\\\\\\s]+/gi)) {
                  add(match[0].replace(/\\\\\\//g, "/"));
                }
                for (const match of text.matchAll(/\\/\\/assets\\.myntassets\\.com[^"'\\\\\\s]+/gi)) {
                  add("https:" + match[0].replace(/\\\\\\//g, "/"));
                }
                for (const match of text.matchAll(/(?:imageUrl|imageURL|image|src|thumbnail|baseUrl)"?\\s*:\\s*"([^"]+)"/gi)) {
                  add(match[1]);
                }
              }

              for (const meta of document.querySelectorAll("meta[property='og:image'], meta[property='og:image:secure_url'], meta[name='twitter:image'], meta[itemprop='image']")) {
                add(meta.getAttribute("content"));
              }

              for (const source of document.querySelectorAll("picture source")) {
                addSrcset(source.getAttribute("srcset"));
                addSrcset(source.getAttribute("data-srcset"));
              }

              for (const img of document.querySelectorAll("img")) {
                add(img.currentSrc);
                add(img.src);
                add(img.getAttribute("data-src"));
                add(img.getAttribute("data-original"));
                add(img.getAttribute("data-lazy"));
                add(img.getAttribute("data-lazy-src"));
                add(img.getAttribute("data-image"));
                add(img.getAttribute("data-img"));
                addSrcset(img.srcset);
                addSrcset(img.getAttribute("srcset"));
                addSrcset(img.getAttribute("data-srcset"));
                addSrcset(img.getAttribute("data-lazy-srcset"));
              }

              for (const node of document.querySelectorAll("*")) {
                const background = window.getComputedStyle(node).backgroundImage || "";
                for (const match of background.matchAll(/url\\(["']?([^"')]+)["']?\\)/gi)) {
                  add(match[1]);
                }
              }

              const cleaned = [...new Set(candidates)]
                .map((url) => url.replace(/\\\\\\//g, "/").replace(/&amp;/g, "&"))
                .filter((url) => /https:\\/\\/assets\\.myntassets\\.com/i.test(url))
                .filter((url) => !/sprite|logo|placeholder|transparent|blank|icon|badge/i.test(url));

              cleaned.sort((a, b) => {
                const score = (url) => {
                  let value = 0;
                  if (/\\/assets\\/images\\//i.test(url)) value += 8;
                  if (/-1\\./.test(url) || /_1\\./.test(url)) value += 3;
                  if (/h_\\d+|w_\\d+/i.test(url)) value += 2;
                  if (/q_\\d+|q_auto/i.test(url)) value += 1;
                  if (/thumb|small|swatch|brand|banner/i.test(url)) value -= 5;
                  return value;
                };
                return score(b) - score(a) || b.length - a.length;
              });

              return cleaned[0] || "";
            }
            """
        )
        image_url = normalize_image_url(image_url)
        if image_url:
            stats["image_detail_found"] += 1
            await log(
                "Recovered image from detail page",
                worker_id=worker_id,
                label=label,
                page_number=page_number,
                product_id=product.get("product_id", ""),
                image_url=image_url,
            )
        return image_url
    except Exception as error:
        await log(
            "Detail image lookup failed",
            worker_id=worker_id,
            label=label,
            page_number=page_number,
            product_id=product.get("product_id", ""),
            error=str(error),
            level="WARN",
        )
        return ""
    finally:
        await close_page(detail_page)


async def page_signature(page):
    try:
        return await page.evaluate(
            """
            () => {
              const firstBuy = document.querySelector("a[href*='/buy']")?.href || "";
              const activePage =
                document.querySelector(".pagination-active")?.textContent ||
                document.querySelector("[class*='pagination-active']")?.textContent ||
                "";
              return `${location.href}|${firstBuy}|${activePage.trim()}`;
            }
            """
        )
    except Exception:
        return ""


async def scrape_page(page, query, page_number, worker_id, open_page=False):
    keyword, department_param, label = query
    url = build_search_url(keyword, department_param, page_number)

    for attempt in range(1, CONFIG["RETRY_LIMIT"] + 1):
        try:
            started_at = datetime.now()
            await log(
                "Opening search page" if open_page else "Scraping current search page",
                worker_id=worker_id,
                label=label,
                page_number=page_number,
                attempt=attempt,
                url=url,
            )
            if open_page:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=CONFIG["NAVIGATION_TIMEOUT_MS"],
                )

                try:
                    await page.wait_for_load_state("networkidle", timeout=CONFIG["NAVIGATION_TIMEOUT_MS"])
                except PlaywrightTimeoutError:
                    pass

            await page.wait_for_selector(
                "li.product-base, .product-base",
                timeout=CONFIG["PRODUCT_WAIT_TIMEOUT_MS"],
            )
            await auto_scroll(page)

            products = await extract_products(page)
            stats["pages_visited"] += 1
            stats["products_seen"] += len(products)

            added = 0
            duplicates = 0
            non_columbia_filtered = 0
            missing_images = 0
            image_detail_found_on_page = 0
            scraped_at = timestamp()
            should_checkpoint = False
            added_product_logs = []
            prepared_products = []

            for product in products:
                product_id = product_id_from_url(product["url"]) or product["product_id"]
                if not product_id:
                    continue

                if not is_required_brand(product.get("brand", "")):
                    non_columbia_filtered += 1
                    continue

                product["product_id"] = product_id
                product["image_url"] = normalize_image_url(product.get("image_url", ""))
                if not product["image_url"]:
                    missing_images += 1
                    stats["missing_images_seen"] += 1
                    if CONFIG["DETAIL_LOOKUP_WHEN_IMAGE_MISSING"]:
                        recovered_image_url = await detail_image_lookup(
                            page.context,
                            product,
                            worker_id,
                            label=label,
                            page_number=page_number,
                        )
                        if recovered_image_url:
                            product["image_url"] = recovered_image_url
                            image_detail_found_on_page += 1
                    if CONFIG["REQUIRE_IMAGE_URL"] and not product["image_url"]:
                        continue

                prepared_products.append(product)

            async with product_lock:
                for product in prepared_products:
                    product_id = product["product_id"]

                    if product_id in products_by_id:
                        duplicates += 1
                        if not products_by_id[product_id].get("image_url") and product.get("image_url"):
                            products_by_id[product_id]["image_url"] = product["image_url"]
                            stats["existing_images_repaired"] += 1
                            should_checkpoint = True
                        continue

                    products_by_id[product_id] = {
                        "product_id": product_id,
                        "brand": CONFIG["REQUIRED_BRAND"],
                        "name": product.get("name", ""),
                        "price": normalize_price(product.get("price", "")),
                        "url": product["url"],
                        "image_url": product.get("image_url", ""),
                        "scraped_at": scraped_at,
                    }
                    added += 1

                    if CONFIG["LOG_EVERY_NEW_PRODUCT"]:
                        added_product_logs.append(
                            {
                                "worker_id": worker_id,
                                "label": label,
                                "page_number": page_number,
                                "product_id": product_id,
                                "brand": products_by_id[product_id]["brand"],
                                "name": products_by_id[product_id]["name"],
                                "total_unique_products": len(products_by_id),
                            }
                        )

                    if len(products_by_id) - last_checkpoint_unique_count >= CONFIG["CHECKPOINT_EVERY_UNIQUE_ITEMS"]:
                        should_checkpoint = True

            for product_log in added_product_logs:
                await log("Added unique product", **product_log)

            if should_checkpoint:
                await write_checkpoint(f"Reached {CONFIG['CHECKPOINT_EVERY_UNIQUE_ITEMS']} new unique products")

            stats["unique_products"] = len(products_by_id)
            stats["duplicates_skipped"] += duplicates
            stats["non_columbia_filtered"] += non_columbia_filtered

            await log(
                "Finished search page",
                worker_id=worker_id,
                label=label,
                page_number=page_number,
                products_found_on_page=len(products),
                unique_added_on_page=added,
                duplicates_on_page=duplicates,
                non_columbia_filtered_on_page=non_columbia_filtered,
                missing_images_on_page=missing_images,
                image_detail_found_on_page=image_detail_found_on_page,
                total_unique_products=len(products_by_id),
                elapsed_seconds=round((datetime.now() - started_at).total_seconds(), 2),
            )
            return {"products_found": len(products), "added": added}
        except Exception as error:
            await log(
                "Search page failed",
                worker_id=worker_id,
                label=label,
                page_number=page_number,
                attempt=attempt,
                error=str(error),
            )

            if attempt == CONFIG["RETRY_LIMIT"]:
                stats["pages_failed"] += 1
                return {"products_found": 0, "added": 0, "failed": True}

            await asyncio.sleep(CONFIG["RETRY_BACKOFF_SECONDS"] * attempt)

    return {"products_found": 0, "added": 0, "failed": True}


async def click_next_page(page, query, page_number, worker_id):
    label = query[2]
    before_signature = await page_signature(page)
    before_url = page.url

    await scroll_to_bottom(page)

    next_info = await page.evaluate(
        """
        () => {
          const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const absoluteUrl = (href) => {
            try {
              return new URL(href, location.origin).href;
            } catch {
              return "";
            }
          };
          const pageNow = Number(new URL(location.href).searchParams.get("p") || "1");
          const all = [...document.querySelectorAll("a, button, li, div, span")];
          const pagination = all
            .filter((el) => /pagination|page|next/i.test(String(el.className || "") + " " + clean(el.textContent)))
            .slice(-20)
            .map((el) => ({
              tag: el.tagName,
              text: clean(el.textContent).slice(0, 80),
              className: String(el.className || "").slice(0, 120),
              href: absoluteUrl(el.getAttribute("href") || el.querySelector("a[href]")?.getAttribute("href") || ""),
            }));

          const candidates = all
            .map((raw) => {
              const el = raw.closest("li, a, button, div") || raw;
              const text = clean(el.textContent);
              const className = String(el.className || "");
              const href = absoluteUrl(el.getAttribute("href") || el.querySelector("a[href]")?.getAttribute("href") || "");
              const looksNext =
                /pagination-next|\\bnext\\b/i.test(className) ||
                /\\bnext\\b/i.test(text) ||
                /[?&]p=\\d+/.test(href) && Number(new URL(href).searchParams.get("p") || "0") === pageNow + 1;
              const disabled =
                Boolean(el.disabled) ||
                el.getAttribute("aria-disabled") === "true" ||
                /disabled/i.test(className);
              const clickable = el.matches("a, button") ? el : el.querySelector("a, button") || el;
              return { el, clickable, text, className, href, looksNext, disabled };
            })
            .filter((item) => item.looksNext);

          const usable = candidates.find((item) => !item.disabled);
          if (!usable) {
            return {
              found: candidates.length > 0,
              disabled: candidates.length > 0,
              pagination,
            };
          }

          return {
            found: true,
            disabled: false,
            text: usable.text.slice(0, 80),
            className: usable.className.slice(0, 120),
            href: usable.href,
            tagName: usable.el.tagName,
            pagination,
          };
        }
        """
    )

    if not next_info.get("found"):
        await log(
            "No next page control found",
            worker_id=worker_id,
            label=label,
            page_number=page_number,
            url=before_url,
            pagination=next_info.get("pagination", []) if CONFIG["LOG_PAGINATION_DEBUG"] else "",
        )
        return False

    if next_info.get("disabled"):
        await log(
            "Next page control is disabled",
            worker_id=worker_id,
            label=label,
            page_number=page_number,
            url=before_url,
            pagination=next_info.get("pagination", []) if CONFIG["LOG_PAGINATION_DEBUG"] else "",
        )
        return False

    await log(
        "Clicking next page",
        worker_id=worker_id,
        label=label,
        page_number=page_number,
        url=before_url,
        next_text=next_info.get("text", ""),
        next_href=next_info.get("href", ""),
    )

    clicked = await page.evaluate(
        """
        () => {
          const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const absoluteUrl = (href) => {
            try {
              return new URL(href, location.origin).href;
            } catch {
              return "";
            }
          };
          const pageNow = Number(new URL(location.href).searchParams.get("p") || "1");

          for (const raw of [...document.querySelectorAll("a, button, li, div, span")]) {
            const el = raw.closest("li, a, button, div") || raw;
            const text = clean(el.textContent);
            const className = String(el.className || "");
            const href = absoluteUrl(el.getAttribute("href") || el.querySelector("a[href]")?.getAttribute("href") || "");
            const looksNext =
              /pagination-next|\\bnext\\b/i.test(className) ||
              /\\bnext\\b/i.test(text) ||
              /[?&]p=\\d+/.test(href) && Number(new URL(href).searchParams.get("p") || "0") === pageNow + 1;
            const disabled = Boolean(el.disabled) ||
              el.getAttribute("aria-disabled") === "true" ||
              /disabled/i.test(className);
            if (!looksNext || disabled) continue;

            const clickable = el.matches("a, button") ? el : el.querySelector("a, button") || el;
            if (!clickable) continue;

            clickable.scrollIntoView({ block: "center", inline: "center" });
            clickable.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
            clickable.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
            clickable.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
            clickable.click();
            return true;
          }

          return false;
        }
        """
    )

    if not clicked:
        if next_info.get("href"):
            await page.goto(next_info["href"], wait_until="domcontentloaded", timeout=CONFIG["NAVIGATION_TIMEOUT_MS"])
            clicked = True

    if not clicked:
        await log(
            "Next page control could not be clicked",
            worker_id=worker_id,
            label=label,
            page_number=page_number,
            url=before_url,
        )
        return False

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=CONFIG["NEXT_PAGE_WAIT_MS"])
    except PlaywrightTimeoutError:
        pass

    try:
        await page.wait_for_function(
            "(previous) => document.location.href !== previous",
            arg=before_url,
            timeout=CONFIG["NEXT_PAGE_WAIT_MS"],
        )
    except PlaywrightTimeoutError:
        try:
            await page.wait_for_function(
                "(previous) => { const firstBuy = document.querySelector(\"a[href*='/buy']\")?.href || ''; const activePage = document.querySelector('.pagination-active')?.textContent || document.querySelector(\"[class*='pagination-active']\")?.textContent || ''; return `${location.href}|${firstBuy}|${activePage.trim()}` !== previous; }",
                arg=before_signature,
                timeout=CONFIG["NEXT_PAGE_WAIT_MS"],
            )
        except PlaywrightTimeoutError:
            await log(
                "Next page click did not change page",
                worker_id=worker_id,
                label=label,
                page_number=page_number,
                url=before_url,
                pagination=next_info.get("pagination", []) if CONFIG["LOG_PAGINATION_DEBUG"] else "",
            )
            return False

    await log(
        "Next page loaded",
        worker_id=worker_id,
        label=label,
        page_number=page_number + 1,
        new_url=page.url,
    )
    return True


async def fresh_page(context):
    page = await context.new_page()
    page.set_default_timeout(CONFIG["NAVIGATION_TIMEOUT_MS"])
    return page


async def close_page(page):
    try:
        await page.close()
    except Exception:
        pass


async def create_browser_context(playwright, worker_id=None):
    browser = await playwright.chromium.launch(
        headless=CONFIG["HEADLESS"],
        args=[
            "--disable-http2",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    )
    context = await browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        ignore_https_errors=True,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    await log("Browser context ready", worker_id=worker_id, headless=CONFIG["HEADLESS"])
    return browser, context


async def scrape_query(context, query_index, worker_id):
    query = SEARCH_QUERIES[query_index]
    label = query[2]
    max_pages = CONFIG["FIRST_QUERY_MAX_PAGES"] if query_index == 0 else CONFIG["DEFAULT_QUERY_MAX_PAGES"]
    page = await fresh_page(context)

    await log("Started query", worker_id=worker_id, label=label, query_index=query_index, max_pages=max_pages)

    try:
        for page_number in range(1, max_pages + 1):
            result = await scrape_page(page, query, page_number, worker_id, open_page=page_number == 1)
            if result.get("failed"):
                await close_page(page)
                page = await fresh_page(context)
                if page_number == 1:
                    await log(
                        "First page failed; stopping query",
                        worker_id=worker_id,
                        label=label,
                        query_index=query_index,
                    )
                    break

            if result["products_found"] == 0:
                await log(
                    "Stopping query because current page returned no products",
                    worker_id=worker_id,
                    label=label,
                    page_number=page_number,
                )
                break

            if page_number >= max_pages:
                await log(
                    "Stopping query because configured max pages was reached",
                    worker_id=worker_id,
                    label=label,
                    page_number=page_number,
                    max_pages=max_pages,
                )
                break

            has_next_page = await click_next_page(page, query, page_number, worker_id)
            if not has_next_page:
                break

            await asyncio.sleep(CONFIG["PAGE_DELAY_SECONDS"])
    finally:
        await close_page(page)
        await log("Finished query", worker_id=worker_id, label=label, total_unique_products=len(products_by_id))


async def repair_existing_missing_images(context, worker_id, repair_queue):
    repaired_by_worker = 0
    while True:
        try:
            product_id = repair_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        try:
            async with product_lock:
                product = dict(products_by_id.get(product_id) or {})

            if not product or product.get("image_url") or not product.get("url"):
                continue

            image_url = await detail_image_lookup(
                context,
                product,
                worker_id,
                label="Image Repair",
            )
            if not image_url:
                continue

            async with product_lock:
                if product_id in products_by_id and not products_by_id[product_id].get("image_url"):
                    products_by_id[product_id]["image_url"] = image_url
                    stats["existing_images_repaired"] += 1
                    repaired_by_worker += 1

            if stats["existing_images_repaired"] % CONFIG["CHECKPOINT_EVERY_UNIQUE_ITEMS"] == 0:
                await write_checkpoint("Repaired missing image URLs")
        finally:
            repair_queue.task_done()

    if repaired_by_worker:
        await log(
            "Finished image repair batch",
            worker_id=worker_id,
            existing_images_repaired=repaired_by_worker,
            total_unique_products=len(products_by_id),
        )


async def worker_with_context(context, worker_id, queue, repair_queue):
    if repair_queue is not None:
        await repair_existing_missing_images(context, worker_id, repair_queue)

    while True:
        try:
            query_index = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        await log("Worker picked query", worker_id=worker_id, query_index=query_index)
        try:
            await scrape_query(context, query_index, worker_id)
        finally:
            queue.task_done()


async def worker_with_browser(playwright, worker_id, queue, repair_queue):
    browser, context = await create_browser_context(playwright, worker_id=worker_id)
    try:
        await worker_with_context(context, worker_id, queue, repair_queue)
    finally:
        await context.close()
        await browser.close()
        await log("Worker browser closed", worker_id=worker_id)


async def main():
    LOG_PATH.write_text("", encoding="utf-8")
    await load_existing_products()
    await log_separator()
    await log(
        "Myntra Columbia scraper starting",
        concurrent_tabs=CONFIG["CONCURRENT_TABS"],
        concurrency_mode=CONFIG["CONCURRENCY_MODE"],
        headless=CONFIG["HEADLESS"],
        query_count=len(SEARCH_QUERIES),
        output_file=CONFIG["OUTPUT_FILE"],
    )
    await log_separator()

    async with async_playwright() as playwright:
        queue = asyncio.Queue()
        for query_index in range(len(SEARCH_QUERIES)):
            queue.put_nowait(query_index)

        repair_queue = None
        if CONFIG["REPAIR_EXISTING_MISSING_IMAGES"]:
            missing_image_ids = [
                product_id
                for product_id, product in products_by_id.items()
                if not product.get("image_url")
            ]
            if CONFIG["MAX_EXISTING_IMAGE_REPAIRS"] > 0:
                missing_image_ids = missing_image_ids[: CONFIG["MAX_EXISTING_IMAGE_REPAIRS"]]

            repair_queue = asyncio.Queue()
            for product_id in missing_image_ids:
                repair_queue.put_nowait(product_id)

            await log(
                "Queued existing missing image repairs",
                missing_images_seen=len(missing_image_ids),
            )

        worker_count = min(CONFIG["CONCURRENT_TABS"], len(SEARCH_QUERIES))

        if CONFIG["CONCURRENCY_MODE"] == "browsers":
            workers = [
                asyncio.create_task(worker_with_browser(playwright, worker_id + 1, queue, repair_queue))
                for worker_id in range(worker_count)
            ]

            try:
                await asyncio.gather(*workers)
            finally:
                await write_checkpoint("Final save before shutdown")
        elif CONFIG["CONCURRENCY_MODE"] == "tabs":
            browser, context = await create_browser_context(playwright, worker_id="shared")
            workers = [
                asyncio.create_task(worker_with_context(context, worker_id + 1, queue, repair_queue))
                for worker_id in range(worker_count)
            ]

            try:
                await asyncio.gather(*workers)
            finally:
                await write_checkpoint("Final save before browser close")
                await context.close()
                await browser.close()
        else:
            raise ValueError('CONFIG["CONCURRENCY_MODE"] must be "browsers" or "tabs"')

    await log("Scraper finished", **stats)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user")
