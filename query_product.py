from __future__ import annotations

import argparse
import json
from pathlib import Path

from processing.catalog_engine import search_tuple_matches


def query(image_path: Path, top_k: int, minimum_similarity: float) -> dict:
    return search_tuple_matches(image_path, top_k=top_k, minimum_similarity=minimum_similarity)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Match an image and return Amazon/AJIO/Columbia/Adventuras links."
    )
    parser.add_argument("image", type=Path)
    parser.add_argument("--top-k", type=int, default=1)
    parser.add_argument("--minimum-similarity", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = query(args.image, args.top_k, args.minimum_similarity)
    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()