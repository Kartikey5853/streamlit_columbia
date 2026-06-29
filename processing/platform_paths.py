from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
JSON_DIR = DATA_DIR / "json"
EMBEDDINGS_DIR = DATA_DIR / "embeddings"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = BASE_DIR / "logs"
CONFIG_PATH = BASE_DIR / "config.json"

AMAZON_PRODUCTS = JSON_DIR / "amazon" / "amazon_products.json"
MARKETPLACE_PRODUCTS = JSON_DIR / "combined" / "marketplace_products.json"
MYNTRA_PRODUCTS = JSON_DIR / "myntra" / "myntra_products.json"
TATACLIQ_PRODUCTS = JSON_DIR / "tatacliq" / "tatacliq_products.json"
FINAL_TUPLES = JSON_DIR / "combined" / "final_tuples.json"
MASTER_PRODUCTS_PKL = EMBEDDINGS_DIR / "master_products.pkl"

SCRAPE_PROGRESS = CACHE_DIR / "scrape_progress.json"
PROCESS_STATUS = CACHE_DIR / "process_status.json"
PRODUCTS_PKL = EMBEDDINGS_DIR / "products.pkl"
CLIP_INDEX = EMBEDDINGS_DIR / "clip.index"
DINOV2_INDEX = EMBEDDINGS_DIR / "dinov2.index"
METADATA_PKL = EMBEDDINGS_DIR / "metadata.pkl"

SITES = ("amazon", "ajio", "columbia", "adventuras", "myntra", "tatacliq")
OUTPUT_GROUPS = (*SITES, "combined", "matcher")


LEGACY_JSON_FILES = {
    AMAZON_PRODUCTS: JSON_DIR / "amazon_products.json",
    MARKETPLACE_PRODUCTS: JSON_DIR / "marketplace_products.json",
    MYNTRA_PRODUCTS: JSON_DIR / "myntra_products.json",
    TATACLIQ_PRODUCTS: JSON_DIR / "tatacliq_products.json",
    FINAL_TUPLES: JSON_DIR / "final_tuples.json",
}


def ensure_directories() -> None:
    for path in [
        DATA_DIR,
        JSON_DIR,
        EMBEDDINGS_DIR,
        CACHE_DIR,
        LOG_DIR,
        *(JSON_DIR / group for group in OUTPUT_GROUPS),
        *(LOG_DIR / group for group in OUTPUT_GROUPS),
        BASE_DIR / "scrapers" / "amazon",
        BASE_DIR / "scrapers" / "ajio",
        BASE_DIR / "scrapers" / "columbia",
        BASE_DIR / "scrapers" / "adventuras",
        BASE_DIR / "scrapers" / "myntra",
        BASE_DIR / "scrapers" / "tatacliq",
        BASE_DIR / "streamlit_app",
    ]:
        path.mkdir(parents=True, exist_ok=True)
    for new_path, old_path in LEGACY_JSON_FILES.items():
        if old_path.exists() and not new_path.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_bytes(old_path.read_bytes())


def log_path(site: str) -> Path:
    return LOG_DIR / site / f"latest_{site}.log"


def dated_log_path(site: str, date_string: str) -> Path:
    return LOG_DIR / site / f"{site}_{date_string}.log"


def latest_json_path(site: str) -> Path:
    return JSON_DIR / site / f"latest_{site}.json"


def dated_json_path(site: str, date_string: str) -> Path:
    return JSON_DIR / site / f"{site}_{date_string}.json"


def current_json_path(site: str) -> Path:
    latest = latest_json_path(site)
    if latest.exists() and latest.stat().st_size > 0:
        return latest
    canonical = {
        "amazon": AMAZON_PRODUCTS,
        "myntra": MYNTRA_PRODUCTS,
        "tatacliq": TATACLIQ_PRODUCTS,
    }.get(site)
    return canonical or latest


ensure_directories()
