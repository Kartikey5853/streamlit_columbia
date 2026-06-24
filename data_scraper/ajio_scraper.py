import argparse
import asyncio
import json

from playwright.async_api import async_playwright

try:
    from .marketplace_common import MarketplaceConfig, MarketplaceScraper
except ImportError:
    from marketplace_common import MarketplaceConfig, MarketplaceScraper


AJIO = MarketplaceConfig(
    name="ajio",
    base_url="https://www.ajio.com",
    search_urls=(
        "https://www.ajio.com/search/?text={query}",
        "https://www.ajio.com/s/{query}",
    ),
    search_input_selectors=(
        "input[placeholder*='Search']",
        "input.react-autosuggest__input",
        "input[name='searchVal']",
        "input[type='text']",
        "input[type='search']",
    ),
    search_open_selectors=(
        "button[aria-label*='Search']",
        "[class*='search'] button",
        "[class*='search']",
    ),
    search_submit_selectors=(
        "button[aria-label='Search']",
        "button[type='submit']",
        "button.search__button",
    ),
    first_product_selectors=(
        "a[href*='/p/']",
        ".item a",
        ".rilrtl-products-list__item a",
    ),
    title_selectors=(
        ".prod-name",
        "h1.prod-name",
        "h1",
    ),
    price_selectors=(
        ".prod-sp",
        ".prod-cp",
        "[class*='price']",
    ),
    image_selectors=(
        ("meta[property='og:image']", "content"),
        (".img-container img", "src"),
        ("img[fetchpriority='high']", "src"),
    ),
    no_result_markers=(
        "Sorry! We couldn't find any matching items for",
    ),
    blocked_markers=(
        "Access Denied",
        "captcha",
    ),
)


class AjioScraper(MarketplaceScraper):
    def __init__(self):
        super().__init__(AJIO)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("upc")
    parser.add_argument("reference_price", type=float)
    parser.add_argument("--headless", action="store_true")
    parser.set_defaults(headless=False)
    args = parser.parse_args()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=args.headless)
        page = await browser.new_page(locale="en-IN")
        result = await AjioScraper().search(page, args.upc, args.reference_price)
        print(json.dumps(result, ensure_ascii=False))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
