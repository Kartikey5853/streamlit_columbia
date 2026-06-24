from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from processing.platform_paths import (
    AMAZON_PRODUCTS,
    BASE_DIR,
    MYNTRA_PRODUCTS,
    TATACLIQ_PRODUCTS,
    dated_json_path,
    dated_log_path,
    latest_json_path,
    log_path,
)
from processing.process_status import mark_started, mark_stopped, update_site_status


PYTHON = sys.executable


def command_for(site: str, headless: bool) -> list[str]:
    if site == "amazon":
        command = [PYTHON, str(BASE_DIR / "data_scraper" / "amazon_scraper_v2.py"), "--no-indexer"]
    elif site == "myntra":
        command = [PYTHON, str(BASE_DIR / "data_scraper" / "mynthra_scraper.py")]
    elif site == "tatacliq":
        command = [
            PYTHON,
            str(BASE_DIR / "data_scraper" / "tata_scraper.py"),
            "--output",
            str(latest_json_path("tatacliq")),
            "--no-indexer",
        ]
    elif site in {"ajio", "columbia", "adventuras"}:
        command = [PYTHON, "-m", "scrapers.site_ean_runner", site]
    else:
        raise ValueError(f"Unknown scraper: {site}")
    if headless:
        command.append("--headless")
    return command


def env_for(site: str, run_date: str) -> dict[str, str]:
    env = os.environ.copy()
    env["CPI_SITE"] = site
    env["CPI_OUTPUT"] = str(latest_json_path(site))
    env["CPI_DATED_OUTPUT"] = str(dated_json_path(site, run_date))
    env["CPI_LOG"] = str(log_path(site))
    env["CPI_DATED_LOG"] = str(dated_log_path(site, run_date))
    return env


def parse_status_from_log(site: str, path: Path, position: int) -> tuple[int, dict]:
    counts = {"success_count": 0, "failure_count": 0, "warning_count": 0, "current_ean": None}
    if not path.exists():
        return position, counts
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(position)
        for line in handle:
            parsed = None
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                pass
            if parsed:
                ean = parsed.get("EAN")
                if ean and ean != "-":
                    counts["current_ean"] = ean
                status = str(parsed.get("status", "")).upper()
                message = str(parsed.get("message", "")).lower()
            else:
                status = "INFO"
                message = line.lower()
                ean_match = re.search(r"\b\d{12,13}\b", line)
                if ean_match:
                    counts["current_ean"] = ean_match.group(0)
            if "success" in message or "scraped successfully" in message or " added " in message:
                counts["success_count"] += 1
            if status == "ERROR" or "failed" in message or "error" in message:
                counts["failure_count"] += 1
            if status == "WARNING" or "warning" in message or "not found" in message:
                counts["warning_count"] += 1
        return handle.tell(), counts


def copy_if_exists(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def canonical_output(site: str) -> Path | None:
    return {
        "amazon": AMAZON_PRODUCTS,
        "myntra": MYNTRA_PRODUCTS,
        "tatacliq": TATACLIQ_PRODUCTS,
    }.get(site)


def run(site: str, headless: bool) -> int:
    run_date = date.today().isoformat()
    latest_log = log_path(site)
    dated_log = dated_log_path(site, run_date)
    latest_output = latest_json_path(site)
    dated_output = dated_json_path(site, run_date)

    latest_log.parent.mkdir(parents=True, exist_ok=True)
    latest_output.parent.mkdir(parents=True, exist_ok=True)
    latest_log.write_text("", encoding="utf-8")
    dated_log.write_text("", encoding="utf-8")
    if not latest_output.exists():
        latest_output.write_text("{}\n", encoding="utf-8")
    if not dated_output.exists():
        dated_output.write_text("{}\n", encoding="utf-8")

    mark_started(site, os.getpid(), "Starting")
    command = command_for(site, headless)
    status_position = 0
    totals = {"success_count": 0, "failure_count": 0, "warning_count": 0, "current_ean": None}
    return_code = 1
    with latest_log.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "scraper": site,
            "EAN": "-",
            "status": "INFO",
            "message": f"starting {site} scraper",
        }) + "\n")
        handle.flush()
        process = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env_for(site, run_date),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        update_site_status(site, {"running": True, "child_pid": process.pid, "message": "Running"})
        while process.poll() is None:
            status_position, counts = parse_status_from_log(site, latest_log, status_position)
            totals["success_count"] += counts["success_count"]
            totals["failure_count"] += counts["failure_count"]
            totals["warning_count"] += counts["warning_count"]
            if counts.get("current_ean"):
                totals["current_ean"] = counts["current_ean"]
            update_site_status(site, {
                "current_ean": totals["current_ean"],
                "success_count": totals["success_count"],
                "failure_count": totals["failure_count"],
                "warning_count": totals["warning_count"],
                "message": "Running",
            })
            time.sleep(2)
        return_code = process.returncode
        status_position, counts = parse_status_from_log(site, latest_log, status_position)
        totals["success_count"] += counts["success_count"]
        totals["failure_count"] += counts["failure_count"]
        totals["warning_count"] += counts["warning_count"]
        if counts.get("current_ean"):
            totals["current_ean"] = counts["current_ean"]
        update_site_status(site, {
            "current_ean": totals["current_ean"],
            "success_count": totals["success_count"],
            "failure_count": totals["failure_count"],
            "warning_count": totals["warning_count"],
            "message": f"Exited with code {return_code}",
        })
        handle.write(json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "scraper": site,
            "EAN": "-",
            "status": "INFO" if return_code == 0 else "ERROR",
            "message": f"{site} scraper exited with code {return_code}",
        }) + "\n")
    copy_if_exists(latest_log, dated_log)
    copy_if_exists(latest_output, dated_output)
    canonical = canonical_output(site)
    if canonical is not None:
        copy_if_exists(latest_output, canonical)
    mark_stopped(site, f"Exited with code {return_code}")
    return return_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Managed background scraper runner.")
    parser.add_argument("site", choices=["amazon", "ajio", "columbia", "adventuras", "myntra", "tatacliq"])
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run(args.site, args.headless))


if __name__ == "__main__":
    main()
