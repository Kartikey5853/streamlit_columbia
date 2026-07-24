from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from processing.catalog_engine import search_tuple_matches
from processing.json_store import load_json, products_by_ean
from processing.platform_paths import FINAL_TUPLES, AMAZON_PRODUCTS
from processing.unified_products import resolve_normalized_product


def _lookup_ean(value: str) -> dict | None:
    ean = re.sub(r"\D", "", value)
    if not ean:
        return None
    payload = load_json(FINAL_TUPLES, {"products": {}})
    row = payload.get("products", {}).get(ean) if isinstance(payload, dict) else None
    if row:
        return {"EAN": ean, "tuple": row}
    amazon = products_by_ean(load_json(AMAZON_PRODUCTS, {}))
    if ean in amazon:
        return {"EAN": ean, "tuple": {"amazon": amazon[ean]}}
    return None


def search(query: str, top_k: int, minimum_score: float) -> dict:
    query_path = Path(query)
    if query_path.is_file():
        result = search_tuple_matches(query_path, top_k=top_k, minimum_similarity=minimum_score)
        return {
            "query": query,
            "query_type": "image",
            "matches": result.get("matches", []),
        }
    normalized = resolve_normalized_product(query)
    if normalized:
        sku, row = normalized
        return {
            "query": query,
            "query_type": "sku_ean",
            "matches": [{
                "sku": sku,
                "tuple": row,
            }],
        }
    if re.fullmatch(r"\D*\d{12,13}\D*", query):
        match = _lookup_ean(query)
        return {
            "query": query,
            "query_type": "upc",
            "matches": [match] if match else [],
        }
    raise ValueError("Input must be an image path or a 12/13 digit UPC/EAN.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Search final tuples by image or UPC/EAN.")
    parser.add_argument("query", help="Image path or UPC/EAN number")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--minimum-score", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = search(args.query, args.top_k, args.minimum_score)
    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
