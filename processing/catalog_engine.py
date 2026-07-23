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
    CANONICAL_MAPPING,
    CLIP_INDEX,
    EMBEDDING_CACHE_PKL,
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

DIRECT_EAN_SITES = ("ajio", "columbia", "adventuras")
MATCH_SITES = MARKETPLACES
MODEL_NAME = "hf-hub:Marqo/marqo-fashionSigLIP"

_THREAD_LOCAL = threading.local()


@dataclass(frozen=True)
class IndexedRecord:
    site: str
    key: str | None
    product_id: str | None
    dataset_index: int
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


def _product_id(product: dict) -> str | None:
    for key in ("source_product_id", "product_id", "productId", "id", "asin", "sku", "upc", "ean", "url", "link"):
        value = product.get(key)
        if value:
            return str(value)
    return None


def _record_for_product(product: dict, site: str, dataset_index: int) -> IndexedRecord | None:
    image = product.get("image") or product.get("image_url")
    title = product.get("title") or product.get("name")
    if not image or not title:
        return None
    return IndexedRecord(
        site=site,
        key=_match_key(product),
        product_id=_product_id(product),
        dataset_index=dataset_index,
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
        for idx, product in enumerate(product_list(payload)):
            record = _record_for_product(product, site, idx)
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


def _load_embedding_cache(path: Path = EMBEDDING_CACHE_PKL) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            cache = pickle.load(handle)
        return cache if isinstance(cache, dict) else {}
    except Exception:
        return {}


def _save_embedding_cache(cache: dict[str, dict], path: Path = EMBEDDING_CACHE_PKL) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        pickle.dump(cache, handle)


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

    records = collect_visual_records(input_paths)
    if not records:
        raise RuntimeError("No embeddable products were found in the visual sources.")

    manifest = load_manifest(manifest_path)
    embedding_cache = _load_embedding_cache()

    images_in_records = {str(record.image) for record in records if record.image}
    missing_images = [image for image in images_in_records if image not in embedding_cache]

    if output_index.exists() and output_metadata.exists() and manifest_matches(manifest, input_paths) and not missing_images:
        metadata = load_visual_index(output_index, output_metadata)[1]
        return {
            "embedded": len(metadata),
            "new_embeddings": 0,
            "download_failures": 0,
            "cached": True,
            "index_path": str(output_index),
            "metadata_path": str(output_metadata),
            "cache_path": str(EMBEDDING_CACHE_PKL),
        }

    download_failures = 0

    if missing_images:
        model, preprocess, device, torch_module = _load_clip_model()
        with ThreadPoolExecutor(max_workers=download_workers) as pool:
            for start in range(0, len(missing_images), chunk_size):
                image_urls = missing_images[start:start + chunk_size]
                downloaded = list(pool.map(_download_image, image_urls))
                batch_images: list[Image.Image] = []
                batch_urls: list[str] = []
                for image_url, image in zip(image_urls, downloaded):
                    if image is None:
                        download_failures += 1
                        continue
                    batch_images.append(image)
                    batch_urls.append(image_url)
                    if len(batch_images) >= batch_size:
                        vectors = _embed_images(batch_images, model, preprocess, device, torch_module)
                        for vector, url in zip(vectors, batch_urls):
                            embedding_cache[url] = {
                                "vector": vector,
                                "dimension": int(vector.shape[0]),
                                "updated_at": datetime.now().isoformat(timespec="seconds"),
                            }
                        batch_images.clear()
                        batch_urls.clear()

                if batch_images:
                    vectors = _embed_images(batch_images, model, preprocess, device, torch_module)
                    for vector, url in zip(vectors, batch_urls):
                        embedding_cache[url] = {
                            "vector": vector,
                            "dimension": int(vector.shape[0]),
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        }

        _save_embedding_cache(embedding_cache)

    clip_index = None
    metadata: list[dict] = []
    vector_batch: list[np.ndarray] = []

    for record in records:
        if not record.image:
            continue
        cached = embedding_cache.get(str(record.image))
        if not isinstance(cached, dict):
            continue
        vector = cached.get("vector")
        if vector is None:
            continue
        vector_np = np.asarray(vector, dtype="float32")
        if vector_np.ndim != 1 or vector_np.size == 0:
            continue
        vector_batch.append(vector_np)
        metadata.append({
            "site": record.site,
            "source": record.site,
            "key": record.key,
            "product_id": record.product_id,
            "source_product_id": record.raw.get("source_product_id") or record.product_id,
            "sku": record.raw.get("sku"),
            "asin": record.raw.get("asin"),
            "ean": record.raw.get("ean") or record.raw.get("upc"),
            "dataset_index": record.dataset_index,
            "title": record.title,
            "price": record.price,
            "price_value": record.price_value,
            # AJIO has two distinct price fields.  Keep both on vector
            # candidates so a validated match does not collapse them.
            "normal_price": record.raw.get("normal_price", record.raw.get("price")),
            "normal_price_value": record.raw.get("normal_price_value", record.raw.get("price_value")),
            "offer_price": record.raw.get("offer_price", record.raw.get("special_price")),
            "offer_price_value": record.raw.get("offer_price_value"),
            "availability": record.raw.get("availability", record.raw.get("available")),
            "url": record.url,
            "image": record.image,
        })
        if len(vector_batch) >= chunk_size:
            stacked = np.stack(vector_batch).astype("float32")
            if clip_index is None:
                clip_index = faiss.IndexFlatIP(stacked.shape[1])
            clip_index.add(stacked)
            vector_batch.clear()

    if vector_batch:
        stacked = np.stack(vector_batch).astype("float32")
        if clip_index is None:
            clip_index = faiss.IndexFlatIP(stacked.shape[1])
        clip_index.add(stacked)

    if clip_index is None or not metadata:
        raise RuntimeError("No CLIP vectors were generated for the visual index.")

    _save_index(clip_index, metadata, output_index, output_metadata)
    save_manifest(manifest_path, {
        "sources": [source_signature(path) for path in input_paths],
        "index_path": str(output_index),
        "metadata_path": str(output_metadata),
        "embedded": len(metadata),
        "new_embeddings": len(missing_images),
        "cache_path": str(EMBEDDING_CACHE_PKL),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    return {
        "embedded": len(metadata),
        "new_embeddings": len(missing_images),
        "download_failures": download_failures,
        "cached": False,
        "index_path": str(output_index),
        "metadata_path": str(output_metadata),
        "cache_path": str(EMBEDDING_CACHE_PKL),
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
    for tuple_key, row in products.items():
        if not isinstance(row, dict):
            continue
        canonical_id = str(row.get("canonical_product_id") or tuple_key)
        for site in MATCH_SITES:
            card = row.get(site)
            if not isinstance(card, dict):
                continue
            for key in (card.get("source_product_id"), card.get("product_id"), card.get("asin"), card.get("url"), card.get("title")):
                if key:
                    lookup[(site, str(key))] = (canonical_id, row)
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
    reference_site: str | None,
    index,
    metadata: list[dict],
    config: dict,
    top_k: int,
    embedding_cache: dict[str, dict] | None = None,
    allow_new_embedding: bool = True,
) -> tuple[dict[str, dict], list[dict]]:
    image = reference.get("image") or reference.get("image_url")
    if not image:
        return {}, []

    vector = None
    if embedding_cache is not None:
        cached = embedding_cache.get(str(image))
        if isinstance(cached, dict):
            cached_vector = cached.get("vector")
            if cached_vector is not None:
                candidate = np.asarray(cached_vector, dtype="float32")
                if candidate.ndim == 1 and candidate.size > 0:
                    vector = candidate.reshape(1, -1)

    if vector is None:
        if not allow_new_embedding:
            return {}, []
        image_result = _download_image(str(image))
        if image_result is None:
            return {}, []
        vector = embed_image(image_result)
        if embedding_cache is not None:
            embedding_cache[str(image)] = {
                "vector": np.asarray(vector[0], dtype="float32"),
                "dimension": int(vector.shape[1]),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }

    # FAISS returns one global ranking.  Truncating that ranking to ``top_k``
    # before grouping by marketplace starves sites whose visually similar
    # products rank just below another site's products (most noticeably
    # Amazon/AJIO).  Reserve up to ``top_k`` candidates for every target site
    # so the per-site selection below is actually meaningful.
    target_site_count = len({
        str(item.get("site") or "")
        for item in metadata
        if item.get("site") and (not reference_site or str(item.get("site")) != reference_site)
    })
    count = min(max(1, top_k) * max(1, target_site_count), index.ntotal)
    scores, positions = index.search(vector.astype("float32"), count)
    scored: list[dict] = []
    best_by_site: dict[str, dict] = {}
    for score, position in zip(scores[0], positions[0]):
        if position < 0 or position >= len(metadata):
            continue
        candidate = metadata[int(position)]
        candidate_site = str(candidate.get("site") or "")
        if not candidate_site:
            continue
        if reference_site and candidate_site == reference_site:
            continue
        candidate_with_score = {
            **candidate,
            "source": candidate.get("source") or candidate_site,
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


def _copy_card(card: dict | None) -> dict | None:
    return dict(card) if isinstance(card, dict) else None


def _today_iso() -> str:
    return datetime.now().date().isoformat()


def _merge_status(existing: dict | None = None) -> dict:
    base = {
        site: {"available": False, "last_seen": None}
        for site in MARKETPLACES
    }
    if isinstance(existing, dict):
        for site in MARKETPLACES:
            value = existing.get(site)
            if isinstance(value, dict):
                base[site] = {
                    "available": bool(value.get("available", False)),
                    "last_seen": value.get("last_seen"),
                }
    return base


def _merge_history(existing: dict | None = None) -> dict:
    base: dict[str, list[dict]] = {site: [] for site in MARKETPLACES}
    if isinstance(existing, dict):
        for site in MARKETPLACES:
            entries = existing.get(site)
            if isinstance(entries, list):
                base[site] = [entry for entry in entries if isinstance(entry, dict)]
    return base


def _append_history(history: dict, site: str, date_string: str, price: object, available: bool) -> None:
    entries = history.setdefault(site, [])
    record = {
        "date": date_string,
        "price": price,
        "availability": bool(available),
    }
    if entries and entries[-1] == record:
        return
    entries.append(record)


def _is_available(product: dict | None) -> bool:
    """Treat an explicit false availability value as out of stock.

    Older imports do not always carry availability, so their presence remains
    the best evidence that the item is for sale.
    """
    return not isinstance(product, dict) or product.get("availability", product.get("available")) is not False


def _canonical_id_for_columbia(
    product: dict,
    mapping_records: dict,
    previous_by_columbia: dict[str, dict],
) -> str:
    from .product_store import source_key, source_product_id

    product_id = source_product_id("columbia", product)
    if not product_id:
        raise ValueError("Eligible Columbia product is missing its stable source_product_id.")
    mapped = mapping_records.get(source_key("columbia", product_id) or "")
    if isinstance(mapped, dict) and mapped.get("canonical_product_id"):
        return str(mapped["canonical_product_id"])
    previous = previous_by_columbia.get(product_id)
    if isinstance(previous, dict) and previous.get("canonical_product_id"):
        return str(previous["canonical_product_id"])
    return f"canonical:columbia:{product_id}"


def build_final_tuples(output: Path = FINAL_TUPLES) -> dict:
    """Build exactly one canonical tuple for every Columbia catalog product."""
    from .product_store import (
        backfill_price_canonical_ids,
        canonical_rows,
        source_product_id,
        sync_canonical_mapping,
    )

    config = load_config()
    threshold = float(config["match_threshold"])
    manifest_path = FINAL_TUPLES_MANIFEST
    # The former combined marketplace file was an Amazon-enrichment artifact.
    # Current source files are the authoritative daily catalog inputs.
    source_paths = [current_json_path(site) for site in MARKETPLACES]
    columbia_path = current_json_path("columbia")
    if not columbia_path.exists():
        raise RuntimeError(
            "Columbia catalog data is required to build canonical tuples. "
            "Run the Columbia scraper or import its current artifact first."
        )
    if output.resolve() == FINAL_TUPLES.resolve() and output.exists() and manifest_matches(load_manifest(manifest_path), source_paths):
        cached_payload = load_json(output, {"products": {}, "summary": {}})
        columbia_count = len(product_list(load_json(columbia_path, {})))
        cached_products = cached_payload.get("products", {}) if isinstance(cached_payload, dict) else {}
        cached_rules = cached_payload.get("rules", {}) if isinstance(cached_payload, dict) else {}
        if (
            isinstance(cached_products, dict)
            and len(cached_products) == columbia_count
            and cached_rules.get("candidate_pool_per_site") is True
            and cached_rules.get("catalog_scope") == "all_columbia_products"
        ):
            return cached_payload

    logger = get_scraper_logger("matcher", log_path("matcher"))
    started_total = datetime.now()
    previous_payload = load_json(output, {"products": {}}) if output.exists() else {"products": {}}
    previous_by_canonical = canonical_rows(previous_payload)
    previous_by_columbia: dict[str, dict] = {}
    for _, row in previous_by_canonical.values():
        card = row.get("columbia") if isinstance(row, dict) else None
        if isinstance(card, dict):
            source_id = source_product_id("columbia", card)
            if source_id:
                previous_by_columbia[source_id] = row

    mapping_payload = load_json(CANONICAL_MAPPING, {"records": {}})
    mapping_records = mapping_payload.get("records", {}) if isinstance(mapping_payload, dict) else {}
    if not isinstance(mapping_records, dict):
        mapping_records = {}
    target_sources = source_paths
    embedding_cache = _load_embedding_cache()

    log_event(logger, logging.INFO, "STEP-2A", f"START loading existing visual index from {[path.name for path in target_sources]}")
    try:
        # Rebuilding canonical tuples is a relationship migration, not an
        # embedding job.  Reuse a valid existing index even if today's raw
        # catalog files have changed; `pipeline --step all` remains the
        # explicit path for genuinely new-product embedding updates.
        index, metadata = load_visual_index()
        build_result = {
            "embedded": len(metadata), "new_embeddings": 0, "download_failures": 0,
            "cached": True, "reused_existing_index": True,
        }
    except RuntimeError:
        build_result = build_visual_index(target_sources)
        index, metadata = load_visual_index()
    log_event(logger, logging.INFO, "STEP-2A", f"DONE visual index ready; embedded={build_result.get('embedded', 0)} cached={bool(build_result.get('cached'))}")
    log_event(logger, logging.INFO, "STEP-2B", f"DONE loaded index vectors={index.ntotal} metadata={len(metadata)}")

    source_products = {
        site: product_list(load_json(current_json_path(site), {}))
        for site in MARKETPLACES
    }
    columbia_products = source_products["columbia"]
    products: dict[str, dict] = {}
    canonical_owner: dict[str, str] = {}
    accepted_matches = 0
    match_top_k = max(1, int(config.get("visual_match_top_k", 12)))
    scrape_date = _today_iso()
    log_event(logger, logging.INFO, "STEP-2C", f"START Columbia catalog tuple assembly columbia_products={len(columbia_products)}")

    for index_pos, columbia_product in enumerate(columbia_products, start=1):
        canonical_id = _canonical_id_for_columbia(columbia_product, mapping_records, previous_by_columbia)
        columbia_source_id = source_product_id("columbia", columbia_product)
        # Some legacy Amazon/EAN mappings collapsed different Columbia
        # variants onto one ID.  Preserve an existing ID only for its first
        # Columbia source; every other eligible Columbia product gets its own
        # stable canonical tuple instead of silently disappearing.
        if canonical_owner.get(canonical_id) not in (None, columbia_source_id):
            canonical_id = f"canonical:columbia:{columbia_source_id}"
        canonical_owner[canonical_id] = str(columbia_source_id)
        previous_row = previous_by_canonical.get(canonical_id, (None, None))[1]
        row = empty_tuple("")
        row["canonical_product_id"] = canonical_id
        row["columbia"] = product_card(columbia_product)
        status = _merge_status(previous_row.get("status") if isinstance(previous_row, dict) else None)
        history = _merge_history(previous_row.get("history") if isinstance(previous_row, dict) else None)
        status["columbia"] = {
            "available": _is_available(columbia_product),
            "last_seen": scrape_date,
        }
        match_meta: dict[str, dict] = {}

        reference = row["columbia"]
        if isinstance(reference, dict) and (reference.get("title") or reference.get("image")):
            best_by_site, scored = match_reference_to_targets(
                reference, "columbia", index, metadata, config, match_top_k, embedding_cache,
                allow_new_embedding=False,
            )
            for site in (site for site in MARKETPLACES if site != "columbia"):
                match = best_by_site.get(site)
                if match:
                    row[site] = match["card"]
                    match_meta[site] = match["meta"]
                    status[site] = {"available": _is_available(match["card"]), "last_seen": scrape_date}
                    accepted_matches += 1
                else:
                    # A source absent from today's matching data is not an
                    # out-of-stock product: it is not present on that site.
                    row[site] = None
                    status[site] = {"available": False, "last_seen": None}
        else:
            log_event(logger, logging.WARNING, canonical_id, "Columbia anchor has no image/title; retained as a Columbia-only tuple")

        row["match"] = {site: meta for site, meta in match_meta.items() if meta}
        row["status"] = status
        row["history"] = history
        products[canonical_id] = row
        if index_pos % 100 == 0:
            log_event(logger, logging.INFO, "STEP-2C", f"PROGRESS tuples_built={index_pos}/{len(columbia_products)}")

    payload = {
        "schema_version": 5,
        "primary_key": "canonical_product_id",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "rules": {
            "catalog_scope": "all_columbia_products",
            "eligibility": "Every product in the Columbia catalog, including out-of-stock products",
            "anchor": "columbia",
            "threshold": threshold,
            "top_k": match_top_k,
            "candidate_pool_per_site": True,
            "weights": {
                "clip": float(config["match_clip_weight"]),
                "title": float(config["match_title_weight"]),
                "price": float(config["match_price_weight"]),
            },
            "visual_index": build_result,
        },
        "summary": {
            "tuples": len(products),
            "columbia_products": len(columbia_products),
            "columbia_available_products": sum(_is_available(product) for product in columbia_products),
            "source_product_counts": {site: len(source_products[site]) for site in MARKETPLACES},
            "accepted_cross_market_matches": accepted_matches,
        },
        "products": products,
    }
    sync_canonical_mapping(payload, write=True)
    save_json_atomic(output, payload)
    backfill_price_canonical_ids(payload, write=True)
    _save_embedding_cache(embedding_cache)
    if output.resolve() == FINAL_TUPLES.resolve():
        save_json_atomic(dated_json_path("combined", datetime.now().date().isoformat()), payload)
        save_manifest(manifest_path, {
            "sources": [source_signature(path) for path in source_paths],
            "output": str(output),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": payload["summary"],
        })
    elapsed = (datetime.now() - started_total).total_seconds()
    log_event(logger, logging.INFO, "STEP-2D", f"DONE tuple assembly in {elapsed:.2f}s; tuples={len(products)} accepted_cross_market_matches={accepted_matches}")
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
        canonical_id, row = None, None
        site = str(candidate.get("site") or "")
        for key in (candidate.get("url"), candidate.get("product_id"), candidate.get("title")):
            if not key:
                continue
            found = lookup.get((site, str(key)))
            if found:
                canonical_id, row = found
                break
        if not canonical_id or not row:
            for key, candidate_row in payload.get("products", {}).items():
                if not isinstance(candidate_row, dict):
                    continue
                card = candidate_row.get(candidate.get("site"))
                if isinstance(card, dict) and (
                    card.get("source_product_id") == candidate.get("source_product_id")
                    or card.get("url") == candidate.get("url") or card.get("title") == candidate.get("title")
                ):
                    canonical_id = str(candidate_row.get("canonical_product_id") or key)
                    row = candidate_row
                    break
        if not canonical_id or not row:
            continue
        matches.append({
            "canonical_product_id": canonical_id,
            "EAN": row.get("EAN"),
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


def search_tuple_matches_batch(image_paths: list[Path], top_k: int = 5, minimum_similarity: float = 0.0) -> dict:
    if len(image_paths) > 50:
        raise ValueError("Batch image search supports up to 50 images.")

    index, metadata = load_visual_index()
    payload = load_json(FINAL_TUPLES, {"products": {}})
    lookup = build_tuple_lookup(payload)

    results: list[dict] = []
    for image_path in image_paths:
        image = Image.open(image_path).convert("RGB")
        vector = embed_image(image)
        count = min(max(1, top_k), index.ntotal)
        scores, positions = index.search(vector.astype("float32"), count)

        image_matches: list[dict] = []
        for score, position in zip(scores[0], positions[0]):
            if position < 0 or position >= len(metadata):
                continue
            candidate = metadata[int(position)]
            similarity = float(score)
            if similarity < minimum_similarity:
                continue
            canonical_id, row = None, None
            site = str(candidate.get("site") or "")
            for key in (candidate.get("url"), candidate.get("product_id"), candidate.get("title")):
                if not key:
                    continue
                found = lookup.get((site, str(key)))
                if found:
                    canonical_id, row = found
                    break
            if not canonical_id or not row:
                continue
            image_matches.append({
                "canonical_product_id": canonical_id,
                "EAN": row.get("EAN"),
                "site": site,
                "confidence": round(similarity, 6),
                "tuple": row,
            })

        top_match = image_matches[0] if image_matches else None
        results.append({
            "filename": image_path.name,
            "query_image": str(image_path.resolve()),
            "canonical_product_id": top_match.get("canonical_product_id") if top_match else None,
            "matched_ean": top_match.get("EAN") if top_match else None,
            "confidence": top_match.get("confidence") if top_match else None,
            "tuple": top_match.get("tuple") if top_match else None,
            "matches": image_matches,
        })

    return {
        "count": len(results),
        "results": results,
    }


def search_image_as_tuple(image_path: Path, top_k: int = 50) -> dict | None:
    result = search_tuple_matches(image_path, top_k=top_k)
    matches = result.get("matches", [])
    if not matches:
        return None
    first = matches[0]
    return {
        "canonical_product_id": first.get("canonical_product_id"),
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
