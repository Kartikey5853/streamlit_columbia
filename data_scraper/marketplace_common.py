import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout


PRICE_TOLERANCE = 1000.0


def parse_price(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[\d,]+(?:\.\d+)?", str(value))
    return float(match.group(0).replace(",", "")) if match else None


def format_price(value: float) -> str:
    return f"₹{value:,.2f}"


def clean_url(base_url: str, value: str | None) -> str | None:
    if not value:
        return None
    return urljoin(base_url, value.strip())


def same_host(left: str, right: str) -> bool:
    return urlparse(left).netloc.casefold() == urlparse(right).netloc.casefold()


def price_is_close(reference_price: float, candidate_price: float) -> bool:
    return abs(reference_price - candidate_price) <= PRICE_TOLERANCE


async def first_attribute(page: Page, selectors: list[str], attribute: str) -> str | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count():
                value = await locator.get_attribute(attribute)
                if value:
                    return value.strip()
        except Exception:
            continue
    return None


async def first_text(page: Page, selectors: list[str]) -> str | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count():
                value = re.sub(r"\s+", " ", await locator.text_content() or "").strip()
                if value:
                    return value
        except Exception:
            continue
    return None


async def meta_content(page: Page, names: list[str]) -> str | None:
    for name in names:
        for selector in (
            f"meta[property='{name}']",
            f"meta[name='{name}']",
            f"meta[itemprop='{name}']",
        ):
            value = await first_attribute(page, [selector], "content")
            if value:
                return value
    return None


async def product_json_ld(page: Page) -> dict:
    values = await page.locator('script[type="application/ld+json"]').all_text_contents()
    queue = []
    for value in values:
        try:
            queue.append(json.loads(value))
        except json.JSONDecodeError:
            continue

    while queue:
        value = queue.pop(0)
        if isinstance(value, list):
            queue.extend(value)
            continue
        if not isinstance(value, dict):
            continue
        item_type = value.get("@type")
        if item_type == "Product" or (
            isinstance(item_type, list) and "Product" in item_type
        ):
            return value
        queue.extend(child for child in value.values() if isinstance(child, (dict, list)))
    return {}


def json_ld_offer(product: dict) -> dict:
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        return next((offer for offer in offers if isinstance(offer, dict)), {})
    return offers if isinstance(offers, dict) else {}


def json_ld_image(product: dict) -> str | None:
    image = product.get("image")
    if isinstance(image, list):
        image = next((value for value in image if isinstance(value, str)), None)
    if isinstance(image, dict):
        image = image.get("url")
    return image if isinstance(image, str) else None


@dataclass(frozen=True)
class MarketplaceConfig:
    name: str
    base_url: str
    search_urls: tuple[str, ...]
    search_input_selectors: tuple[str, ...]
    search_open_selectors: tuple[str, ...]
    search_submit_selectors: tuple[str, ...]
    first_product_selectors: tuple[str, ...]
    title_selectors: tuple[str, ...]
    price_selectors: tuple[str, ...]
    image_selectors: tuple[tuple[str, str], ...]
    no_result_markers: tuple[str, ...]
    blocked_markers: tuple[str, ...]


class MarketplaceScraper:
    def __init__(self, config: MarketplaceConfig):
        self.config = config

    async def search(
        self,
        page: Page,
        upc: str,
        reference_price: float,
    ) -> dict | None:
        product_page = await self._open_first_result(page, upc)
        if not product_page:
            status = await self._page_status(page, upc)
            if status in {"blocked", "not_found"}:
                return {"status": status}
            return None
        result = await self._extract_product(page)
        if not result or not price_is_close(reference_price, result["price_value"]):
            return None
        return {
            "title": result["title"],
            "image": result["image"],
            "price": format_price(result["price_value"]),
            "link": result["link"],
        }

    async def _open_first_result(self, page: Page, upc: str) -> bool:
        if await self._search_from_current_page(page, upc):
            if await self._open_result_from_search_page(page, upc):
                return True
        if await self._is_blocked_page(page):
            return False

        for template in self.config.search_urls:
            search_url = template.format(query=quote_plus(upc))
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(500)
            except PlaywrightTimeout:
                continue

            if self._looks_like_product_url(page.url):
                return True
            if await self._is_blocked_page(page):
                return False

            for selector in self.config.first_product_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=3000)
                except PlaywrightTimeout:
                    pass

                link = page.locator(selector).first
                try:
                    if not await link.count():
                        continue
                    href = await link.get_attribute("href")
                    if not href:
                        continue
                    await page.goto(
                        clean_url(self.config.base_url, href),
                        wait_until="commit",
                        timeout=45000,
                    )
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    return True
                except PlaywrightTimeout:
                    if self._looks_like_product_url(page.url):
                        return True
                    continue
        return False

    async def _search_from_current_page(self, page: Page, upc: str) -> bool:
        try:
            if page.url == "about:blank" or not same_host(page.url, self.config.base_url):
                await page.goto(
                    self.config.base_url,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                await page.wait_for_timeout(1500)
        except PlaywrightTimeout:
            return False

        if await self._is_blocked_page(page):
            return False

        for opener in self.config.search_open_selectors:
            locator = page.locator(opener).first
            try:
                if await locator.count() and await locator.is_visible(timeout=1000):
                    await locator.click(timeout=3000)
                    await page.wait_for_timeout(500)
                    break
            except Exception:
                continue

        for selector in self.config.search_input_selectors:
            field = page.locator(selector).first
            try:
                if not await field.count() or not await field.is_visible(timeout=1000):
                    continue
                await field.click(timeout=3000)
                await field.press("Control+A")
                await field.fill(upc)
                await field.press("Enter")
                await self._wait_after_search(page)
                if await self._is_blocked_page(page):
                    return False
                if await self._page_matches_query(page, upc):
                    return True

                for submit_selector in self.config.search_submit_selectors:
                    button = page.locator(submit_selector).first
                    try:
                        if not await button.count() or not await button.is_visible(timeout=1000):
                            continue
                        await button.click(timeout=3000)
                        await self._wait_after_search(page)
                        if await self._is_blocked_page(page):
                            return False
                        if await self._page_matches_query(page, upc):
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    async def _wait_after_search(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PlaywrightTimeout:
            pass
        await page.wait_for_timeout(1500)

    async def _open_result_from_search_page(self, page: Page, upc: str) -> bool:
        if self._looks_like_product_url(page.url):
            return True
        if await self._is_blocked_page(page):
            return False
        if await self._is_not_found_page(page, upc):
            return False
        if not await self._page_matches_query(page, upc):
            return False

        for selector in self.config.first_product_selectors:
            try:
                await page.wait_for_selector(selector, timeout=5000)
            except PlaywrightTimeout:
                pass

            link = page.locator(selector).first
            try:
                if not await link.count():
                    continue
                href = await link.get_attribute("href")
                if not href:
                    continue
                await page.goto(
                    clean_url(self.config.base_url, href),
                    wait_until="commit",
                    timeout=45000,
                )
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                return True
            except PlaywrightTimeout:
                if self._looks_like_product_url(page.url):
                    return True
                continue
        return False

    async def _page_matches_query(self, page: Page, upc: str) -> bool:
        if self._looks_like_product_url(page.url):
            return True
        if upc in page.url:
            return True
        try:
            title = await page.title()
            if upc in title:
                return True
        except Exception:
            pass
        try:
            text = await page.locator("body").inner_text(timeout=3000)
            return upc in text
        except Exception:
            return False

    async def _page_text_for_status(self, page: Page) -> str:
        pieces = []
        try:
            pieces.append(await page.title())
        except Exception:
            pass
        try:
            pieces.append(await page.locator("body").inner_text(timeout=3000))
        except Exception:
            pass
        return "\n".join(pieces).casefold()

    async def _is_blocked_page(self, page: Page) -> bool:
        text = await self._page_text_for_status(page)
        return any(marker.casefold() in text for marker in self.config.blocked_markers)

    async def _is_not_found_page(self, page: Page, upc: str) -> bool:
        text = await self._page_text_for_status(page)
        if upc not in text and upc not in page.url:
            return False
        return any(marker.casefold() in text for marker in self.config.no_result_markers)

    async def _page_status(self, page: Page, upc: str) -> str | None:
        if await self._is_blocked_page(page):
            return "blocked"
        if await self._is_not_found_page(page, upc):
            return "not_found"
        return None

    async def _extract_product(self, page: Page) -> dict | None:
        await page.wait_for_timeout(500)
        product = await product_json_ld(page)
        offer = json_ld_offer(product)

        title = product.get("name") if isinstance(product.get("name"), str) else None
        if not title:
            title = await meta_content(page, ["og:title", "twitter:title", "title"])
        if not title:
            title = await first_text(page, list(self.config.title_selectors))
        if title:
            title = re.sub(r"\s+", " ", title).strip()

        price = parse_price(offer.get("price"))
        if price is None:
            price = parse_price(
                await meta_content(
                    page,
                    [
                        "product:price:amount",
                        "og:price:amount",
                        "twitter:data1",
                    ],
                )
            )
        if price is None:
            price = parse_price(await first_text(page, list(self.config.price_selectors)))

        image = clean_url(self.config.base_url, json_ld_image(product))
        if not image:
            for selector, attribute in self.config.image_selectors:
                image = clean_url(
                    self.config.base_url,
                    await first_attribute(page, [selector], attribute),
                )
                if image:
                    break

        if price is None or not image or not title:
            return None
        return {
            "title": title,
            "image": image,
            "price_value": price,
            "link": page.url.split("#", 1)[0],
        }

    def _looks_like_product_url(self, url: str) -> bool:
        return "/products/" in url or "/p/" in url


async def scrape_with_retries(
    scraper: MarketplaceScraper,
    page: Page,
    upc: str,
    reference_price: float,
    retries: int = 2,
) -> dict | None:
    for attempt in range(retries):
        try:
            return await scraper.search(page, upc, reference_price)
        except Exception:
            if attempt + 1 == retries:
                return None
            await asyncio.sleep(0.5)
    return None


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_json_atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
    temporary.replace(path)
