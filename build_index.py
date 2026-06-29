"""
build_index.py  —  Marqo FashionSigLIP + FAISS (URL version)
──────────────────────────────────────────────────────────────
Fetches images directly from URLs in products.json.
No local images directory needed.

Install:
    pip install open_clip_torch faiss-cpu torch torchvision pillow requests numpy

Usage:
    python build_index.py                     # all products
    python build_index.py --limit 50          # first 50 only (testing)
    python build_index.py --platform Myntra   # different platform folder
"""

import os
import sys
import json
import pickle
import argparse
import logging
import time
import requests
import numpy as np
import torch
import faiss
from PIL import Image
from io import BytesIO
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "json" / "amazon"
EMBEDDINGS_DIR = BASE_DIR / "data" / "embeddings"
LOG_DIR = BASE_DIR / "logs" / "amazon"

PRODUCTS_JSON     = DATA_DIR / "latest_amazon.json"
INDEX_FILE        = EMBEDDINGS_DIR / "embeddings_amazon.index"
METADATA_FILE     = EMBEDDINGS_DIR / "metadata_amazon.pkl"
TEXT_INDEX_FILE   = EMBEDDINGS_DIR / "embeddings_amazon_text.index"
TEXT_METADATA_FILE = EMBEDDINGS_DIR / "metadata_amazon_text.pkl"
EMBED_BATCH_SIZE  = 8
WATCH_BATCH_SIZE  = 25
POLL_INTERVAL     = 5.0
DEVICE            = "cuda"

# ── LIMIT SLIDER ──────────────────────────
# Set via --limit arg, or change default here
# None = process everything
# 50   = only first 50 products (useful for testing)
DEFAULT_LIMIT = None

logger = logging.getLogger("indexer")

# ─────────────────────────────────────────
# LOAD MODEL
# ─────────────────────────────────────────

def load_model():
    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA is required, but the installed PyTorch build cannot use it. "
            "Install the CUDA PyTorch wheels listed in requirements-amazon.txt."
        )
    logger.info("[>] Loading Marqo FashionSigLIP on %s...", DEVICE)
    logger.info("[>] GPU: %s", torch.cuda.get_device_name(0))
    import open_clip
    model, _, preprocess = open_clip.create_model_and_transforms(
        "hf-hub:Marqo/marqo-fashionSigLIP"
    )
    model = model.to(DEVICE).eval().half()
    tokenizer = open_clip.get_tokenizer("hf-hub:Marqo/marqo-fashionSigLIP")
    logger.info("[OK] Model ready.\n")
    return model, preprocess, tokenizer

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def fetch_image(url: str) -> Image.Image | None:
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception:
        return None


def embed_batch(model, preprocess, images: list) -> np.ndarray:
    tensors = torch.stack([preprocess(img) for img in images]).to(DEVICE)
    with torch.inference_mode():
        with torch.autocast(device_type="cuda", dtype=torch.float16):
            features = model.encode_image(tensors)
            features = torch.nn.functional.normalize(features, dim=1)
    result = features.cpu().numpy().astype("float32")
    del tensors, features
    torch.cuda.empty_cache()
    return result


def embed_text_batch(model, tokenizer, texts: list[str]) -> np.ndarray:
    tokens = tokenizer(texts).to(DEVICE)
    with torch.no_grad():
        features = model.encode_text(tokens)
        features = torch.nn.functional.normalize(features, dim=1)
    return features.cpu().numpy().astype("float32")


def setup_logging(log_path: str | None) -> None:
    handlers = [logging.StreamHandler(sys.stdout)]

    if log_path:
        log_file = Path(log_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def product_key(product: dict) -> str | None:
    return product.get("upc")


def load_products(products_path: Path) -> list[dict]:
    if not products_path.exists():
        return []
    with open(products_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("products"), dict):
        return list(data["products"].values())
    if isinstance(data, list):
        return data
    return []


def product_text(product: dict) -> str:
    return str(product.get("title") or product.get("name") or "").strip()


def load_existing_state(index_path: Path, metadata_path: Path):
    if index_path.exists() and metadata_path.exists():
        index = faiss.read_index(str(index_path))
        with open(metadata_path, "rb") as f:
            metadata = pickle.load(f)
        return index, metadata
    return None, []


def collect_processed_keys(metadata_list: list[dict]) -> set:
    processed = set()
    for entry in metadata_list:
        key = (
            entry.get("key")
            or entry.get("asin")
            or entry.get("product_id")
            or entry.get("url")
            or entry.get("image_url")
            or entry.get("name")
        )
        if key:
            processed.add(key)
    return processed


def save_index(index, metadata_list: list, index_path: Path, metadata_path: Path) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(index_path))
    with open(metadata_path, "wb") as f:
        pickle.dump(metadata_list, f)


def add_products_to_index(
    products: list[dict],
    model,
    preprocess,
    index,
    metadata_list: list,
) -> tuple[object, int, int]:
    embeddings_list = []
    batch_images = []
    batch_meta = []
    failed = 0

    def flush():
        if not batch_images:
            return
        embs = embed_batch(model, preprocess, batch_images)
        embeddings_list.append(embs)
        metadata_list.extend(batch_meta)
        batch_images.clear()
        batch_meta.clear()

    for product in products:
        name = product.get("title") or product.get("name", "")
        image_url = product.get("image_url", "")
        key = product_key(product)

        if not image_url:
            failed += 1
            logger.info("  X SKIP - no image URL  |  %s", name[:40])
            continue

        img = fetch_image(image_url)
        if img is None:
            failed += 1
            logger.info("  X FAIL - fetch failed  |  %s", name[:40])
            continue

        batch_images.append(img)
        batch_meta.append({
            "key": key,
            "upc": product.get("upc", ""),
            "title": product.get("title", name),
            "price": product.get("price", ""),
            "price_value": product.get("price_value"),
            "currency": product.get("currency", ""),
            "material_composition": product.get("material_composition", ""),
            "material_normalized": product.get("material_normalized", ""),
            "url": product.get("url", ""),
            "image_url": image_url,
        })

        if len(batch_images) >= EMBED_BATCH_SIZE:
            flush()

    flush()

    if not embeddings_list:
        return index, 0, failed

    embeddings = np.vstack(embeddings_list).astype("float32")
    dim = embeddings.shape[1]

    if index is None:
        index = faiss.IndexFlatIP(dim)

    index.add(embeddings)
    return index, embeddings.shape[0], failed


def add_products_to_text_index(
    products: list[dict],
    model,
    tokenizer,
    index,
    metadata_list: list,
) -> tuple[object, int, int]:
    texts = []
    batch_meta = []
    failed = 0

    for product in products:
        text = product_text(product)
        if not text:
            failed += 1
            continue
        texts.append(text)
        batch_meta.append({
            "key": product_key(product),
            "upc": product.get("upc", ""),
            "title": product.get("title", product.get("name", "")),
            "material_composition": product.get("material_composition", ""),
            "material_normalized": product.get("material_normalized", ""),
            "price": product.get("price", ""),
            "price_value": product.get("price_value"),
            "currency": product.get("currency", ""),
            "url": product.get("url", ""),
            "text": text,
        })

    if not texts:
        return index, 0, failed

    embeddings = embed_text_batch(model, tokenizer, texts)
    if index is None:
        index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)
    metadata_list.extend(batch_meta)
    return index, embeddings.shape[0], failed

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

def build_index_once(
    products_path: Path,
    index_path: Path,
    metadata_path: Path,
    text_index_path: Path,
    text_metadata_path: Path,
    limit: int | None,
):
    products = load_products(products_path)

    if not products:
        logger.info("[X] products.json not found at %s", products_path)
        sys.exit(1)

    if limit:
        products = products[:limit]
        logger.info("[>] Limit set to %s - processing first %s products.", limit, len(products))

    total = len(products)
    logger.info("[>] Total to process: %s", total)
    logger.info("=" * 58)

    model, preprocess, tokenizer = load_model()

    metadata_list: list[dict] = []
    start = datetime.now()
    index, added, failed = add_products_to_index(
        products,
        model,
        preprocess,
        None,
        metadata_list,
    )

    if index is not None:
        save_index(index, metadata_list, index_path, metadata_path)
    else:
        logger.info("[!] No images processed; continuing with text embeddings.")
    text_metadata_list: list[dict] = []
    text_index, text_added, text_failed = add_products_to_text_index(
        products,
        model,
        tokenizer,
        None,
        text_metadata_list,
    )
    if text_index is not None:
        save_index(text_index, text_metadata_list, text_index_path, text_metadata_path)

    elapsed = (datetime.now() - start).seconds
    logger.info("\n" + "=" * 58)
    logger.info("  OK  Done!")
    logger.info("  Indexed  : %s", len(metadata_list))
    logger.info("  Failed   : %s", failed)
    logger.info("  Time     : %sm %ss", elapsed // 60, elapsed % 60)
    logger.info("  Index    : %s", index_path)
    logger.info("  Metadata : %s", metadata_path)
    logger.info("  Text indexed: %s (failed: %s)", text_added, text_failed)
    logger.info("  Text index: %s", text_index_path)
    logger.info("=" * 58)


def watch_and_index(
    products_path: Path,
    index_path: Path,
    metadata_path: Path,
    text_index_path: Path,
    text_metadata_path: Path,
    batch_size: int,
    poll_interval: float,
):
    logger.info("[>] Watch mode enabled. Batch size: %s", batch_size)
    model, preprocess, tokenizer = load_model()

    index, metadata_list = load_existing_state(index_path, metadata_path)
    processed_keys = collect_processed_keys(metadata_list)
    text_index, text_metadata_list = load_existing_state(
        text_index_path,
        text_metadata_path,
    )
    processed_text_keys = collect_processed_keys(text_metadata_list)
    attempted_image_keys = set(processed_keys)
    attempted_text_keys = set(processed_text_keys)
    logger.info("[>] Existing vectors: %s", len(metadata_list))
    logger.info("[>] Existing text vectors: %s", len(text_metadata_list))

    while True:
        products = load_products(products_path)
        pending = [
            product for product in products
            if product_key(product) and product_key(product) not in attempted_image_keys
        ]
        pending_text = [
            product for product in products
            if product_key(product) and product_key(product) not in attempted_text_keys
        ]

        if not pending and not pending_text:
            time.sleep(poll_interval)
            continue

        batch = pending[:batch_size]
        if batch:
            attempted_image_keys.update(product_key(product) for product in batch)
            logger.info("[>] New images detected: %s (processing %s)", len(pending), len(batch))
            before = len(metadata_list)
            index, added, failed = add_products_to_index(
                batch,
                model,
                preprocess,
                index,
                metadata_list,
            )
            for entry in metadata_list[before:]:
                if entry.get("key"):
                    processed_keys.add(entry["key"])
            if added:
                save_index(index, metadata_list, index_path, metadata_path)
            logger.info("[OK] Image indexed: %s. Failed: %s", added, failed)

        text_batch = pending_text[:batch_size]
        if text_batch:
            attempted_text_keys.update(product_key(product) for product in text_batch)
            before = len(text_metadata_list)
            text_index, text_added, text_failed = add_products_to_text_index(
                text_batch,
                model,
                tokenizer,
                text_index,
                text_metadata_list,
            )
            for entry in text_metadata_list[before:]:
                if entry.get("key"):
                    processed_text_keys.add(entry["key"])
            if text_added:
                save_index(
                    text_index,
                    text_metadata_list,
                    text_index_path,
                    text_metadata_path,
                )
            logger.info("[OK] Text indexed: %s. Failed: %s", text_added, text_failed)

        time.sleep(1.0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="Only process first N products (default: all)")
    parser.add_argument("--products", type=str, default=str(PRODUCTS_JSON),
                        help="Path to products.json")
    parser.add_argument("--index", type=str, default=str(INDEX_FILE),
                        help="Path to embeddings index file")
    parser.add_argument("--metadata", type=str, default=str(METADATA_FILE),
                        help="Path to embeddings metadata file")
    parser.add_argument("--text-index", type=str, default=str(TEXT_INDEX_FILE),
                        help="Path to text embeddings index file")
    parser.add_argument("--text-metadata", type=str, default=str(TEXT_METADATA_FILE),
                        help="Path to text embeddings metadata file")
    parser.add_argument("--watch", action="store_true",
                        help="Watch products.json and incrementally index new products")
    parser.add_argument("--batch-size", type=int, default=WATCH_BATCH_SIZE,
                        help="Number of new products to embed per incremental batch")
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL,
                        help="Polling interval (seconds) for watch mode")
    parser.add_argument("--log", type=str, default=None,
                        help="Optional log file path")
    args = parser.parse_args()

    setup_logging(args.log)
    products_path = Path(args.products)
    index_path = Path(args.index)
    metadata_path = Path(args.metadata)
    text_index_path = Path(args.text_index)
    text_metadata_path = Path(args.text_metadata)

    if args.watch:
        watch_and_index(
            products_path=products_path,
            index_path=index_path,
            metadata_path=metadata_path,
            text_index_path=text_index_path,
            text_metadata_path=text_metadata_path,
            batch_size=args.batch_size,
            poll_interval=args.poll_interval,
        )
        return

    build_index_once(
        products_path=products_path,
        index_path=index_path,
        metadata_path=metadata_path,
        text_index_path=text_index_path,
        text_metadata_path=text_metadata_path,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
