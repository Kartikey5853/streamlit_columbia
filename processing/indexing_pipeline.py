from __future__ import annotations

import argparse
import json
import logging
import os
import time
import traceback
from datetime import datetime

from .catalog_engine import build_final_tuples, build_visual_index
from .config import load_config
from .platform_paths import current_json_path, log_path
from .process_status import mark_started, mark_stopped, update_site_status
from .structured_logging import get_scraper_logger, log_event


PIPELINE_SITE = "matcher"


def _normalize_step(step: str) -> str:
    aliases = {
        "1": "index",
        "2": "index",
        "3": "match",
        "4": "match",
        "5": "match",
        "all": "all",
        "index": "index",
        "match": "match",
    }
    return aliases.get(step, step)


def run_pipeline(step: str = "all") -> dict:
    logger = get_scraper_logger(PIPELINE_SITE, log_path(PIPELINE_SITE))
    mark_started(PIPELINE_SITE, os.getpid(), "Indexing pipeline starting")
    normalized_step = _normalize_step(step)
    log_event(logger, logging.INFO, "PIPELINE", f"START step={step} normalized={normalized_step}")
    summary = {
        "visual_index": 0,
        "tuple_count": 0,
        "match_count": 0,
        "rejected_count": 0,
    }

    try:
        if normalized_step in {"all", "index"}:
            started = time.perf_counter()
            all_sources = [current_json_path(site) for site in ("amazon", "ajio", "columbia", "adventuras", "myntra", "tatacliq")]
            log_event(logger, logging.INFO, "STEP-1", "START Building shared all-platform visual index")
            result = build_visual_index(all_sources)
            summary["visual_index"] = result["embedded"]
            update_site_status(PIPELINE_SITE, {"success_count": 1, "message": "Visual index ready"})
            elapsed = time.perf_counter() - started
            log_event(
                logger,
                logging.INFO,
                "STEP-1",
                (
                    f"DONE shared visual index in {elapsed:.2f}s; embedded={result.get('embedded', 0)} "
                    f"cached={bool(result.get('cached'))} download_failures={result.get('download_failures', 0)}"
                ),
            )

        if normalized_step in {"all", "match"}:
            started = time.perf_counter()
            log_event(logger, logging.INFO, "STEP-2", "START Building final tuples with one Amazon-to-target search pass")
            payload = build_final_tuples()
            summary["tuple_count"] = payload["summary"]["tuples"]
            summary["match_count"] = payload["summary"]["accepted_cross_market_matches"]
            summary["rejected_count"] = max(0, (summary["tuple_count"] * 2) - summary["match_count"])
            update_site_status(PIPELINE_SITE, {"success_count": 2, "message": "Final tuples ready"})
            elapsed = time.perf_counter() - started
            log_event(
                logger,
                logging.INFO,
                "STEP-2",
                (
                    f"DONE final tuples in {elapsed:.2f}s; tuples={summary['tuple_count']} "
                    f"accepted={summary['match_count']} rejected={summary['rejected_count']}"
                ),
            )

        log_event(logger, logging.INFO, "PIPELINE", f"DONE summary={json.dumps(summary, ensure_ascii=False)}")
        update_site_status(PIPELINE_SITE, {"message": f"Completed at {datetime.now().isoformat(timespec='seconds')}"})
        mark_stopped(PIPELINE_SITE, "Indexing pipeline complete")
        return summary
    except Exception as exc:
        trace = traceback.format_exc(limit=20)
        log_event(logger, logging.ERROR, "PIPELINE", f"ERROR {exc}")
        log_event(logger, logging.ERROR, "PIPELINE", f"TRACEBACK {trace}")
        update_site_status(PIPELINE_SITE, {"failure_count": 1, "message": f"Failed: {exc}"})
        mark_stopped(PIPELINE_SITE, f"Failed: {exc}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the shared indexing and matching pipeline.")
    parser.add_argument("--step", choices=["all", "index", "match", "1", "2", "3", "4", "5"], default="all")
    args = parser.parse_args()
    print(json.dumps(run_pipeline(args.step), indent=2))


if __name__ == "__main__":
    main()