from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
from datetime import datetime

from .embedding_builder import build_indexes
from .json_store import load_json, product_list, save_json_atomic
from .matcher import build_tuples
from .platform_paths import (
    AMAZON_PRODUCTS,
    FINAL_TUPLES,
    MARKETPLACE_PRODUCTS,
    MASTER_PRODUCTS_PKL,
    MYNTRA_PRODUCTS,
    TATACLIQ_PRODUCTS,
    dated_log_path,
    log_path,
)
from .process_status import mark_started, mark_stopped, update_site_status
from .structured_logging import get_scraper_logger, log_event
from .config import load_config


PIPELINE_SITE = "matcher"


def combine_master_products() -> int:
    amazon = load_json(AMAZON_PRODUCTS, {})
    marketplace = load_json(MARKETPLACE_PRODUCTS, {})
    master = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "amazon": amazon,
        "marketplace": marketplace,
    }
    MASTER_PRODUCTS_PKL.parent.mkdir(parents=True, exist_ok=True)
    with MASTER_PRODUCTS_PKL.open("wb") as handle:
        pickle.dump(master, handle)
    return len(product_list(amazon)) + len(product_list(marketplace))


def log_step(logger, step: int, message: str, status: str = "INFO") -> None:
    level = logging.ERROR if status == "ERROR" else logging.WARNING if status == "WARNING" else logging.INFO
    log_event(logger, level, f"STEP-{step}", message)
    update_site_status(PIPELINE_SITE, {"current_ean": f"STEP-{step}", "message": message})


def run_pipeline(step: str = "all") -> dict:
    logger = get_scraper_logger(PIPELINE_SITE, log_path(PIPELINE_SITE))
    mark_started(PIPELINE_SITE, os.getpid(), "Indexing pipeline starting")
    summary = {
        "master_products": 0,
        "myntra_embeddings": 0,
        "tatacliq_embeddings": 0,
        "tuple_count": 0,
        "match_count": 0,
        "rejected_count": 0,
    }
    try:
        if step in {"all", "1"}:
            log_step(logger, 1, "Loading Amazon and marketplace JSON; generating master_products.pkl")
            summary["master_products"] = combine_master_products()
            update_site_status(PIPELINE_SITE, {"success_count": 1})

        if step in {"all", "2"}:
            log_step(logger, 2, "Generating Myntra CLIP embeddings; building FAISS indexes")
            result = build_indexes([AMAZON_PRODUCTS, MYNTRA_PRODUCTS], build_clip=True, build_dinov2=False)
            summary["myntra_embeddings"] = result["embedded"]
            update_site_status(PIPELINE_SITE, {"success_count": 2})

        if step in {"all", "3"}:
            log_step(logger, 3, "Generating Tata CLiQ CLIP embeddings; rebuilding FAISS indexes")
            result = build_indexes([AMAZON_PRODUCTS, MYNTRA_PRODUCTS, TATACLIQ_PRODUCTS], build_clip=True, build_dinov2=False)
            summary["tatacliq_embeddings"] = result["embedded"]
            update_site_status(PIPELINE_SITE, {"success_count": 3})

        if step in {"all", "4", "5"}:
            log_step(logger, 4, "Running strict Amazon to Myntra and Tata matching")
            payload = build_tuples(FINAL_TUPLES)
            summary["tuple_count"] = payload["summary"]["tuples"]
            summary["match_count"] = payload["summary"]["accepted_cross_market_matches"]
            possible = summary["tuple_count"] * 2
            summary["rejected_count"] = max(0, possible - summary["match_count"])
            update_site_status(PIPELINE_SITE, {"success_count": 4})
            log_step(logger, 5, "Generated final tuples and exported products.pkl, clip.index, metadata.pkl")

        log_step(logger, 6, "Indexing pipeline complete")
        mark_stopped(PIPELINE_SITE, "Indexing pipeline complete")
        return summary
    except Exception as exc:
        log_step(logger, 6, f"Indexing pipeline failed: {exc}", "ERROR")
        update_site_status(PIPELINE_SITE, {"failure_count": 1})
        mark_stopped(PIPELINE_SITE, f"Failed: {exc}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Run indexing and matching pipeline.")
    parser.add_argument("--step", choices=["all", "1", "2", "3", "4", "5"], default="all")
    args = parser.parse_args()
    print(json.dumps(run_pipeline(args.step), indent=2))


if __name__ == "__main__":
    main()
