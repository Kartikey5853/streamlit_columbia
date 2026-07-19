"""Run the non-Amazon catalog scrapers and ingest each successful result."""
from __future__ import annotations

import argparse
import logging
import os
import traceback

from data_scraper.adventuras_scraper import scrape_columbia_products as scrape_adventuras
from data_scraper.ajio_scraper_wrapper import run as scrape_ajio
from data_scraper.columbia_scraper import scrape_all_products as scrape_columbia
from data_scraper.myntra_scraper import run as scrape_myntra
from data_scraper.tata_scraper import scrape_columbia_products as scrape_tatacliq
from processing.platform_paths import log_path
from processing.process_status import mark_started, mark_stopped, update_site_status
from processing.product_store import ingest_scrape
from processing.structured_logging import get_scraper_logger, log_event


SITE_RUNNERS = {
    "ajio": lambda headless: scrape_ajio(headless=headless),
    "myntra": lambda headless: scrape_myntra(headless=headless),
    # These catalog APIs do not launch a browser; the headless setting is not
    # applicable, which is recorded explicitly in the run log.
    "tatacliq": lambda headless: scrape_tatacliq()[0],
    "columbia": lambda headless: scrape_columbia(),
    "adventuras": lambda headless: scrape_adventuras(),
}


def run_fast_scrapers(headless: bool = True) -> dict:
    site = "fast_scrapers"
    logger = get_scraper_logger(site, log_path(site))
    mark_started(site, os.getpid(), "Starting fast scrapers")
    summary = {"succeeded": [], "failed": {}, "results": {}}
    try:
        for source, runner in SITE_RUNNERS.items():
            if source in {"tatacliq", "columbia", "adventuras"}:
                log_event(logger, logging.INFO, "-", f"{source}: HTTP catalog scraper; headless mode is not applicable")
            else:
                log_event(logger, logging.INFO, "-", f"{source}: starting (headless={headless})")
            try:
                products = runner(headless)
                result = ingest_scrape(source, products)
                summary["succeeded"].append(source)
                summary["results"][source] = result
                log_event(logger, logging.INFO, "-", f"{source}: complete; {result['products']} products, {result['known_products']} known")
            except Exception as exc:
                summary["failed"][source] = str(exc)
                log_event(logger, logging.ERROR, "-", f"{source}: failed; continuing with remaining sources: {exc}")
                log_event(logger, logging.ERROR, "-", traceback.format_exc(limit=8))
        update_site_status(site, {"success_count": len(summary["succeeded"]), "failure_count": len(summary["failed"]), "message": "Fast scraper run complete"})
        return summary
    finally:
        mark_stopped(site, "Fast scraper run complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AJIO, Myntra, TataCliQ, Columbia, and Adventuras.")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headed", action="store_false", dest="headless")
    parser.set_defaults(headless=True)
    args = parser.parse_args()
    result = run_fast_scrapers(args.headless)
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
