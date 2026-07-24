from __future__ import annotations

import argparse
import json
import logging
import os
import time
import traceback
from datetime import datetime

from .catalog_engine import enrich_normalized_products_with_clip
from .config import load_config
from .platform_paths import NORMALIZED_PRODUCTS, log_path
from .process_status import mark_started, mark_stopped, update_site_status
from .structured_logging import get_scraper_logger, log_event
from .unified_products import build_normalized_products


PIPELINE_SITE = "matcher"


def _normalize_step(step: str) -> str:
    aliases = {
        "1": "exact", "exact": "exact",
        "2": "clip", "index": "clip", "match": "clip", "clip": "clip",
        "3": "all", "4": "all", "5": "all", "all": "all",
    }
    return aliases.get(step, step)


def run_pipeline(step: str = "all") -> dict:
    """Run the single SKU/EAN-first pipeline.

    1. Collapse Columbia/AJIO/Adventuras by normalised SKU and attach Amazon
       by every Columbia EAN.  2. Build the three-site CLIP index.  3. Query
       each normalized Columbia SKU once and attach its best Myntra/TataCliq hit.
    """
    logger = get_scraper_logger(PIPELINE_SITE, log_path(PIPELINE_SITE))
    mark_started(PIPELINE_SITE, os.getpid(), "Unified pipeline starting")
    normalized_step = _normalize_step(step)
    summary: dict = {"normalized_products": 0, "columbia_clip_queries": 0, "myntra_clip_linked": 0, "tatacliq_clip_linked": 0}
    log_event(logger, logging.INFO, "PIPELINE", f"START unified step={step} normalized={normalized_step}")

    try:
        if normalized_step in {"all", "exact"}:
            started = time.perf_counter()
            payload = build_normalized_products(NORMALIZED_PRODUCTS)
            summary.update(payload.get("summary", {}))
            log_event(logger, logging.INFO, "STEP-1", f"DONE exact SKU/EAN assembly in {time.perf_counter() - started:.2f}s; rows={summary.get('normalized_products', 0)}")
            update_site_status(PIPELINE_SITE, {"success_count": 1, "message": "Exact SKU/EAN dataset ready"})
        else:
            from .unified_products import load_normalized_products
            payload = load_normalized_products(NORMALIZED_PRODUCTS)
            if not payload.get("products"):
                raise RuntimeError("Run the exact SKU/EAN step before CLIP enrichment.")

        if normalized_step in {"all", "clip"}:
            started = time.perf_counter()
            candidate_limit = int(load_config().get("unified_clip_candidate_limit", 100))
            payload = enrich_normalized_products_with_clip(payload, candidate_limit=candidate_limit, output=NORMALIZED_PRODUCTS)
            summary.update(payload.get("summary", {}))
            log_event(logger, logging.INFO, "STEP-2", f"DONE Columbia-to-Myntra/TataCliq CLIP enrichment in {time.perf_counter() - started:.2f}s; queries={summary.get('columbia_clip_queries', 0)}")
            update_site_status(PIPELINE_SITE, {"success_count": 2, "message": "Unified six-site dataset ready"})

        log_event(logger, logging.INFO, "PIPELINE", f"DONE summary={json.dumps(summary, ensure_ascii=False)}")
        update_site_status(PIPELINE_SITE, {"message": f"Completed at {datetime.now().isoformat(timespec='seconds')}"})
        mark_stopped(PIPELINE_SITE, "Unified pipeline complete")
        return summary
    except Exception as exc:
        trace = traceback.format_exc(limit=20)
        log_event(logger, logging.ERROR, "PIPELINE", f"ERROR {exc}")
        log_event(logger, logging.ERROR, "PIPELINE", f"TRACEBACK {trace}")
        update_site_status(PIPELINE_SITE, {"failure_count": 1, "message": f"Failed: {exc}"})
        mark_stopped(PIPELINE_SITE, f"Failed: {exc}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the unified SKU/EAN and targeted CLIP pipeline.")
    parser.add_argument("--step", choices=["all", "exact", "clip", "index", "match", "1", "2", "3", "4", "5"], default="all")
    args = parser.parse_args()
    print(json.dumps(run_pipeline(args.step), indent=2))


if __name__ == "__main__":
    main()
