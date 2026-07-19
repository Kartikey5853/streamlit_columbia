"""
Myntra Scraper Wrapper (Playwright)
================================================
Does NOT reimplement or modify the scraping logic. This script only:

  1. Launches a real Chrome browser (hardened against basic
     automation-detection signals).
  2. Navigates to the Myntra Columbia listing page.
  3. Reads your existing, unmodified myntra_scraper.js from disk.
  4. Injects it into the live page as a real <script> tag, so it runs
     exactly as if you'd pasted it into DevTools console yourself.
  5. Polls until the script signals completion (it defines
     window.downloadMyntraJSON only once scraping has finished -- this
     wrapper uses that as the done signal instead of touching the
     script itself).
  6. Reads window.__MYNTRA_PRODUCTS__ out of the page and saves it to
     myntra_columbia_products.json via Python.
  7. Always closes the browser afterwards, even on error.

Requirements:
    pip install playwright
    playwright install chrome

Run with:
    python myntra_scraper_wrapper.py
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

# ----------------------------------------------------------------
# Constants
# ----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
MYNTRA_URL: str = "https://www.myntra.com/columbia"
JS_SCRAPER_PATH: Path = BASE_DIR / "myntra_scraper.js"
OUTPUT_FILE: Path = BASE_DIR / "myntra_columbia_products.json"

HEADLESS: bool = False              # keep visible until you've confirmed it works
PAGE_LOAD_TIMEOUT_MS: int = 30_000
POST_LOAD_SETTLE_SECONDS: float = 3.0
POLL_INTERVAL_SECONDS: float = 5.0
MAX_WAIT_SECONDS: float = 60 * 60   # 1 hour hard ceiling for the whole scrape

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("myntra_wrapper")


def load_js_scraper(js_path: Path) -> str:
    """Read the existing JS scraper file, unmodified, from disk."""
    if not js_path.exists():
        raise FileNotFoundError(
            f"Could not find {js_path}. Place myntra_scraper.js next to this "
            f"script, or update JS_SCRAPER_PATH."
        )
    return js_path.read_text(encoding="utf-8")


def forward_browser_console_to_log(page: Page) -> None:
    """Pipe the page's own console.log output into Python's logger, so
    you see the scraper's normal progress messages (page numbers,
    pagination-context status, duplicate counts, etc.) right in the
    terminal."""
    def handle_console(msg):
        try:
            log.info(f"[browser console] {msg.text}")
        except Exception:
            pass
    page.on("console", handle_console)


def launch_hardened_browser(p, headless: bool):
    """Launch real Chrome with basic automation-detection signals
    disabled."""
    browser = p.chromium.launch(
        headless=headless,
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    return browser, context


def wait_for_completion(page: Page, max_wait_seconds: float, poll_interval_seconds: float) -> bool:
    """
    Poll the page until the injected scraper signals it has finished.
    The scraper (unmodified) defines window.downloadMyntraJSON only
    after it has fully finished collecting products, so that
    function's existence is used as the completion signal without
    touching the script itself.

    Returns True if completion was detected, False if max_wait_seconds
    was reached first (in which case the caller should still try to
    salvage whatever is in window.__MYNTRA_PRODUCTS__ so far).
    """
    start = time.monotonic()

    while time.monotonic() - start < max_wait_seconds:
        is_done = page.evaluate("typeof window.downloadMyntraJSON === 'function'")
        if is_done:
            return True

        count = page.evaluate(
            "window.__MYNTRA_PRODUCTS__ ? window.__MYNTRA_PRODUCTS__.length : 0"
        )
        last_page = page.evaluate("window.__MYNTRA_LAST_PAGE__ || null")
        log.info(f"Still scraping... unique products so far: {count} | last page: {last_page}")

        time.sleep(poll_interval_seconds)

    return False


def extract_products(page: Page) -> list[dict]:
    """Pull the final deduplicated product list out of the page's window object."""
    return page.evaluate("window.__MYNTRA_PRODUCTS__ || []")


def save_to_json(products: list[dict], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)
    log.info(f"Saved {len(products)} products to {output_path.name}")


def run(headless: bool = HEADLESS) -> list[dict]:
    js_code = load_js_scraper(JS_SCRAPER_PATH)
    log.info(f"Loaded scraper script ({len(js_code)} characters) from {JS_SCRAPER_PATH}")

    with sync_playwright() as p:
        browser, context = launch_hardened_browser(p, headless)
        page = context.new_page()
        forward_browser_console_to_log(page)

        try:
            log.info(f"Navigating to {MYNTRA_URL} ...")
            page.goto(MYNTRA_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

            log.info(f"Page loaded. Waiting {POST_LOAD_SETTLE_SECONDS}s for the page to settle...")
            page.wait_for_timeout(int(POST_LOAD_SETTLE_SECONDS * 1000))

            log.info("Injecting myntra_scraper.js into the page (unmodified)...")
            page.add_script_tag(content=js_code)

            log.info("Waiting for scraper to finish (this can take a while across many pages)...")
            completed = wait_for_completion(page, MAX_WAIT_SECONDS, POLL_INTERVAL_SECONDS)

            if not completed:
                log.warning(
                    f"Reached the {MAX_WAIT_SECONDS}s wait ceiling before the scraper "
                    f"signaled completion. Salvaging whatever was collected so far."
                )

            products = extract_products(page)
            log.info(f"Retrieved {len(products)} products from the page.")
            return products

        except PlaywrightTimeoutError as err:
            log.error(f"Timed out waiting on the page: {err}")
            try:
                return extract_products(page)
            except Exception:
                return []

        except Exception as err:
            log.error(f"Unexpected error during scraping: {err}")
            try:
                return extract_products(page)
            except Exception:
                return []

        finally:
            log.info("Closing browser.")
            context.close()
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Myntra scraper wrapper.")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headed", action="store_false", dest="headless")
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE)
    parser.set_defaults(headless=HEADLESS)
    args = parser.parse_args()
    try:
        products = run(headless=args.headless)
    except FileNotFoundError as err:
        log.error(str(err))
        sys.exit(1)

    save_to_json(products, args.output)
    log.info(f"Done. Total products: {len(products)}")


if __name__ == "__main__":
    main()
