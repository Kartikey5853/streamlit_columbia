import argparse
import json
import pickle
import re
from pathlib import Path

import faiss
import numpy as np
import torch
from PIL import Image


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "json"
EMBEDDINGS_DIR = BASE_DIR / "data" / "embeddings"
PRODUCTS_FILE = DATA_DIR / "amazon" / "latest_amazon.json"
INDEX_FILE = EMBEDDINGS_DIR / "embeddings_amazon.index"
METADATA_FILE = EMBEDDINGS_DIR / "metadata_amazon.pkl"
MARKETPLACE_FILE = DATA_DIR / "combined" / "marketplace_products.json"
MODEL_NAME = "hf-hub:Marqo/marqo-fashionSigLIP"


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_products(path: Path) -> dict[str, dict]:
    value = load_json(path, {})
    products = value.get("products", value) if isinstance(value, dict) else {}
    if not isinstance(products, dict):
        return {}
    return {
        str(upc): product
        for upc, product in products.items()
        if isinstance(product, dict)
    }


def load_marketplaces(path: Path) -> dict[str, dict]:
    value = load_json(path, {})
    products = value.get("products", value) if isinstance(value, dict) else {}
    return products if isinstance(products, dict) else {}


def amazon_source(product: dict) -> dict:
    return {
        "title": product.get("title") or product.get("name"),
        "image": product.get("image_url"),
        "price": product.get("price"),
        "link": product.get("url"),
    }


def product_record(upc: str, product: dict, marketplaces: dict) -> dict:
    cached = marketplaces.get(upc)
    if isinstance(cached, dict):
        return cached
    return {
        "upc": upc,
        "title": product.get("title") or product.get("name"),
        "material_composition": product.get("material_composition"),
        "amazon": amazon_source(product),
        "ajio": None,
        "columbia": None,
        "adventuras": None,
    }


def enrich_match(upc: str, product: dict, marketplaces: dict, score: float, source: str) -> dict:
    record = product_record(upc, product, marketplaces)
    return {
        "match_source": source,
        "clip_score": round(float(score), 6),
        "price": record.get("amazon", {}).get("price"),
        "material_composition": record.get("material_composition"),
        **record,
    }


def load_image_model():
    import open_clip

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, _, preprocess = open_clip.create_model_and_transforms(MODEL_NAME)
    return model.to(device).eval(), preprocess, device


def image_embedding(image_path: Path, model, preprocess, device: str) -> np.ndarray:
    image = Image.open(image_path).convert("RGB")
    tensor = preprocess(image).unsqueeze(0).to(device)
    with torch.no_grad():
        vector = model.encode_image(tensor)
        vector = torch.nn.functional.normalize(vector, dim=1)
    return vector.cpu().numpy().astype("float32")


def search_by_image(query_path: Path, top_k: int, minimum_score: float) -> dict:
    index = faiss.read_index(str(INDEX_FILE))
    with open(METADATA_FILE, "rb") as handle:
        metadata = pickle.load(handle)
    marketplaces = load_marketplaces(MARKETPLACE_FILE)

    model, preprocess, device = load_image_model()
    vector = image_embedding(query_path, model, preprocess, device)
    count = min(max(1, top_k), index.ntotal)
    scores, positions = index.search(vector, count)

    matches = []
    for score, position in zip(scores[0], positions[0]):
        if position < 0 or float(score) < minimum_score:
            continue
        item = metadata[position]
        upc = str(item.get("upc") or item.get("key") or "")
        matches.append(enrich_match(upc, item, marketplaces, float(score), "image"))

    return {
        "query": str(query_path.resolve()),
        "query_type": "image",
        "matches": matches,
    }


def search_by_upc(value: str) -> dict:
    upc = re.sub(r"\D", "", value)
    products = load_products(PRODUCTS_FILE)
    marketplaces = load_marketplaces(MARKETPLACE_FILE)

    product = products.get(upc)
    if product is None:
        for candidate_upc, candidate in products.items():
            if str(candidate.get("upc") or "") == upc:
                upc = candidate_upc
                product = candidate
                break

    matches = []
    if product:
        matches.append(enrich_match(upc, product, marketplaces, 1.0, "upc"))

    return {
        "query": value,
        "query_type": "upc",
        "matches": matches,
    }


def search(query: str, top_k: int, minimum_score: float) -> dict:
    query_path = Path(query)
    if query_path.is_file():
        return search_by_image(query_path, top_k, minimum_score)
    if re.fullmatch(r"\D*\d{12,13}\D*", query):
        return search_by_upc(query)
    raise ValueError("Input must be an image path or a 12/13 digit UPC/EAN.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search Columbia products by image or UPC/EAN."
    )
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
