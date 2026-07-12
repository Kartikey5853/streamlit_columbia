from __future__ import annotations

import argparse
from pathlib import Path

from .catalog_engine import build_visual_index
from .platform_paths import CLIP_INDEX, METADATA_PKL, current_json_path
from .product_schema import MARKETPLACES


def build_indexes(inputs: list[Path], build_clip: bool = True, build_dinov2: bool = False) -> dict:
    if not build_clip:
        raise ValueError("This pipeline only supports CLIP embeddings.")
    # Build one shared index across all configured marketplaces.
    sources = [path for path in inputs if path.exists()]
    return build_visual_index(sources or [current_json_path(site) for site in MARKETPLACES])


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the shared visual FAISS index.")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--no-clip", action="store_true")
    args = parser.parse_args()
    print(build_indexes(args.inputs, not args.no_clip, False))


if __name__ == "__main__":
    main()