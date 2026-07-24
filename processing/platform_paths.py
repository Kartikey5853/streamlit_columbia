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
NORMALIZED_PRODUCTS = JSON_DIR / "combined" / "normalized_products.json"
CANONICAL_MAPPING = JSON_DIR / "combined" / "canonical_product_mapping.json"
IDENTIFIER_LOOKUP = JSON_DIR / "combined" / "identifier_lookup.json"
NORMALIZED_IDENTIFIER_LOOKUP = CACHE_DIR / "normalized_identifier_lookup.json"
LATEST_PRICES = JSON_DIR / "combined" / "latest_prices.json"
PRICE_HISTORY = JSON_DIR / "combined" / "price_history.json"
UNMATCHED_PRODUCTS = JSON_DIR / "combined" / "unmatched_products.json"

SCRAPE_PROGRESS = CACHE_DIR / "scrape_progress.json"
PROCESS_STATUS = CACHE_DIR / "process_status.json"
CLIP_INDEX = EMBEDDINGS_DIR / "clip.index"
METADATA_PKL = EMBEDDINGS_DIR / "metadata.pkl"
EMBEDDING_CACHE_PKL = CACHE_DIR / "clip_embedding_cache.pkl"
VISUAL_INDEX_MANIFEST = CACHE_DIR / "visual_index_manifest.json"
FINAL_TUPLES_MANIFEST = CACHE_DIR / "final_tuples_manifest.json"

SCRAPER_JSON_PRODUCTS = {
    "amazon": BASE_DIR / "data_scraper" / "amazon.json",
    "ajio": BASE_DIR / "data_scraper" / "ajio.json",
    "columbia": BASE_DIR / "data_scraper" / "columbia.json",
    "adventuras": BASE_DIR / "data_scraper" / "adventuras.json",
}

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


def preferred_json_paths(site: str) -> tuple[Path, ...]:
    """Return source JSON paths in preference order for normalization jobs."""
    candidates = []
    scraper_path = SCRAPER_JSON_PRODUCTS.get(site)
    if scraper_path and scraper_path.exists():
        candidates.append(scraper_path)
    current_path = current_json_path(site)
    if current_path.exists() and current_path not in candidates:
        candidates.append(current_path)
    return tuple(candidates)


ensure_directories()
