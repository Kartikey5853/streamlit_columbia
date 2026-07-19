"""
Columbia Sportswear (Shopify) Product Scraper
================================================
Fetches the full product catalog from Columbia's public Shopify
products.json endpoint, paginating until the endpoint stops
returning a full page of results.

This is a direct Python port of a working JavaScript/fetch()
scraper -- the scraping logic (pagination, availability rule,
variant selection for SKU/price) is unchanged, only the runtime
and HTTP client differ.

Run with:
    python columbia_scraper.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ----------------------------------------------------------------
# Constants
# ----------------------------------------------------------------
BASE_URL: str = "https://www.columbiasportswear.co.in"
PRODUCTS_ENDPOINT: str = f"{BASE_URL}/products.json"
LIMIT: int = 250
REQUEST_DELAY_SECONDS: float = 0.5
MAX_RETRIES: int = 3
RETRY_BACKOFF_SECONDS: float = 2.0
OUTPUT_FILE: Path = Path("columbia_products.json")


def fetch_page(session: requests.Session, page: int) -> dict[str, Any]:
    """
    Fetch a single page of the products.json endpoint, retrying
    automatically on failure.

    Raises the last encountered exception if all retries are
    exhausted, so the caller can decide how to handle a hard failure.
    """
    params = {"limit": LIMIT, "page": page}
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(PRODUCTS_ENDPOINT, params=params, timeout=15)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as err:
            last_error = err
            print(f"  Attempt {attempt}/{MAX_RETRIES} failed for page {page}: {err}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    # All retries exhausted.
    assert last_error is not None
    raise last_error


def select_variant(variants: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Pick the variant used for SKU/price reporting:
      - the first available variant, if any exist
      - otherwise the first variant in the list
      - otherwise an empty dict
    """
    if not variants:
        return {}
    for variant in variants:
        if variant.get("available") is True:
            return variant
    return variants[0]


def is_any_variant_available(variants: list[dict[str, Any]]) -> bool:
    """True if at least one variant/size is available."""
    return any(variant.get("available") is True for variant in variants or [])


def normalize_product(product: dict[str, Any], scraped_at: str) -> dict[str, Any]:
    """Convert one raw Shopify product record into the flat output schema."""
    variants = product.get("variants") or []
    images = product.get("images") or []

    selected_variant = select_variant(variants)
    available = is_any_variant_available(variants)

    return {
        "product_id": str(product.get("id", "")),
        "source": "columbia",
        "brand": product.get("vendor", "") or "",
        "sku": selected_variant.get("sku", "") or "",
        "name": product.get("title", "") or "",
        "price": selected_variant.get("price", "") or "",
        "url": f"{BASE_URL}/products/{product.get('handle', '')}",
        "image_url": (images[0].get("src", "") if images else "") or "",
        "available": available,
        "scraped_at": scraped_at,
    }


def scrape_all_products() -> list[dict[str, Any]]:
    """
    Walk every page of the products.json endpoint and return the
    combined, normalized product list. Stops when a page returns
    zero products, or when a page returns fewer than LIMIT products
    (both signal the final page has been reached).
    """
    all_products: list[dict[str, Any]] = []
    page = 1

    with requests.Session() as session:
        session.headers.update({"Accept": "application/json"})

        while True:
            print(f"Fetching Page {page}...")

            try:
                data = fetch_page(session, page)
            except requests.RequestException as err:
                print(f"Page {page} failed after {MAX_RETRIES} retries: {err}")
                print("Stopping pagination due to unrecoverable error.")
                break

            products = data.get("products") or []

            if len(products) == 0:
                print("No more products. Stopping.")
                break

            scraped_at = datetime.now(timezone.utc).isoformat()
            for product in products:
                all_products.append(normalize_product(product, scraped_at))

            print(f"Fetched {len(products)} products")
            print(f"Page {page} processed | Total products collected: {len(all_products)}\n")

            if len(products) < LIMIT:
                print("Reached final page.")
                break

            page += 1
            time.sleep(REQUEST_DELAY_SECONDS)

    return all_products


def print_stats(all_products: list[dict[str, Any]]) -> None:
    """Print the final availability breakdown, matching the JS output format."""
    available = [p for p in all_products if p["available"] is True]
    unavailable = [p for p in all_products if p["available"] is False]

    print("--------------------------------")
    print("SCRAPING FINISHED")
    print(f"Total Products: {len(all_products)}")
    print(f"Available Products: {len(available)}")
    print(f"Unavailable Products: {len(unavailable)}")
    print("--------------------------------")


def save_to_json(all_products: list[dict[str, Any]], output_path: Path) -> None:
    """Write the collected products to disk as pretty-printed JSON."""
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(all_products, f, indent=2, ensure_ascii=False)
    print(f"Downloaded: {output_path.name}")


def main() -> None:
    all_products = scrape_all_products()
    print(f"Total collected: {len(all_products)}")
    print_stats(all_products)
    save_to_json(all_products, OUTPUT_FILE)


if __name__ == "__main__":
    main()