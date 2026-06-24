from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
    tmp.replace(path)


def product_list(payload: Any) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("products"), dict):
        return [item for item in payload["products"].values() if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("products"), list):
        return [item for item in payload["products"] if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [item for item in payload.values() if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def products_by_ean(payload: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if isinstance(payload, dict) and isinstance(payload.get("products"), dict):
        source = payload["products"].items()
    elif isinstance(payload, dict):
        source = payload.items()
    else:
        source = []
    for key, product in source:
        if not isinstance(product, dict):
            continue
        ean = normalize_ean(product.get("ean") or product.get("upc") or key)
        if ean:
            out[ean] = product
    for product in product_list(payload):
        ean = normalize_ean(product.get("ean") or product.get("upc"))
        if ean:
            out[ean] = product
    return out


def normalize_ean(value: Any) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) in {12, 13}:
        return digits
    return None
