from __future__ import annotations

import argparse
from pathlib import Path

from .catalog_engine import search_image_as_tuple, search_tuple_matches_batch


def search_image(image_path: Path, top_k: int = 50) -> dict | None:
    return search_image_as_tuple(image_path, top_k=top_k)


def search_images(image_paths: list[Path], top_k: int = 5, minimum_similarity: float = 0.0) -> dict:
    return search_tuple_matches_batch(image_paths, top_k=top_k, minimum_similarity=minimum_similarity)


def main() -> None:
    parser = argparse.ArgumentParser(description="Search final tuples by product image.")
    parser.add_argument("image", type=Path)
    parser.add_argument("--top-k", type=int, default=50)
    args = parser.parse_args()
    print(search_image(args.image, args.top_k))


if __name__ == "__main__":
    main()