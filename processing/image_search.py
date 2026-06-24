from __future__ import annotations

import argparse
import pickle
from pathlib import Path

from PIL import Image

from .embedding_builder import embed_clip
from .json_store import load_json
from .platform_paths import CLIP_INDEX, FINAL_TUPLES, METADATA_PKL


def search_image(image_path: Path, top_k: int = 50) -> dict | None:
    import faiss

    if not CLIP_INDEX.exists() or not METADATA_PKL.exists():
        raise RuntimeError("Build the CLIP index before running image search.")
    clip_index = faiss.read_index(str(CLIP_INDEX))
    with METADATA_PKL.open("rb") as handle:
        metadata = pickle.load(handle)
    image = Image.open(image_path).convert("RGB")
    clip_vector = embed_clip([image])
    count = min(max(1, top_k), clip_index.ntotal)
    clip_scores, clip_positions = clip_index.search(clip_vector.astype("float32"), count)
    tuples = load_json(FINAL_TUPLES, {"products": {}}).get("products", {})

    for score, pos in zip(clip_scores[0], clip_positions[0]):
        if pos < 0 or pos >= len(metadata):
            continue
        meta = metadata[int(pos)]
        if str(meta.get("source") or "").casefold() != "amazon":
            continue
        ean = str(meta.get("EAN") or "")
        if not ean:
            continue
        row = tuples.get(ean)
        if not row:
            continue
        return {
            "EAN": ean,
            "tuple": row,
        }
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Search final tuples by product image.")
    parser.add_argument("image", type=Path)
    parser.add_argument("--top-k", type=int, default=50)
    args = parser.parse_args()
    print(search_image(args.image, args.top_k))


if __name__ == "__main__":
    main()
