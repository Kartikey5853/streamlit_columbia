from __future__ import annotations

import argparse
import pickle
from io import BytesIO
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from .json_store import load_json, product_list
from .platform_paths import CLIP_INDEX, EMBEDDINGS_DIR, METADATA_PKL, PRODUCTS_PKL
from .product_schema import price_value


def fetch_image(url: str) -> Image.Image | None:
    try:
        response = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        return Image.open(BytesIO(response.content)).convert("RGB")
    except Exception:
        return None


def normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return (vectors / norms).astype("float32")


def load_clip():
    import open_clip
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, _, preprocess = open_clip.create_model_and_transforms("hf-hub:Marqo/marqo-fashionSigLIP")
    return model.to(device).eval(), preprocess, device, torch


def embed_clip(images: list[Image.Image]) -> np.ndarray:
    model, preprocess, device, torch = load_clip()
    tensors = torch.stack([preprocess(image) for image in images]).to(device)
    with torch.no_grad():
        features = model.encode_image(tensors)
        features = torch.nn.functional.normalize(features, dim=1)
    return features.cpu().numpy().astype("float32")


def product_image(product: dict) -> str | None:
    return product.get("image") or product.get("image_url")


def metadata_for(product: dict, source: str, ean: str | None = None) -> dict:
    return {
        "source": source,
        "EAN": ean or product.get("EAN") or product.get("ean") or product.get("upc"),
        "product_id": product.get("product_id"),
        "title": product.get("title") or product.get("name"),
        "price": product.get("price"),
        "price_value": price_value(product.get("price_value") or product.get("price")),
        "url": product.get("url") or product.get("link"),
        "image": product_image(product),
    }


def collect_products(inputs: list[Path]) -> tuple[list[dict], list[dict]]:
    products: list[dict] = []
    metadata: list[dict] = []
    for path in inputs:
        source = path.stem.replace("_products", "")
        payload = load_json(path, {})
        if isinstance(payload, dict) and isinstance(payload.get("products"), dict):
            iterator = []
            for key, value in payload["products"].items():
                if not isinstance(value, dict):
                    continue
                if any(site in value for site in ["amazon", "ajio", "myntra", "tatacliq"]):
                    for site, card in value.items():
                        if site in {"EAN", "upc", "updated_at", "match"} or not isinstance(card, dict):
                            continue
                        iterator.append((site, key, card))
                else:
                    iterator.append((source, key, value))
        else:
            iterator = [(source, None, item) for item in product_list(payload)]
        for site, ean, product in iterator:
            if not product_image(product):
                continue
            products.append(product)
            metadata.append(metadata_for(product, site, ean))
    return products, metadata


def build_indexes(inputs: list[Path], build_clip: bool = True, build_dinov2: bool = False) -> dict:
    import faiss

    EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)
    products, metadata = collect_products(inputs)
    images = []
    kept_products = []
    kept_metadata = []
    for product, meta in zip(products, metadata):
        image = fetch_image(meta.get("image"))
        if image is None:
            continue
        images.append(image)
        kept_products.append(product)
        kept_metadata.append(meta)
    if not images:
        raise RuntimeError("No product images could be downloaded for embeddings.")

    if build_clip:
        clip_vectors = embed_clip(images)
        clip_index = faiss.IndexFlatIP(clip_vectors.shape[1])
        clip_index.add(clip_vectors)
        faiss.write_index(clip_index, str(CLIP_INDEX))
    with PRODUCTS_PKL.open("wb") as handle:
        pickle.dump(kept_products, handle)
    with METADATA_PKL.open("wb") as handle:
        pickle.dump(kept_metadata, handle)
    return {"embedded": len(kept_metadata), "clip": build_clip, "dinov2": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build CLIP product embeddings.")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--no-clip", action="store_true")
    args = parser.parse_args()
    print(build_indexes(args.inputs, not args.no_clip, False))


if __name__ == "__main__":
    main()
