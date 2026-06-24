from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from processing.platform_paths import AMAZON_PRODUCTS, BASE_DIR, MYNTRA_PRODUCTS, TATACLIQ_PRODUCTS, log_path
from processing.structured_logging import get_scraper_logger, log_event


SCRIPTS = {
    "amazon": BASE_DIR / "data_scraper" / "amazon_scraper_v2.py",
    "myntra": BASE_DIR / "data_scraper" / "mynthra_scraper.py",
    "tatacliq": BASE_DIR / "data_scraper" / "tata_scraper.py",
}

OUTPUTS = {
    "amazon": AMAZON_PRODUCTS,
    "myntra": MYNTRA_PRODUCTS,
    "tatacliq": TATACLIQ_PRODUCTS,
}

def run_site(site: str, headless: bool, passthrough: list[str]) -> int:
    logger = get_scraper_logger(site, log_path(site))
    script = SCRIPTS[site]
    output = OUTPUTS[site]
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(script)]

    if site == "amazon" and "--no-indexer" not in passthrough:
        command.append("--no-indexer")
    if site == "tatacliq":
        command.extend(["--output", str(output), "--no-indexer"])
    if headless:
        command.append("--headless")
    command.extend(passthrough)

    log_event(logger, logging.INFO, None, f"starting {site} scraper")
    with log_path(site).open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return_code = process.wait()

    if return_code == 0:
        log_event(logger, logging.INFO, None, f"{site} scraper completed successfully")
    else:
        log_event(logger, logging.ERROR, None, f"{site} scraper exited with code {return_code}")
    return return_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Run existing scraper through platform wrapper.")
    parser.add_argument("site", choices=sorted(SCRIPTS))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headed", action="store_false", dest="headless")
    parser.set_defaults(headless=False)
    args, passthrough = parser.parse_known_args()
    raise SystemExit(run_site(args.site, args.headless, passthrough))


if __name__ == "__main__":
    main()
