from __future__ import annotations

import argparse
import json
import pickle
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from .config import load_config
from .json_store import load_json, product_list, products_by_ean, save_json_atomic
from .platform_paths import AMAZON_PRODUCTS, CLIP_INDEX, FINAL_TUPLES, MARKETPLACE_PRODUCTS, METADATA_PKL, MYNTRA_PRODUCTS, TATACLIQ_PRODUCTS, current_json_path, dated_json_path, log_path
from .product_schema import MARKETPLACES, empty_tuple, price_value, product_card
from .structured_logging import get_scraper_logger, log_event
import logging


def words(value: str) -> set[str]:
    stop = {"columbia", "men", "mens", "women", "womens", "unisex", "for", "the"}
    return {part for part in "".join(ch.lower() if ch.isalnum() else " " for ch in value).split() if part not in stop}


def title_similarity(left: str | None, right: str | None) -> float:
    left = left or ""
    right = right or ""
    left_words = words(left)
    right_words = words(right)
    overlap = len(left_words & right_words) / max(1, len(left_words | right_words))
    sequence = SequenceMatcher(None, left.lower(), right.lower()).ratio()
    return round((0.65 * overlap) + (0.35 * sequence), 6)


def strict_price_score(left_price, right_price, config: dict | None = None) -> tuple[float, str, float | None]:
    config = config or load_config()
    left = price_value(left_price)
    right = price_value(right_price)
    if left is None or right is None:
        return 0.0, "missing_price", None
    diff = abs(left - right)
    no_penalty_diff = float(config["price_no_penalty_diff"])
    moderate_diff = float(config["price_moderate_penalty_diff"])
    heavy_diff = float(config["price_heavy_penalty_diff"])
    if diff <= no_penalty_diff:
        return 1.0, "no_penalty", diff
    if diff <= moderate_diff:
        return float(config["price_moderate_score"]), "moderate_penalty", diff
    if diff <= heavy_diff:
        return float(config["price_heavy_score"]), "heavy_penalty", diff
    return float(config["price_near_rejection_score"]), "near_rejection", diff


def visual_placeholder_score(left: dict, right: dict) -> tuple[float, float]:
    """Fallback until FAISS vector candidates are available for these products."""
    title = title_similarity(left.get("title") or left.get("name"), right.get("title") or right.get("name"))
    return title, title


def vector_key(product: dict) -> str | None:
    url = product.get("url") or product.get("link")
    if url:
        return f"url:{url}"
    title = product.get("title") or product.get("name")
    image = product.get("image") or product.get("image_url")
    if title and image:
        return f"title_image:{title}|{image}"
    return None


class VisualScoreLookup:
    def __init__(self):
        self.available = False
        self.by_key: dict[str, int] = {}
        self.clip_index = None
        if not (CLIP_INDEX.exists() and METADATA_PKL.exists()):
            return
        try:
            import faiss

            self.clip_index = faiss.read_index(str(CLIP_INDEX))
            with METADATA_PKL.open("rb") as handle:
                metadata = pickle.load(handle)
            for index, item in enumerate(metadata):
                key = vector_key({"url": item.get("url"), "title": item.get("title"), "image": item.get("image")})
                if key:
                    self.by_key[key] = index
            self.available = True
        except Exception:
            self.available = False

    def score(self, left: dict, right: dict) -> tuple[float, float]:
        if not self.available:
            return visual_placeholder_score(left, right)
        left_pos = self.by_key.get(vector_key(left))
        right_pos = self.by_key.get(vector_key(right))
        if left_pos is None or right_pos is None:
            return visual_placeholder_score(left, right)
        clip_score = float(
            self.clip_index.reconstruct(left_pos).dot(self.clip_index.reconstruct(right_pos))
        )
        return max(0.0, clip_score), max(0.0, clip_score)


def confidence(clip_score: float, dino_score: float, title_score: float, price_score: float, config: dict) -> float:
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


def normalized_candidates(payload) -> list[dict]:
    return [product_card(item) | {"raw": item} for item in product_list(payload) if product_card(item)]


def product_number(product: dict) -> str:
    raw = product.get("raw") if isinstance(product.get("raw"), dict) else product
    for key in ("product_id", "productId", "id", "sku", "style_id", "styleId", "product_code", "productCode", "code"):
        value = raw.get(key) if isinstance(raw, dict) else None
        if value:
            return str(value)
    url = product.get("url") or (raw.get("url") if isinstance(raw, dict) else None)
    if url:
        return str(url).rstrip("/").rsplit("/", 2)[-2 if str(url).rstrip("/").endswith("/buy") else -1]
    return "-"


def best_match(reference: dict, candidates: list[dict], threshold: float, visual: VisualScoreLookup, config: dict) -> tuple[dict | None, dict | None, list[dict]]:
    best_card = None
    best_meta = None
    scored: list[dict] = []
    for candidate in candidates:
        clip_score, dino_score = visual.score(reference, candidate)
        title_score = title_similarity(reference.get("title") or reference.get("name"), candidate.get("title"))
        price_score, price_status, price_diff = strict_price_score(reference.get("price"), candidate.get("price"), config)
        score = confidence(clip_score, dino_score, title_score, price_score, config)
        accepted = score >= threshold and not (bool(config["reject_near_price_mismatch"]) and price_status == "near_rejection")
        meta = {
            "clip_score": clip_score,
            "visual_score": dino_score,
            "title_score": title_score,
            "price_score": price_score,
            "price_status": price_status,
            "price_difference": price_diff,
            "confidence": score,
            "accepted": accepted,
        }
        scored.append({
            **meta,
            "title": candidate.get("title"),
            "price": candidate.get("price"),
            "url": candidate.get("url"),
            "product_number": product_number(candidate),
        })
        if not accepted:
            continue
        if best_meta is None or score > best_meta["confidence"]:
            best_card = candidate
            best_meta = meta
    if best_card:
        best_card = {key: best_card.get(key) for key in ("title", "image", "url", "price")}
    return best_card, best_meta, sorted(scored, key=lambda item: item["confidence"], reverse=True)


def card_for_existing(row: dict, site: str) -> dict | None:
    return product_card(row.get(site)) if isinstance(row, dict) else None


def first_reference(row: dict) -> tuple[str | None, dict | None]:
    for site in MARKETPLACES:
        card = row.get(site)
        if isinstance(card, dict) and (card.get("title") or card.get("name")):
            return site, card
    return None, None


def build_tuples(output: Path = FINAL_TUPLES) -> dict:
    logger = get_scraper_logger("matcher", log_path("matcher"))
    config = load_config()
    threshold = float(config["match_threshold"])
    amazon_products = products_by_ean(load_json(AMAZON_PRODUCTS, {}))
    marketplace = load_json(MARKETPLACE_PRODUCTS, {"products": {}})
    existing_by_ean = marketplace.get("products", {}) if isinstance(marketplace, dict) else {}
    myntra_source = current_json_path("myntra")
    tatacliq_source = current_json_path("tatacliq")
    myntra_candidates = normalized_candidates(load_json(myntra_source, []))
    tatacliq_candidates = normalized_candidates(load_json(tatacliq_source, []))
    visual = VisualScoreLookup()
    log_event(logger, logging.INFO, "STEP-1", f"loaded Amazon products: {len(amazon_products)}")
    log_event(logger, logging.INFO, "STEP-1", f"loaded existing marketplace tuples: {len(existing_by_ean) if isinstance(existing_by_ean, dict) else 0}")
    log_event(logger, logging.INFO, "STEP-2", f"loaded Myntra candidates from {myntra_source.name}: {len(myntra_candidates)}")
    log_event(logger, logging.INFO, "STEP-3", f"loaded Tata CLiQ candidates from {tatacliq_source.name}: {len(tatacliq_candidates)}")
    log_event(logger, logging.INFO, "STEP-4", f"visual indexes: {'CLIP only' if visual.available else 'title fallback'}")

    products: dict[str, dict] = {}
    matched = 0
    all_eans = sorted(set(amazon_products) | (set(existing_by_ean) if isinstance(existing_by_ean, dict) else set()))
    for ean in all_eans:
        amazon_product = amazon_products.get(ean)
        row = empty_tuple(ean)
        row["amazon"] = product_card(amazon_product)
        existing = existing_by_ean.get(ean, {}) if isinstance(existing_by_ean, dict) else {}
        for site in ["ajio", "columbia", "adventuras"]:
            row[site] = card_for_existing(existing, site)
        for site in ["myntra", "tatacliq"]:
            row[site] = card_for_existing(existing, site)

        match_meta = {}
        reference_site, reference = first_reference(row)
        if reference:
            log_event(logger, logging.INFO, ean, f"matching reference={reference_site}; candidates myntra={len(myntra_candidates)} tatacliq={len(tatacliq_candidates)}")
            for site, candidates in [("myntra", myntra_candidates), ("tatacliq", tatacliq_candidates)]:
                if row.get(site):
                    log_event(logger, logging.INFO, ean, f"{site} already present in marketplace data; keeping existing card")
                    continue
                matched_card, meta, scored = best_match(reference, candidates, threshold, visual, config)
                row[site], match_meta[site] = matched_card, meta
                for rank, item in enumerate(scored[:5], start=1):
                    log_event(
                        logger,
                        logging.INFO,
                        ean,
                        (
                            f"{site} candidate #{rank}: confidence={item['confidence']} "
                            f"clip={item['clip_score']:.4f} visual={item['visual_score']:.4f} "
                            f"title={item['title_score']:.4f} price={item['price_status']} "
                            f"diff={item['price_difference']} accepted={item['accepted']} "
                            f"{site}_product_number={item.get('product_number')}"
                        ),
                    )
                if meta:
                    log_event(logger, logging.INFO, ean, f"{site} accepted with confidence={meta['confidence']}")
                else:
                    log_event(logger, logging.WARNING, ean, f"{site} no candidate met threshold={threshold}")
        else:
            log_event(logger, logging.WARNING, ean, "no reference card available; tuple created from EAN only")
        row["match"] = {site: meta for site, meta in match_meta.items() if meta}
        matched += sum(1 for site in ["myntra", "tatacliq"] if row.get(site))
        products[ean] = row
        log_event(logger, logging.INFO, ean, f"tuple built; matched sites: {sum(1 for site in MARKETPLACES if row.get(site))}")

    payload = {
        "schema_version": 1,
        "primary_key": "EAN",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "rules": {
            "threshold": threshold,
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
        },
        "summary": {"tuples": len(products), "accepted_cross_market_matches": matched},
        "products": products,
    }
    save_json_atomic(output, payload)
    if output.resolve() == FINAL_TUPLES.resolve():
        save_json_atomic(dated_json_path("combined", datetime.now().date().isoformat()), payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build final EAN tuples with strict matching.")
    parser.add_argument("--output", default=str(FINAL_TUPLES))
    args = parser.parse_args()
    payload = build_tuples(Path(args.output))
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
