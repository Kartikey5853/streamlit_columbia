from __future__ import annotations

import logging
import pickle
import threading
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from typing import Iterable

import faiss
import numpy as np
import requests
import torch
from PIL import Image

from .config import load_config
from .json_store import load_json, product_list, products_by_ean, save_json_atomic
from .platform_paths import (
    AMAZON_PRODUCTS,
    CLIP_INDEX,
    FINAL_TUPLES,
    FINAL_TUPLES_MANIFEST,
    MARKETPLACE_PRODUCTS,
    METADATA_PKL,
    VISUAL_INDEX_MANIFEST,
    current_json_path,
    dated_json_path,
    log_path,
)
from .product_schema import MARKETPLACES, empty_tuple, price_value, product_card
from .structured_logging import get_scraper_logger, log_event


logger = logging.getLogger("catalog_engine")

TARGET_VISUAL_SITES = ("myntra", "tatacliq")
DIRECT_EAN_SITES = ("ajio", "columbia", "adventuras")
MATCH_SITES = (*DIRECT_EAN_SITES, *TARGET_VISUAL_SITES)
MODEL_NAME = "hf-hub:Marqo/marqo-fashionSigLIP"

_THREAD_LOCAL = threading.local()


@dataclass(frozen=True)
class IndexedRecord:
    site: str
    key: str | None
    title: str | None
    price: object
    url: str | None
    image: str | None
    price_value: float | None
    raw: dict


def _chunked(values: list[IndexedRecord], size: int) -> Iterable[list[IndexedRecord]]:
    step = max(1, size)
    for start in range(0, len(values), step):
        yield values[start:start + step]


def _load_thread_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        _THREAD_LOCAL.session = session
    return session


def source_signature(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path),
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def load_manifest(path: Path) -> dict:
    return load_json(path, {}) if path.exists() else {}


def manifest_matches(manifest: dict, paths: Iterable[Path]) -> bool:
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        return False
    current = []
    for path in paths:
        if not path.exists():
            return False
        current.append(source_signature(path))
    return sources == current


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json_atomic(path, manifest)


def normalize_words(value: str) -> set[str]:
    stop_words = {"columbia", "men", "mens", "women", "womens", "unisex", "for", "the"}
    cleaned = "".join(character.lower() if character.isalnum() else " " for character in value)
    return {part for part in cleaned.split() if part and part not in stop_words}


def title_similarity(left: str | None, right: str | None) -> float:
    left = left or ""
    right = right or ""
    left_words = normalize_words(left)
    right_words = normalize_words(right)
    overlap = len(left_words & right_words) / max(1, len(left_words | right_words))
    sequence = SequenceMatcher(None, left.casefold(), right.casefold()).ratio()
    return round((0.65 * overlap) + (0.35 * sequence), 6)


def strict_price_score(left_price, right_price, config: dict | None = None) -> tuple[float, str, float | None]:
    config = config or load_config()
    left = price_value(left_price)
    right = price_value(right_price)
    if left is None or right is None:
        return 0.0, "missing_price", None
    difference = abs(left - right)
    no_penalty_diff = float(config["price_no_penalty_diff"])
    moderate_diff = float(config["price_moderate_penalty_diff"])
    heavy_diff = float(config["price_heavy_penalty_diff"])
    if difference <= no_penalty_diff:
        return 1.0, "no_penalty", difference
    if difference <= moderate_diff:
        return float(config["price_moderate_score"]), "moderate_penalty", difference
    if difference <= heavy_diff:
        return float(config["price_heavy_score"]), "heavy_penalty", difference
    return float(config["price_near_rejection_score"]), "near_rejection", difference


def confidence(clip_score: float, title_score: float, price_score: float, config: dict) -> float:
    weights = {
        "clip": float(config["match_clip_weight"]),
        "title": float(config["match_title_weight"]),
        "price": float(config["match_price_weight"]),
    }
    total = sum(weights.values()) or 1.0
    score = (
        (weights["clip"] * clip_score)
        + (weights["title"] * title_score)
        + (weights["price"] * price_score)
    ) / total
    return round(score, 6)


def _match_key(product: dict) -> str | None:
    for key in ("url", "link", "product_id", "productId", "id", "sku"):
        value = product.get(key)
        if value:
            return str(value)
    title = product.get("title") or product.get("name")
    image = product.get("image") or product.get("image_url")
    if title and image:
        return f"{title}|{image}"
    return None


def _record_for_product(product: dict, site: str) -> IndexedRecord | None:
    image = product.get("image") or product.get("image_url")
    title = product.get("title") or product.get("name")
    if not image or not title:
        return None
    return IndexedRecord(
        site=site,
        key=_match_key(product),
        title=str(title),
        price=product.get("price"),
        url=str(product.get("url") or product.get("link") or "") or None,
        image=str(image),
        price_value=price_value(product.get("price_value") or product.get("price")),
        raw=product,
    )


def collect_visual_records(paths: Iterable[Path]) -> list[IndexedRecord]:
    records: list[IndexedRecord] = []
    for path in paths:
        payload = load_json(path, {})
        site = path.parent.name
        for product in product_list(payload):
            record = _record_for_product(product, site)
            if record is not None:
                records.append(record)
    return records


def _download_image(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        with _load_thread_session().get(url, timeout=15) as response:
            response.raise_for_status()
            return Image.open(BytesIO(response.content)).convert("RGB")
    except Exception:
        return None


@lru_cache(maxsize=1)
def _load_clip_model():
    import open_clip

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, _, preprocess = open_clip.create_model_and_transforms(MODEL_NAME)
    model = model.to(device).eval()
    if device == "cuda":
        model = model.half()
    return model, preprocess, device, torch


def _embed_images(images: list[Image.Image], model, preprocess, device: str, torch_module) -> np.ndarray:
    tensors = torch_module.stack([preprocess(image) for image in images]).to(device)
    with torch_module.inference_mode():
        if device == "cuda":
            with torch_module.autocast(device_type="cuda", dtype=torch.float16):
                features = model.encode_image(tensors)
        else:
            features = model.encode_image(tensors)
        features = torch_module.nn.functional.normalize(features, dim=1)
    vectors = features.detach().cpu().numpy().astype("float32")
    del tensors, features
    return vectors


def embed_image(image: Image.Image) -> np.ndarray:
    model, preprocess, device, torch_module = _load_clip_model()
    return _embed_images([image], model, preprocess, device, torch_module)


def load_visual_index(index_path: Path = CLIP_INDEX, metadata_path: Path = METADATA_PKL) -> tuple[object, list[dict]]:
    if not index_path.exists() or not metadata_path.exists():
        raise RuntimeError("Build the visual index before running image search or matching.")
    index = faiss.read_index(str(index_path))
    with metadata_path.open("rb") as handle:
        metadata = pickle.load(handle)
    return index, list(metadata)


def _save_index(index, metadata: list[dict], index_path: Path, metadata_path: Path) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(index_path))
    with metadata_path.open("wb") as handle:
        pickle.dump(metadata, handle)


def build_visual_index(
    inputs: Iterable[Path],
    *,
    output_index: Path = CLIP_INDEX,
    output_metadata: Path = METADATA_PKL,
    manifest_path: Path = VISUAL_INDEX_MANIFEST,
    batch_size: int | None = None,
    download_workers: int | None = None,
    chunk_size: int | None = None,
) -> dict:
    input_paths = [path for path in inputs if path.exists()]
    if not input_paths:
        raise RuntimeError("No visual source files were found.")

    config = load_config()
    batch_size = max(1, int(batch_size or config.get("embedding_batch_size", 8)))
    download_workers = max(1, int(download_workers or config.get("image_download_workers", 8)))
    chunk_size = max(batch_size, int(chunk_size or config.get("visual_index_chunk_size", batch_size * 8)))

    manifest = load_manifest(manifest_path)
    if output_index.exists() and output_metadata.exists() and manifest_matches(manifest, input_paths):
        metadata = load_visual_index(output_index, output_metadata)[1]
        return {
            "embedded": len(metadata),
            "download_failures": 0,
            "cached": True,
            "index_path": str(output_index),
            "metadata_path": str(output_metadata),
        }

    records = collect_visual_records(input_paths)
    if not records:
        raise RuntimeError("No embeddable products were found in the visual sources.")

    model, preprocess, device, torch_module = _load_clip_model()
    clip_index = None
    metadata: list[dict] = []
    download_failures = 0

    with ThreadPoolExecutor(max_workers=download_workers) as pool:
        for chunk in _chunked(records, chunk_size):
            images = list(pool.map(lambda record: _download_image(record.image or ""), chunk))

            batch_images: list[Image.Image] = []
            batch_metadata: list[dict] = []
            for record, image in zip(chunk, images):
                if image is None:
                    download_failures += 1
                    continue
                batch_images.append(image)
                batch_metadata.append({
                    "site": record.site,
                    "key": record.key,
                    "title": record.title,
                    "price": record.price,
                    "price_value": record.price_value,
                    "url": record.url,
                    "image": record.image,
                })
                if len(batch_images) >= batch_size:
                    vectors = _embed_images(batch_images, model, preprocess, device, torch_module)
                    if clip_index is None:
                        clip_index = faiss.IndexFlatIP(vectors.shape[1])
                    clip_index.add(vectors)
                    metadata.extend(batch_metadata)
                    batch_images.clear()
                    batch_metadata.clear()

            if batch_images:
                vectors = _embed_images(batch_images, model, preprocess, device, torch_module)
                if clip_index is None:
                    clip_index = faiss.IndexFlatIP(vectors.shape[1])
                clip_index.add(vectors)
                metadata.extend(batch_metadata)

    if clip_index is None or not metadata:
        raise RuntimeError("No CLIP vectors were generated for the visual index.")

    _save_index(clip_index, metadata, output_index, output_metadata)
    save_manifest(manifest_path, {
        "sources": [source_signature(path) for path in input_paths],
        "index_path": str(output_index),
        "metadata_path": str(output_metadata),
        "embedded": len(metadata),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    return {
        "embedded": len(metadata),
        "download_failures": download_failures,
        "cached": False,
        "index_path": str(output_index),
        "metadata_path": str(output_metadata),
    }


def load_marketplace_store(path: Path = MARKETPLACE_PRODUCTS) -> dict[str, dict]:
    payload = load_json(path, {"schema_version": 1, "primary_key": "EAN", "products": {}})
    if not isinstance(payload, dict):
        return {}
    products = payload.get("products", {})
    return products if isinstance(products, dict) else {}


def build_tuple_lookup(payload: dict) -> dict[tuple[str, str], tuple[str, dict]]:
    lookup: dict[tuple[str, str], tuple[str, dict]] = {}
    products = payload.get("products", {}) if isinstance(payload, dict) else {}
    if not isinstance(products, dict):
        return lookup
    for ean, row in products.items():
        if not isinstance(row, dict):
            continue
        for site in MATCH_SITES:
            card = row.get(site)
            if not isinstance(card, dict):
                continue
            for key in (card.get("url"), card.get("product_id"), card.get("title")):
                if key:
                    lookup[(site, str(key))] = (str(ean), row)
    return lookup


def resolve_tuple_row(payload: dict, candidate: dict) -> tuple[str | None, dict | None]:
    lookup = build_tuple_lookup(payload)
    site = str(candidate.get("site") or "")
    for key in (candidate.get("url"), candidate.get("product_id"), candidate.get("title")):
        if not key:
            continue
        found = lookup.get((site, str(key)))
        if found:
            return found
    return None, None


def _score_candidate(reference: dict, candidate: dict, config: dict) -> dict:
    clip_score = max(0.0, float(candidate["clip_score"]))
    title_score = title_similarity(reference.get("title") or reference.get("name"), candidate.get("title"))
    price_score, price_status, price_difference = strict_price_score(reference.get("price"), candidate.get("price"), config)
    score = confidence(clip_score, title_score, price_score, config)
    accepted = score >= float(config["match_threshold"]) and not (
        bool(config.get("reject_near_price_mismatch", True)) and price_status == "near_rejection"
    )
    return {
        "clip_score": clip_score,
        "visual_score": clip_score,
        "title_score": title_score,
        "price_score": price_score,
        "price_status": price_status,
        "price_difference": price_difference,
        "confidence": score,
        "accepted": accepted,
        "site": candidate["site"],
        "title": candidate.get("title"),
        "price": candidate.get("price"),
        "url": candidate.get("url"),
        "product_number": candidate.get("key") or candidate.get("url") or "-",
    }


def match_reference_to_targets(
    reference: dict,
    index,
    metadata: list[dict],
    config: dict,
    top_k: int,
) -> tuple[dict[str, dict], list[dict]]:
    image = reference.get("image") or reference.get("image_url")
    if not image:
        return {}, []
    image_result = _download_image(image)
    if image_result is None:
        return {}, []
    vector = embed_image(image_result)
    count = min(max(1, top_k), index.ntotal)
    scores, positions = index.search(vector.astype("float32"), count)
    scored: list[dict] = []
    best_by_site: dict[str, dict] = {}
    for score, position in zip(scores[0], positions[0]):
        if position < 0 or position >= len(metadata):
            continue
        candidate = metadata[int(position)]
        if candidate.get("site") not in TARGET_VISUAL_SITES:
            continue
        candidate_with_score = {
            **candidate,
            "clip_score": float(score),
        }
        result = _score_candidate(reference, candidate_with_score, config)
        scored.append(result)
        if not result["accepted"]:
            continue
        site = result["site"]
        existing = best_by_site.get(site)
        if existing is None or result["confidence"] > existing["meta"]["confidence"]:
            best_by_site[site] = {
                "card": product_card(candidate),
                "meta": result,
            }
    scored.sort(key=lambda item: item["confidence"], reverse=True)
    return best_by_site, scored


def _normalize_marketplace_row(row: dict | None, ean: str, include_target_sites: bool = False) -> dict:
    normalized = empty_tuple(ean)
    if isinstance(row, dict):
        normalized["amazon"] = product_card(row.get("amazon"))
        sites = MATCH_SITES if include_target_sites else DIRECT_EAN_SITES
        for site in sites:
            card = row.get(site)
            if isinstance(card, dict):
                normalized[site] = product_card(card)
    return normalized


def build_final_tuples(output: Path = FINAL_TUPLES) -> dict:
    config = load_config()
    threshold = float(config["match_threshold"])
    manifest_path = FINAL_TUPLES_MANIFEST
    source_paths = [
        AMAZON_PRODUCTS,
        MARKETPLACE_PRODUCTS,
        current_json_path("myntra"),
        current_json_path("tatacliq"),
    ]
    if output.resolve() == FINAL_TUPLES.resolve() and output.exists() and manifest_matches(load_manifest(manifest_path), source_paths):
        return load_json(output, {"products": {}, "summary": {}})

    logger = get_scraper_logger("matcher", log_path("matcher"))
    amazon_products = products_by_ean(load_json(AMAZON_PRODUCTS, {}))
    marketplace_store = load_marketplace_store(MARKETPLACE_PRODUCTS)
    target_sources = [current_json_path("myntra"), current_json_path("tatacliq")]
    build_result = build_visual_index(target_sources)
    index, metadata = load_visual_index()
    direct_sources = {
        site: products_by_ean(load_json(current_json_path(site), {}))
        for site in DIRECT_EAN_SITES
    }
    existing_by_ean = marketplace_store if isinstance(marketplace_store, dict) else {}

    log_event(logger, logging.INFO, "STEP-1", f"loaded Amazon products: {len(amazon_products)}")
    log_event(logger, logging.INFO, "STEP-2", f"loaded direct marketplace rows: {len(existing_by_ean)}")
    log_event(logger, logging.INFO, "STEP-3", f"visual index ready: {'cached' if build_result.get('cached') else 'rebuilt'} with {build_result['embedded']} vectors")

    all_eans = sorted(set(amazon_products) | set(existing_by_ean) | set().union(*[set(source) for source in direct_sources.values()]))
    products: dict[str, dict] = {}
    accepted_matches = 0
    match_top_k = max(1, int(config.get("visual_match_top_k", 12)))

    for ean in all_eans:
        row = _normalize_marketplace_row(existing_by_ean.get(ean), ean, include_target_sites=False)
        row["amazon"] = product_card(amazon_products.get(ean))
        for site, source in direct_sources.items():
            row[site] = product_card(source.get(ean))

        match_meta: dict[str, dict] = {}
        reference_site = None
        reference = None
        for site in MARKETPLACES:
            card = row.get(site)
            if isinstance(card, dict) and (card.get("title") or card.get("image")):
                reference_site = site
                reference = card
                break

        if reference is not None:
            log_event(logger, logging.INFO, ean, f"matching reference={reference_site}; targets myntra/tatacliq")
            best_by_site, scored = match_reference_to_targets(reference, index, metadata, config, match_top_k)
            for site in TARGET_VISUAL_SITES:
                if row.get(site):
                    log_event(logger, logging.INFO, ean, f"{site} already present; keeping direct row")
                    continue
                match = best_by_site.get(site)
                if match:
                    row[site] = match["card"]
                    match_meta[site] = match["meta"]
                    accepted_matches += 1
                    log_event(logger, logging.INFO, ean, f"{site} accepted with confidence={match['meta']['confidence']}")
                else:
                    log_event(logger, logging.WARNING, ean, f"{site} no candidate met threshold={threshold}")
            for rank, item in enumerate(scored[:5], start=1):
                log_event(
                    logger,
                    logging.INFO,
                    ean,
                    (
                        f"candidate #{rank}: site={item['site']} confidence={item['confidence']} "
                        f"clip={item['clip_score']:.4f} title={item['title_score']:.4f} "
                        f"price={item['price_status']} diff={item['price_difference']} accepted={item['accepted']}"
                    ),
                )
        else:
            log_event(logger, logging.WARNING, ean, "no reference card available; tuple created from EAN only")

        row["match"] = {site: meta for site, meta in match_meta.items() if meta}
        products[ean] = row
        log_event(logger, logging.INFO, ean, f"tuple built; matched sites: {sum(1 for site in MATCH_SITES if row.get(site))}")

    payload = {
        "schema_version": 1,
        "primary_key": "EAN",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "rules": {
            "threshold": threshold,
            "top_k": match_top_k,
            "weights": {
                "clip": float(config["match_clip_weight"]),
                "title": float(config["match_title_weight"]),
                "price": float(config["match_price_weight"]),
            },
            "price_penalty": {
                f"<={config['price_no_penalty_diff']}": "none",
                f"{config['price_no_penalty_diff']}-{config['price_moderate_penalty_diff']}": config["price_moderate_score"],
                f"{config['price_moderate_penalty_diff']}-{config['price_heavy_penalty_diff']}": config["price_heavy_score"],
                f">{config['price_heavy_penalty_diff']}": config["price_near_rejection_score"],
            },
            "visual_index": build_result,
        },
        "summary": {
            "tuples": len(products),
            "accepted_cross_market_matches": accepted_matches,
        },
        "products": products,
    }
    save_json_atomic(output, payload)
    if output.resolve() == FINAL_TUPLES.resolve():
        save_json_atomic(dated_json_path("combined", datetime.now().date().isoformat()), payload)
        save_manifest(manifest_path, {
            "sources": [source_signature(path) for path in source_paths],
            "output": str(output),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": payload["summary"],
        })
    return payload


def search_tuple_matches(image_path: Path, top_k: int = 5, minimum_similarity: float = 0.0) -> dict:
    index, metadata = load_visual_index()
    payload = load_json(FINAL_TUPLES, {"products": {}})
    lookup = build_tuple_lookup(payload)
    image = Image.open(image_path).convert("RGB")
    vector = embed_image(image)
    count = min(max(1, top_k), index.ntotal)
    scores, positions = index.search(vector.astype("float32"), count)

    matches = []
    for score, position in zip(scores[0], positions[0]):
        if position < 0 or position >= len(metadata):
            continue
        candidate = metadata[int(position)]
        similarity = float(score)
        if similarity < minimum_similarity:
            continue
        ean, row = None, None
        site = str(candidate.get("site") or "")
        for key in (candidate.get("url"), candidate.get("product_id"), candidate.get("title")):
            if not key:
                continue
            found = lookup.get((site, str(key)))
            if found:
                ean, row = found
                break
        if not ean or not row:
            for key, candidate_row in payload.get("products", {}).items():
                if not isinstance(candidate_row, dict):
                    continue
                card = candidate_row.get(candidate.get("site"))
                if isinstance(card, dict) and (
                    card.get("url") == candidate.get("url") or card.get("title") == candidate.get("title")
                ):
                    ean = str(key)
                    row = candidate_row
                    break
        if not ean or not row:
            continue
        matches.append({
            "EAN": ean,
            "site": candidate.get("site"),
            "similarity": round(similarity, 6),
            "candidate": {
                "title": candidate.get("title"),
                "price": candidate.get("price"),
                "url": candidate.get("url"),
                "image": candidate.get("image"),
            },
            "tuple": row,
        })
    return {
        "query_image": str(image_path.resolve()),
        "matches": matches,
    }


def search_image_as_tuple(image_path: Path, top_k: int = 50) -> dict | None:
    result = search_tuple_matches(image_path, top_k=top_k)
    matches = result.get("matches", [])
    if not matches:
        return None
    first = matches[0]
    return {
        "EAN": first.get("EAN"),
        "tuple": first.get("tuple"),
    }


def build_ean_rows() -> dict[str, dict]:
    amazon_products = products_by_ean(load_json(AMAZON_PRODUCTS, {}))
    marketplace_store = load_marketplace_store(MARKETPLACE_PRODUCTS)
    direct_sources = {
        site: products_by_ean(load_json(current_json_path(site), {}))
        for site in DIRECT_EAN_SITES
    }
    all_eans = sorted(set(amazon_products) | set(marketplace_store) | set().union(*[set(source) for source in direct_sources.values()]))
    rows: dict[str, dict] = {}
    for ean in all_eans:
        row = _normalize_marketplace_row(marketplace_store.get(ean), ean)
        row["amazon"] = product_card(amazon_products.get(ean))
        for site, source in direct_sources.items():
            row[site] = product_card(source.get(ean))
        rows[ean] = row
    return rows
