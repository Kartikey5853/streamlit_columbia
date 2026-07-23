"""
Columbia Shopify GraphQL Scraper
================================

Downloads the complete Columbia Sportswear India catalogue directly
from the Shopify Storefront GraphQL API using cursor pagination.

Output:
    columbia_products.json

Schema:
    Version 3
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


# ============================================================
# CONFIGURATION
# ============================================================

STORE_NAME = "columbia-sportswear-india"

API_VERSION = "2025-07"

GRAPHQL_ENDPOINT = (
    f"https://{STORE_NAME}.myshopify.com/api/{API_VERSION}/graphql.json"
)

TOKEN = "23fa3b40e0a46129e1b0cfaff2262c99"

BASE_URL = "https://www.columbiasportswear.co.in"

OUTPUT_FILE = Path("columbia_products.json")

PAGE_SIZE = 250

REQUEST_DELAY = 0.30

MAX_RETRIES = 3

TIMEOUT = 30


HEADERS = {
    "Content-Type": "application/json",
    "X-Shopify-Storefront-Access-Token": TOKEN,
}


# ============================================================
# GRAPHQL QUERY
# ============================================================

GRAPHQL_QUERY = """
query GetProducts(
  $first: Int!
  $after: String
) {
  search(
    query: "tag:*"
    types: PRODUCT
    first: $first
    after: $after
  ) {

    edges {

      cursor

      node {

        ... on Product {

          id

          title

          handle

          vendor

          productType

          tags

          availableForSale

          createdAt

          updatedAt

          publishedAt

          totalInventory

          featuredImage {
            url
          }

          images(first:50){

            edges{

              node{

                url

                altText

              }

            }

          }

          collections(first:250){

            nodes{

              id

              title

              handle

            }

          }

          options{

            id

            name

            values

          }

          priceRange{

            minVariantPrice{

              amount

              currencyCode

            }

            maxVariantPrice{

              amount

              currencyCode

            }

          }

          compareAtPriceRange{

            minVariantPrice{

              amount

              currencyCode

            }

            maxVariantPrice{

              amount

              currencyCode

            }

          }

          variants(first:100){

            edges{

              node{

                id

                title

                sku

                barcode

                availableForSale

                quantityAvailable

                currentlyNotInStock

                weight

                weightUnit

                price{

                  amount

                  currencyCode

                }

                compareAtPrice{

                  amount

                  currencyCode

                }

                selectedOptions{

                  name

                  value

                }

              }

            }

          }

        }

      }

    }

    pageInfo{

      hasNextPage

      endCursor

    }

    totalCount

  }

}
"""


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def split_sku(sku: str |None) -> tuple[str, str]:

    if not sku:
        return "", ""

    sku = sku.strip().upper()

    parts = sku.split("-")

    if len(parts) <= 2:
        return sku, ""

    # Base SKU is always first two parts
    base = f"{parts[0]}-{parts[1]}"

    # Everything else is size / region / fit
    size = "-".join(parts[2:])

    return base, size

def graphql_request(
    session: requests.Session,
    cursor: str | None,
) -> dict[str, Any]:
    """
    Executes one GraphQL request.

    Returns the JSON response.
    """

    payload = {
        "query": GRAPHQL_QUERY,
        "variables": {
            "first": PAGE_SIZE,
            "after": cursor,
        },
    }

    last_exception = None

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            response = session.post(
                GRAPHQL_ENDPOINT,
                headers=HEADERS,
                json=payload,
                timeout=TIMEOUT,
            )

            response.raise_for_status()

            data = response.json()

            if "errors" in data:

                raise RuntimeError(
                    json.dumps(data["errors"], indent=2)
                )

            return data

        except Exception as exc:

            last_exception = exc

            print(
                f"Retry {attempt}/{MAX_RETRIES}"
            )

            if attempt < MAX_RETRIES:

                time.sleep(2 * attempt)

    raise last_exception


def money(node):

    if not node:
        return None

    amount = node.get("amount")

    if amount in (None, ""):
        return None

    return f"{float(amount):.2f}"

def parent_sku(sku: str | None) -> str:

    if not sku:
        return ""

    parts = sku.split("-")

    if len(parts) <= 1:
        return sku

    return "-".join(parts[:-1])



def money_value(node):

    if not node:
        return None

    amount = node.get("amount")

    if amount in (None, ""):
        return None

    return float(amount)


def utc_now():

    return datetime.now(timezone.utc).isoformat()


def output_timestamp():

    return datetime.now().replace(
        microsecond=0
    ).isoformat()


def output_date():

    return datetime.now().strftime(
        "%Y-%m-%d"
    )

# ============================================================
# PAGINATION
# ============================================================

def fetch_all_products() -> list[dict[str, Any]]:
    """
    Downloads every product from Shopify using cursor pagination.
    """

    all_products: list[dict[str, Any]] = []

    cursor: str | None = None

    page = 1

    with requests.Session() as session:

        while True:

            print(f"\nFetching Page {page}")

            data = graphql_request(
                session=session,
                cursor=cursor,
            )

            search = data["data"]["search"]

            edges = search["edges"]

            if not edges:
                print("No products returned.")
                break

            page_info = search["pageInfo"]

            total_count = search.get("totalCount")

            print(
                f"Downloaded {len(edges)} products "
                f"(Total collected: {len(all_products)+len(edges)})"
            )

            if total_count:
                print(f"Store reports approximately {total_count} products.")

            for edge in edges:

                node = edge.get("node")

                if node is not None:
                    all_products.append(node)

            if not page_info["hasNextPage"]:

                print("\nReached final page.")

                break

            cursor = page_info["endCursor"]

            page += 1

            time.sleep(REQUEST_DELAY)

    return all_products


# ============================================================
# VARIANT HELPERS
# ============================================================

def get_variant_nodes(
    product: dict[str, Any]
) -> list[dict[str, Any]]:

    return [
        edge["node"]
        for edge in product.get("variants", {})
                        .get("edges", [])
    ]


def first_available_variant(
    variants: list[dict[str, Any]]
) -> dict[str, Any]:

    if not variants:
        return {}

    for variant in variants:

        if variant.get("availableForSale"):

            return variant

    return variants[0]


def any_variant_available(
    variants: list[dict[str, Any]]
) -> bool:

    for variant in variants:

        if variant.get("availableForSale"):

            return True

    return False


def image_urls(
    product: dict[str, Any]
) -> list[str]:

    images = []

    for edge in product.get("images", {}).get("edges", []):

        node = edge.get("node", {})

        url = node.get("url")

        if url:

            images.append(url)

    return images


def featured_image(
    product: dict[str, Any]
) -> str | None:

    featured = product.get("featuredImage")

    if featured:

        return featured.get("url")

    images = image_urls(product)

    if images:

        return images[0]

    return None


def collection_names(
    product: dict[str, Any]
) -> list[str]:

    collections = []

    for c in product.get("collections", {}).get("nodes", []):

        title = c.get("title")

        if title:

            collections.append(title)

    return collections


def option_map(
    variant: dict[str, Any]
) -> dict[str, str]:

    result = {}

    for option in variant.get("selectedOptions", []):

        result[
            option.get("name", "")
        ] = option.get("value", "")

    return result


# ============================================================
# PRICE LOGIC
# ============================================================

def extract_prices(
    variant: dict[str, Any]
):

    compare = money(
        variant.get("compareAtPrice")
    )

    compare_value = money_value(
        variant.get("compareAtPrice")
    )

    selling = money(
        variant.get("price")
    )

    selling_value = money_value(
        variant.get("price")
    )

    # If compare-at price doesn't exist,
    # treat the selling price as the normal price.

    if compare is None:

        normal_price = selling
        normal_value = selling_value

        offer_price = None
        offer_value = None

        display_price = selling
        display_value = selling_value

    else:

        normal_price = compare
        normal_value = compare_value

        offer_price = selling
        offer_value = selling_value

        display_price = selling
        display_value = selling_value

    return (
        normal_price,
        offer_price,
        display_price,
        normal_value,
        offer_value,
        display_value,
    )


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_product(product: dict[str, Any]) -> dict[str, Any]:

    variants = get_variant_nodes(product)

    # -----------------------------------------
    # Build variant mapping
    # -----------------------------------------

    variant_mapping = []

    for variant in variants:

        full_sku = variant.get("sku", "")

        sku, size = split_sku(full_sku)

        variant_mapping.append({

            "sku": sku,

            "size": size,

            "ean": variant.get("barcode")

        })

    # -----------------------------------------
    # Choose representative variant
    # -----------------------------------------

    available_variants = [
        v
        for v in variants
        if v.get("availableForSale")
    ]

    if available_variants:

        selected_variant = min(
            available_variants,
            key=lambda v: float(v["price"]["amount"])
        )

    else:

        selected_variant = variants[0] if variants else {}

    available = any_variant_available(variants)

    image = featured_image(product)

    images = image_urls(product)

    (
        normal_price,
        offer_price,
        display_price,
        normal_value,
        offer_value,
        display_value,
    ) = extract_prices(selected_variant)

    scraped_at = utc_now()

    raw = {

        "product_id": str(product.get("id", "")),

        "source": "columbia",

        "brand": product.get("vendor"),

        "sku": selected_variant.get("sku"),

        "barcode": selected_variant.get("barcode"),

        "name": product.get("title"),

        "price": display_price,

        "normal_price": normal_price,

        "offer_price": offer_price,

        "url": f"{BASE_URL}/products/{product.get('handle')}",

        "image_url": image,

        "available": available,

        "scraped_at": scraped_at,

    }

    return {

        "source": "columbia",

        "source_product_id": str(product.get("id")),

        "ean": selected_variant.get("barcode"),

        "variant_mapping": variant_mapping,

        "title": product.get("title"),

        "url": f"{BASE_URL}/products/{product.get('handle')}",

        "image_url": image,

        "image_urls": images,

        "normal_price": normal_price,

        "offer_price": offer_price,

        "price": display_price,

        "normal_price_value": normal_value,

        "offer_price_value": offer_value,

        "price_value": display_value,

        "availability": available,

        "scraped_at": scraped_at,

        "raw": raw,

    }

def normalize_all(graphql_products):

    normalized = []

    total = len(graphql_products)

    for index, product in enumerate(graphql_products, start=1):

        if index % 100 == 0 or index == total:
            print(f"Normalizing {index}/{total}")

        vendor = (product.get("vendor") or "").strip().lower()

        if vendor != "columbia":
            continue

        normalized.append(
            normalize_product(product)
        )

    return normalized


# ============================================================
# OUTPUT DOCUMENT
# ============================================================

def build_output(
    products: list[dict[str, Any]]
) -> dict[str, Any]:

    return {

        "schema_version": 3,

        "source": "columbia",

        "scrape_date": output_date(),

        "updated_at": output_timestamp(),

        "products": products,

    }


# ============================================================
# STATISTICS
# ============================================================

def print_statistics(
    products: list[dict[str, Any]]
):

    available = sum(
        1
        for p in products
        if p["availability"]
    )

    unavailable = len(products) - available

    discounted = sum(
        1
        for p in products
        if p["offer_price"] is not None
    )

    print()

    print("=" * 50)

    print("SCRAPING COMPLETE")

    print("=" * 50)

    print(f"Total Products      : {len(products)}")

    print(f"Available Products  : {available}")

    print(f"Unavailable         : {unavailable}")

    print(f"Discounted Products : {discounted}")

    print("=" * 50)

    print()

    # ============================================================
# SAVE OUTPUT
# ============================================================

def save_json(
    output: dict[str, Any],
    output_file: Path = OUTPUT_FILE,
) -> None:

    with output_file.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            output,
            f,
            indent=2,
            ensure_ascii=False,
        )

    print(f"\nSaved JSON -> {output_file}")


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print(" Columbia Shopify GraphQL Scraper")
    print("=" * 60)

    start = time.time()

    # --------------------------------------------------------
    # Download every product
    # --------------------------------------------------------

    graphql_products = fetch_all_products()

    print()
    print(f"Downloaded {len(graphql_products)} GraphQL products.")

    # --------------------------------------------------------
    # Normalize
    # --------------------------------------------------------

    normalized_products = normalize_all(
        graphql_products
    )

    output = build_output(
        normalized_products
    )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    save_json(output)

    # --------------------------------------------------------
    # Stats
    # --------------------------------------------------------

    print_statistics(
        normalized_products
    )

    elapsed = time.time() - start

    print(
        f"Finished in {elapsed:.2f} seconds."
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()