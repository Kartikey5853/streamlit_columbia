import argparse
import json
import pickle
from pathlib import Path

import faiss
import numpy as np
import torch
from PIL import Image


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "json"
EMBEDDINGS_DIR = BASE_DIR / "data" / "embeddings"
INDEX_FILE = EMBEDDINGS_DIR / "embeddings_amazon.index"
METADATA_FILE = EMBEDDINGS_DIR / "metadata_amazon.pkl"
MARKETPLACE_FILE = DATA_DIR / "combined" / "marketplace_products.json"
MODEL_NAME = "hf-hub:Marqo/marqo-fashionSigLIP"


def load_marketplaces(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    return value.get("products", value)


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


def query(image_path: Path, top_k: int, minimum_similarity: float) -> dict:
    if not image_path.is_file():
        raise FileNotFoundError(f"Query image not found: {image_path}")
    index = faiss.read_index(str(INDEX_FILE))
    with open(METADATA_FILE, "rb") as handle:
        metadata = pickle.load(handle)
    marketplaces = load_marketplaces(MARKETPLACE_FILE)
    model, preprocess, device = load_image_model()
    vector = image_embedding(image_path, model, preprocess, device)

    if index.ntotal == 0:
        return {"query_image": str(image_path.resolve()), "matches": []}
    count = min(max(1, top_k), index.ntotal)
    scores, positions = index.search(vector, count)
    matches = []
    for score, position in zip(scores[0], positions[0]):
        if position < 0 or float(score) < minimum_similarity:
            continue
        item = metadata[position]
        upc = str(item.get("upc") or item.get("key") or "")
        tuple_record = marketplaces.get(upc)
        if not tuple_record:
            tuple_record = {
                "upc": upc,
                "title": item.get("title") or item.get("name"),
                "amazon": {
                    "image": item.get("image_url"),
                    "price": item.get("price"),
                    "link": item.get("url"),
                },
                "ajio": None,
                "columbia": None,
                "adventuras": None,
            }
        matches.append({
            "similarity": round(float(score), 6),
            **tuple_record,
        })
    return {"query_image": str(image_path.resolve()), "matches": matches}


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
