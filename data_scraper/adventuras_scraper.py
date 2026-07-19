"""
Adventuras Columbia Product Scraper
------------------------------------
Fetches product data from the Adventuras Shopify storefront endpoint
(https://adventuras.in/products.json), filters for Columbia-branded
products, and saves the results to a JSON file.

This is a direct Python port of an existing, working JavaScript scraper.
The scraping logic (pagination, filtering, availability calculation,
SKU/price selection, and output structure) is preserved exactly -- only
the implementation language and HTTP client have changed.

Requirements: Python 3.12+, requests
    pip install requests

Usage:
    python adventuras_scraper.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

BASE_URL: str = "https://adventuras.in"
LIMIT: int = 250
MAX_RETRIES: int = 3
REQUEST_DELAY_SECONDS: float = 0.5
REQUEST_TIMEOUT_SECONDS: int = 15
TARGET_VENDOR: str = "columbia"
OUTPUT_FILE: str = "adventuras_columbia_products.json"


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def fetch_page(session: requests.Session, page: int) -> dict[str, Any]:
    """
    Fetch a single page of products from the Shopify products.json endpoint.

    Retries up to MAX_RETRIES times on request failures or non-2xx
    responses, with a short backoff between attempts.

    Args:
        session: Shared requests.Session for connection reuse.
        page: The page number to fetch.

    Returns:
        The parsed JSON response body as a dict.

    Raises:
        requests.HTTPError: If all retry attempts fail.
    """
    url = f"{BASE_URL}/products.json"
    params = {"limit": LIMIT, "page": page}

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            print(f"  Attempt {attempt}/{MAX_RETRIES} failed for page {page}: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY_SECONDS)

    # All retries exhausted.
    raise requests.HTTPError(
        f"Failed to fetch page {page} after {MAX_RETRIES} attempts"
    ) from last_error


# --------------------------------------------------------------------------
# Product processing helpers
# --------------------------------------------------------------------------

def is_columbia_product(product: dict[str, Any]) -> bool:
    """
    Check whether a product belongs to the Columbia brand.

    Mirrors the JS check:
        !product.vendor || product.vendor.toLowerCase().trim() !== "columbia"
    """
    vendor = product.get("vendor")
    if not vendor:
        return False
    return vendor.strip().lower() == TARGET_VENDOR


def compute_availability(variants: list[dict[str, Any]]) -> bool:
    """
    Return True if at least one variant is available, matching the JS:
        product.variants?.some(variant => variant.available === true) ?? false
    """
    return any(variant.get("available") is True for variant in variants)


def select_variant(variants: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Select the variant to pull SKU/price from.

    Mirrors the JS logic:
        - first variant where available === true
        - otherwise, the first variant in the list
        - otherwise, an empty object
    """
    for variant in variants:
        if variant.get("available") is True:
            return variant
    if variants:
        return variants[0]
    return {}


def build_product_record(product: dict[str, Any], scraped_at: str) -> dict[str, Any]:
    """
    Transform a raw Shopify product dict into the output record shape.
    """
    variants = product.get("variants") or []
    images = product.get("images") or []

    available = compute_availability(variants)
    selected_variant = select_variant(variants)

    return {
        "product_id": str(product.get("id")),
        "source": "adventuras",
        "brand": product.get("vendor") or "",
        "sku": selected_variant.get("sku") or "",
        "name": product.get("title") or "",
        "price": selected_variant.get("price") or "",
        "url": f"{BASE_URL}/products/{product.get('handle')}",
        "image_url": (images[0].get("src") if images else "") or "",
        "available": available,
        "scraped_at": scraped_at,
    }


# --------------------------------------------------------------------------
# Main scraping routine
# --------------------------------------------------------------------------

def scrape_columbia_products() -> list[dict[str, Any]]:
    """
    Paginate through the Adventuras products.json endpoint, collecting
    all Columbia-branded products until an empty page or a short page
    (fewer than LIMIT products) is encountered.

    Returns:
        A list of Columbia product records.
    """
    all_products: list[dict[str, Any]] = []
    page = 1

    with requests.Session() as session:
        # A reasonable default header set; harmless for a public JSON endpoint.
        session.headers.update({"Accept": "application/json"})

        while True:
            print(f"Fetching Page {page}...")

            data = fetch_page(session, page)
            products = data.get("products") or []

            if not products:
                print("No more products. Stopping.")
                break

            scraped_at = datetime.now(timezone.utc).isoformat()
            print(f"Fetched {len(products)} products")

            for product in products:
                if not is_columbia_product(product):
                    continue
                all_products.append(build_product_record(product, scraped_at))

            print(f"Columbia products collected: {len(all_products)}")

            if len(products) < LIMIT:
                print("Reached final page.")
                break

            page += 1
            time.sleep(REQUEST_DELAY_SECONDS)

    return all_products


def print_statistics(products: list[dict[str, Any]]) -> None:
    """Print final scraping statistics."""
    available = [p for p in products if p["available"] is True]
    unavailable = [p for p in products if p["available"] is False]

    print("--------------------------------")
    print("SCRAPING FINISHED")
    print(f"Total Columbia Products: {len(products)}")
    print(f"Available Products: {len(available)}")
    print(f"Unavailable Products: {len(unavailable)}")
    print("--------------------------------")


def save_products(products: list[dict[str, Any]], output_path: Path) -> None:
    """Save the product list to a JSON file with pretty formatting."""
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)
    print(f"Saved: {output_path}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    products = scrape_columbia_products()
    print_statistics(products)
    save_products(products, Path(OUTPUT_FILE))


if __name__ == "__main__":
    main()
