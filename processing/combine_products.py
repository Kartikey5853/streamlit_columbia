from __future__ import annotations

import argparse
from pathlib import Path

from .embedding_builder import build_indexes
from .matcher import build_tuples
from .platform_paths import AMAZON_PRODUCTS, MARKETPLACE_PRODUCTS, MYNTRA_PRODUCTS, TATACLIQ_PRODUCTS


def run_pipeline(skip_embeddings: bool = False) -> dict:
    inputs = [AMAZON_PRODUCTS, MARKETPLACE_PRODUCTS, MYNTRA_PRODUCTS, TATACLIQ_PRODUCTS]
    result = {}
    if not skip_embeddings:
        result["embeddings"] = build_indexes([path for path in inputs if path.exists()])
    tuples = build_tuples()
    result["tuples"] = tuples["summary"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Combine products, build embeddings, and export final tuples.")
    parser.add_argument("--skip-embeddings", action="store_true")
    args = parser.parse_args()
    print(run_pipeline(args.skip_embeddings))


if __name__ == "__main__":
    main()
