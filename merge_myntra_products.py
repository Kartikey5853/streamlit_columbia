import argparse
import json
import logging
import pickle
import re
import sys
from datetime import datetime
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path

import faiss
import numpy as np
import requests
import torch
from PIL import Image


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "json"
EMBEDDINGS_DIR = BASE_DIR / "data" / "embeddings"
AMAZON_PRODUCTS = DATA_DIR / "latest_amazon.json"
MYNTRA_PRODUCTS = DATA_DIR / "latest_myntra.json"
OUTPUT_FILE = DATA_DIR / "myntra_merged_products.json"

AMAZON_INDEX_FILE = EMBEDDINGS_DIR / "embeddings_amazon_merge.index"
AMAZON_METADATA_FILE = EMBEDDINGS_DIR / "metadata_amazon_merge.pkl"
MYNTRA_INDEX_FILE = EMBEDDINGS_DIR / "embeddings_myntra.index"
MYNTRA_METADATA_FILE = EMBEDDINGS_DIR / "metadata_myntra.pkl"

MODEL_NAME = "hf-hub:Marqo/marqo-fashionSigLIP"
BATCH_SIZE = 8

logger = logging.getLogger("myntra-merge")


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
    temporary.replace(path)


def load_amazon_products(path: Path) -> list[dict]:
    value = load_json(path, {})
    products = value.get("products", value) if isinstance(value, dict) else value
    if isinstance(products, dict):
        return [
            {"upc": str(upc), **product}
            for upc, product in products.items()
            if isinstance(product, dict)
        ]
    if isinstance(products, list):
        return [product for product in products if isinstance(product, dict)]
    return []


def load_myntra_products(path: Path) -> list[dict]:
    value = load_json(path, [])
    products = value.get("products", value) if isinstance(value, dict) else value
    return [product for product in products if isinstance(product, dict)]


def parse_price(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[\d,]+(?:\.\d+)?", str(value))
    return float(match.group(0).replace(",", "")) if match else None


def title_of(product: dict) -> str:
    return str(product.get("title") or product.get("name") or "").strip()


def image_of(product: dict) -> str:
    return str(product.get("image_url") or product.get("image") or "").strip()


def product_key(product: dict, source: str) -> str:
    if source == "amazon":
        return str(product.get("upc") or product.get("key") or "").strip()
    return str(product.get("product_id") or product.get("url") or "").strip()


def normalize_words(value: str) -> set[str]:
    stop_words = {"columbia", "men", "mens", "women", "womens", "unisex", "for"}
    words = re.findall(r"[a-z0-9]+", value.casefold())
    return {word for word in words if len(word) > 1 and word not in stop_words}


def title_score(left: str, right: str) -> float:
    left_words = normalize_words(left)
    right_words = normalize_words(right)
    if left_words or right_words:
        overlap = len(left_words & right_words) / max(1, len(left_words | right_words))
    else:
        overlap = 0.0
    sequence = SequenceMatcher(None, left.casefold(), right.casefold()).ratio()
    return round((overlap * 0.65) + (sequence * 0.35), 6)


def price_score(reference_price: float | None, candidate_price: float | None, tolerance: float) -> tuple[float, str]:
    if reference_price is None or candidate_price is None:
        return 0.0, "missing_price"
    difference = abs(reference_price - candidate_price)
    if difference <= 1:
        return 1.0, "exact_price"
    if difference <= tolerance:
        return max(0.0, 1.0 - (difference / tolerance)), "within_tolerance"
    return 0.0, "price_mismatch"


def fetch_image(url: str) -> Image.Image | None:
    try:
        response = requests.get(
            url,
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        return Image.open(BytesIO(response.content)).convert("RGB")
    except Exception:
        return None


def load_model():
    import open_clip

    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("Loading %s on %s", MODEL_NAME, device)
    model, _, preprocess = open_clip.create_model_and_transforms(MODEL_NAME)
    model = model.to(device).eval()
    if device == "cuda":
        model = model.half()
    return model, preprocess, device


def embed_images(model, preprocess, device: str, images: list[Image.Image]) -> np.ndarray:
    tensors = torch.stack([preprocess(image) for image in images]).to(device)
    with torch.inference_mode():
        if device == "cuda":
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                features = model.encode_image(tensors)
                features = torch.nn.functional.normalize(features, dim=1)
        else:
            features = model.encode_image(tensors)
            features = torch.nn.functional.normalize(features, dim=1)
    result = features.cpu().numpy().astype("float32")
    del tensors, features
    if device == "cuda":
        torch.cuda.empty_cache()
    return result


def metadata_for(product: dict, source: str) -> dict:
    price_value = parse_price(product.get("price_value") or product.get("price"))
    return {
        "source": source,
        "key": product_key(product, source),
        "upc": str(product.get("upc") or ""),
        "product_id": str(product.get("product_id") or ""),
        "brand": product.get("brand"),
        "title": title_of(product),
        "price": product.get("price"),
        "price_value": price_value,
        "url": product.get("url"),
        "image_url": image_of(product),
        "material_composition": product.get("material_composition"),
    }


def build_index(products: list[dict], source: str, model, preprocess, device: str):
    index = None
    metadata = []
    batch_images = []
    batch_metadata = []
    failed = 0

    def flush_batch():
        nonlocal index
        if not batch_images:
            return
        embeddings = embed_images(model, preprocess, device, batch_images)
        if index is None:
            index = faiss.IndexFlatIP(embeddings.shape[1])
        index.add(embeddings)
        metadata.extend(batch_metadata)
        batch_images.clear()
        batch_metadata.clear()

    for product in products:
        image_url = image_of(product)
        key = product_key(product, source)
        if not image_url or not key:
            failed += 1
            continue
        image = fetch_image(image_url)
        if image is None:
            failed += 1
            continue
        batch_images.append(image)
        batch_metadata.append(metadata_for(product, source))
        if len(batch_images) >= BATCH_SIZE:
            flush_batch()

    flush_batch()
    return index, metadata, failed


def save_index(index, metadata: list[dict], index_path: Path, metadata_path: Path) -> None:
    if index is None:
        raise RuntimeError(f"No vectors were built for {index_path.name}")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(index_path))
    with open(metadata_path, "wb") as handle:
        pickle.dump(metadata, handle)


def load_index(index_path: Path, metadata_path: Path):
    if not index_path.exists() or not metadata_path.exists():
        return None, []
    index = faiss.read_index(str(index_path))
    with open(metadata_path, "rb") as handle:
        metadata = pickle.load(handle)
    return index, metadata


def ensure_indexes(args: argparse.Namespace):
    amazon_index, amazon_metadata = load_index(
        Path(args.amazon_index),
        Path(args.amazon_metadata),
    )
    myntra_index, myntra_metadata = load_index(
        Path(args.myntra_index),
        Path(args.myntra_metadata),
    )

    if not args.rebuild_indexes and amazon_index is not None and myntra_index is not None:
        return amazon_index, amazon_metadata, myntra_index, myntra_metadata

    amazon_products = load_amazon_products(Path(args.amazon_products))
    myntra_products = load_myntra_products(Path(args.myntra_products))
    if args.limit:
        amazon_products = amazon_products[: args.limit]
    if args.myntra_limit:
        myntra_products = myntra_products[: args.myntra_limit]
    model, preprocess, device = load_model()

    logger.info("Building Amazon image index from %s products", len(amazon_products))
    amazon_index, amazon_metadata, amazon_failed = build_index(
        amazon_products,
        "amazon",
        model,
        preprocess,
        device,
    )
    logger.info("Building Myntra image index from %s products", len(myntra_products))
    myntra_index, myntra_metadata, myntra_failed = build_index(
        myntra_products,
        "myntra",
        model,
        preprocess,
        device,
    )
    save_index(amazon_index, amazon_metadata, Path(args.amazon_index), Path(args.amazon_metadata))
    save_index(myntra_index, myntra_metadata, Path(args.myntra_index), Path(args.myntra_metadata))
    logger.info("Index failures: amazon=%s, myntra=%s", amazon_failed, myntra_failed)
    return amazon_index, amazon_metadata, myntra_index, myntra_metadata


def candidate_score(amazon_product: dict, myntra_product: dict, clip_score: float, price_tolerance: float) -> dict:
    amazon_price = parse_price(amazon_product.get("price_value") or amazon_product.get("price"))
    myntra_price = parse_price(myntra_product.get("price_value") or myntra_product.get("price"))
    price_boost, price_status = price_score(amazon_price, myntra_price, price_tolerance)
    text_score = title_score(title_of(amazon_product), title_of(myntra_product))
    adjusted_score = min(1.0, clip_score + (0.13 * price_boost) + (0.05 * text_score))

    accepted = False
    reason = "below_threshold"
    if clip_score >= 0.80:
        accepted = True
        reason = "strong_clip"
    if price_status == "exact_price" and clip_score >= 0.72:
        accepted = True
        reason = "exact_price_boost"
    if price_status == "within_tolerance" and clip_score >= 0.85:
        accepted = True
        reason = "strong_clip_price_close"

    return {
        "clip_score": round(float(clip_score), 6),
        "title_score": text_score,
        "price_score": round(price_boost, 6),
        "adjusted_score": round(float(adjusted_score), 6),
        "price_status": price_status,
        "price_difference": (
            round(abs(amazon_price - myntra_price), 2)
            if amazon_price is not None and myntra_price is not None
            else None
        ),
        "accepted": accepted,
        "reason": reason,
    }


def merge_matches(args: argparse.Namespace) -> dict:
    amazon_index, amazon_metadata, myntra_index, myntra_metadata = ensure_indexes(args)
    if amazon_index.ntotal == 0 or myntra_index.ntotal == 0:
        raise RuntimeError("Both indexes must contain vectors before matching.")

    top_k = min(max(1, args.top_k), myntra_index.ntotal)
    products = {}
    matched = 0
    scores, positions = myntra_index.search(
        np.vstack([amazon_index.reconstruct(i) for i in range(amazon_index.ntotal)]).astype("float32"),
        top_k,
    )

    for amazon_position, amazon_product in enumerate(amazon_metadata):
        upc = str(amazon_product.get("upc") or amazon_product.get("key") or "")
        candidates = []
        for clip_score, myntra_position in zip(scores[amazon_position], positions[amazon_position]):
            if myntra_position < 0:
                continue
            myntra_product = myntra_metadata[myntra_position]
            score = candidate_score(
                amazon_product,
                myntra_product,
                float(clip_score),
                args.price_tolerance,
            )
            candidates.append({
                **score,
                "myntra": {
                    "product_id": myntra_product.get("product_id"),
                    "title": myntra_product.get("title"),
                    "price": myntra_product.get("price"),
                    "price_value": myntra_product.get("price_value"),
                    "image": myntra_product.get("image_url"),
                    "link": myntra_product.get("url"),
                },
            })

        accepted = [candidate for candidate in candidates if candidate["accepted"]]
        best = max(
            accepted,
            key=lambda item: (
                item["adjusted_score"],
                item["clip_score"],
                item["title_score"],
                item["price_score"],
            ),
            default=None,
        )
        if best:
            matched += 1

        products[upc] = {
            "upc": upc,
            "amazon": {
                "title": amazon_product.get("title"),
                "price": amazon_product.get("price"),
                "price_value": amazon_product.get("price_value"),
                "image": amazon_product.get("image_url"),
                "link": amazon_product.get("url"),
            },
            "myntra": best["myntra"] if best else None,
            "match": {
                key: best[key]
                for key in (
                    "clip_score",
                    "title_score",
                    "price_score",
                    "adjusted_score",
                    "price_status",
                    "price_difference",
                    "reason",
                )
            } if best else None,
            "candidates": candidates[: args.keep_candidates],
        }

    return {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(),
        "primary_key": "upc",
        "source_files": {
            "amazon": str(Path(args.amazon_products)),
            "myntra": str(Path(args.myntra_products)),
        },
        "rules": {
            "strong_clip_threshold": 0.80,
            "exact_price_min_clip": 0.72,
            "price_tolerance_inr": args.price_tolerance,
            "close_price_strong_clip_threshold": 0.85,
        },
        "summary": {
            "amazon_indexed": len(amazon_metadata),
            "myntra_indexed": len(myntra_metadata),
            "matched": matched,
            "unmatched": len(amazon_metadata) - matched,
        },
        "products": products,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Match UPC Amazon products to Myntra products with CLIP, title, and price scoring."
    )
    parser.add_argument("--amazon-products", default=str(AMAZON_PRODUCTS))
    parser.add_argument("--myntra-products", default=str(MYNTRA_PRODUCTS))
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    parser.add_argument("--amazon-index", default=str(AMAZON_INDEX_FILE))
    parser.add_argument("--amazon-metadata", default=str(AMAZON_METADATA_FILE))
    parser.add_argument("--myntra-index", default=str(MYNTRA_INDEX_FILE))
    parser.add_argument("--myntra-metadata", default=str(MYNTRA_METADATA_FILE))
    parser.add_argument("--rebuild-indexes", action="store_true")
    parser.add_argument("--limit", type=int, help="Only process the first N Amazon products.")
    parser.add_argument("--myntra-limit", type=int, help="Only process the first N Myntra products.")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--keep-candidates", type=int, default=5)
    parser.add_argument("--price-tolerance", type=float, default=1000.0)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    args = parse_args()
    merged = merge_matches(args)
    save_json(Path(args.output), merged)
    logger.info(
        "Wrote %s with %s matches and %s unmatched products",
        args.output,
        merged["summary"]["matched"],
        merged["summary"]["unmatched"],
    )


if __name__ == "__main__":
    main()
