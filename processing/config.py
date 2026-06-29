from __future__ import annotations

import json
from pathlib import Path

from .platform_paths import CONFIG_PATH


DEFAULT_CONFIG = {
    "ajio_cooldown": 20,
    "columbia_cooldown": 60,
    "adventuras_cooldown": 60,
    "match_threshold": 0.82,
    "match_clip_weight": 0.30,
    "match_title_weight": 0.15,
    "match_price_weight": 0.25,
    "price_no_penalty_diff": 500,
    "price_moderate_penalty_diff": 1000,
    "price_heavy_penalty_diff": 5000,
    "price_moderate_score": 0.65,
    "price_heavy_score": 0.20,
    "price_near_rejection_score": 0.0,
    "reject_near_price_mismatch": True,
    "image_search_threshold": 0.84,
    "price_refresh_delay_seconds": 1.5,
    "python_executable": "",
}


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        save_config(DEFAULT_CONFIG, path)
        return dict(DEFAULT_CONFIG)
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    return {**DEFAULT_CONFIG, **value}


def save_config(config: dict, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump({**DEFAULT_CONFIG, **config}, handle, indent=2)
    tmp.replace(path)
