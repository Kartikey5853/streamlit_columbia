"""
Tata CLiQ Columbia Product Scraper
-----------------------------------
Fetches Columbia-branded product listings from the Tata CLiQ search API
(https://searchbff.tatacliq.com/products/mpl/search), paginates through
every result page, and saves the combined dataset to a JSON file.

This is a direct Python port of an existing, working JavaScript scraper.
The scraping logic (query parameters, pagination, retry behavior, field
mapping, availability/price logic, and output structure) is preserved
exactly -- only the implementation language and HTTP client have changed.

Requirements: Python 3.12+, requests
    pip install requests

Usage:
    python tatacliq_scraper.py
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

BASE_URL: str = "https://searchbff.tatacliq.com/products/mpl/search"
PRODUCT_BASE_URL: str = "https://www.tatacliq.com"
PAGE_SIZE: int = 40
MAX_RETRIES: int = 3
RETRY_WAIT_SECONDS: float = 5.0
REQUEST_TIMEOUT_SECONDS: int = 15
OUTPUT_FILE: str = "tatacliq_columbia_products.json"

# Captured session value from the original JS scraper. Kept as-is to
# preserve the exact request format; may need refreshing if it expires.
MCVID: str = "17842193248194609071977202052631231983"


# --------------------------------------------------------------------------
# Delay helpers
# --------------------------------------------------------------------------

def random_delay_seconds() -> float:
    """
    Return a randomized delay in seconds, matching the JS:
        1500 + Math.floor(Math.random() * 2000)   // milliseconds
    """
    return (1500 + random.randint(0, 1999)) / 1000.0


# --------------------------------------------------------------------------
# Request building / HTTP helpers
# --------------------------------------------------------------------------

def build_params(page: int) -> dict[str, str]:
    """
    Build the query parameters for a given page, matching the JS
    scraper's URLSearchParams exactly.
    """
    return {
        "searchText": "columbia:relevance:inStockFlag:true",
        "isKeywordRedirect": "false",
        "isKeywordRedirectEnabled": "false",
        "channel": "WEB",
        "isMDE": "true",
        "isTextSearch": "false",
        "isFilter": "false",
        "qc": "false",
        "test": "invizbff.qpsv3-inviz.ab",
        "page": str(page),
        "mcvid": MCVID,
        "customerId": "",
        "isSuggested": "false",
        "isFilterDataRequired": "true",
        "isPwa": "true",
        "pageSize": str(PAGE_SIZE),
        "typeID": "all",
    }


def fetch_page(session: requests.Session, page: int) -> dict[str, Any]:
    """
    Fetch a single page of search results.

    Raises:
        requests.RequestException / ValueError: on network error or bad JSON.
        requests.HTTPError: on a non-2xx response.
    """
    response = session.get(
        BASE_URL,
        params=build_params(page),
        headers={"Accept": "application/json"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not response.ok:
        raise requests.HTTPError(f"HTTP {response.status_code}")
    return response.json()


def fetch_page_with_retries(session: requests.Session, page: int) -> dict[str, Any] | None:
    """
    Fetch a page, retrying up to MAX_RETRIES times with a fixed wait
    between attempts. Returns None (without raising) if all attempts fail,
    so the caller can skip the page and keep going.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"Page {page + 1} | Attempt {attempt}")
            return fetch_page(session, page)
        except (requests.RequestException, ValueError) as exc:
            print(f"Retry {attempt} failed on page {page + 1}: {exc}")
            time.sleep(RETRY_WAIT_SECONDS)
    return None


# --------------------------------------------------------------------------
# Product processing helpers
# --------------------------------------------------------------------------

def select_price(product: dict[str, Any]) -> str:
    """
    Select the display price for a product.

    Mirrors the JS logic:
        p.price?.sellingPrice?.formattedValue
            || p.price?.mrpPrice?.formattedValue
            || ""
    """
    price = product.get("price") or {}
    selling_price = price.get("sellingPrice") or {}
    mrp_price = price.get("mrpPrice") or {}
    return (
        selling_price.get("formattedValue")
        or mrp_price.get("formattedValue")
        or ""
    )


def build_product_record(product: dict[str, Any], scraped_at: str) -> dict[str, Any]:
    """
    Transform a raw Tata CLiQ product dict into the output record shape.
    """
    product_id = product.get("productId")
    web_url = product.get("webURL")
    image_url = product.get("imageURL")

    return {
        "product_id": str(product_id) if product_id else "",
        "source": "tatacliq",
        "brand": product.get("brandname") or "Unknown",
        "sku": (
            product.get("code")
            or product.get("productCode")
            or product.get("articleNumber")
            or ""
        ),
        "name": product.get("productname"),
        "price": select_price(product),
        "url": f"{PRODUCT_BASE_URL}{web_url}" if web_url else "",
        "image_url": f"https:{image_url}" if image_url else "",
        "available": bool(product.get("inStockFlag")),
        "scraped_at": scraped_at,
    }


def process_products(products: list[dict[str, Any]], clean_products: list[dict[str, Any]]) -> None:
    """Append processed records for a page's products onto clean_products."""
    scraped_at = datetime.now(timezone.utc).isoformat()
    for product in products:
        clean_products.append(build_product_record(product, scraped_at))


# --------------------------------------------------------------------------
# Main scraping routine
# --------------------------------------------------------------------------

def scrape_columbia_products() -> tuple[list[dict[str, Any]], int, int]:
    """
    Fetch the first page to determine total pages/results, then paginate
    through every remaining page, collecting cleaned product records.

    Returns:
        A tuple of (clean_products, successful_pages, failed_pages).
    """
    clean_products: list[dict[str, Any]] = []
    successful_pages = 0
    failed_pages = 0

    with requests.Session() as session:
        print("Fetching first page...")
        first = fetch_page(session, 0)

        pagination = first.get("pagination") or {}
        total_pages = pagination.get("totalPages", 0)
        total_results = pagination.get("totalResults", 0)
        print(f"Found {total_pages} pages, {total_results} total results")

        process_products(first.get("searchresult") or [], clean_products)
        successful_pages += 1
        print(f"Collected {len(clean_products)} products")

        for page in range(1, total_pages):
            data = fetch_page_with_retries(session, page)

            if data is None:
                print(f"Skipped page {page + 1}")
                failed_pages += 1
            else:
                process_products(data.get("searchresult") or [], clean_products)
                successful_pages += 1
                print(f"Total Products: {len(clean_products)}")

            time.sleep(random_delay_seconds())

    return clean_products, successful_pages, failed_pages


def print_statistics(products: list[dict[str, Any]], successful_pages: int, failed_pages: int) -> None:
    """Print final scraping statistics."""
    print("--------------------------------")
    print("SCRAPING FINISHED")
    print(f"Total Products: {len(products)}")
    print(f"Successful Pages: {successful_pages}")
    print(f"Failed Pages: {failed_pages}")
    print("--------------------------------")


def save_products(products: list[dict[str, Any]], output_path: Path) -> None:
    """Save the product list to a JSON file with pretty formatting."""
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)
    print(f"Downloaded: {output_path}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    products, successful_pages, failed_pages = scrape_columbia_products()
    print_statistics(products, successful_pages, failed_pages)
    save_products(products, Path(OUTPUT_FILE))


if __name__ == "__main__":
    main()
