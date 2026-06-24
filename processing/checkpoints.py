from __future__ import annotations

import threading

from .json_store import load_json, save_json_atomic
from .platform_paths import SCRAPE_PROGRESS, SITES


_LOCK = threading.Lock()


def load_progress() -> dict:
    value = load_json(SCRAPE_PROGRESS, {})
    return value if isinstance(value, dict) else {}


def is_done(ean: str, site: str) -> bool:
    return bool(load_progress().get(str(ean), {}).get(site))


def ensure_ean(progress: dict, ean: str) -> dict:
    record = progress.setdefault(str(ean), {})
    for site in SITES:
        record.setdefault(site, False)
    return record


def mark_done(ean: str, site: str) -> None:
    with _LOCK:
        progress = load_progress()
        record = ensure_ean(progress, str(ean))
        record[site] = True
        save_json_atomic(SCRAPE_PROGRESS, progress)


def mark_pending(ean: str, site: str) -> None:
    with _LOCK:
        progress = load_progress()
        record = ensure_ean(progress, str(ean))
        record[site] = False
        save_json_atomic(SCRAPE_PROGRESS, progress)
