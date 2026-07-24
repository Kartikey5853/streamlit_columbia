from __future__ import annotations

import json
import os
import time
from errno import EACCES, EBUSY, EPERM
from pathlib import Path
from typing import Any
from uuid import uuid4


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def unique_temp_path(path: Path) -> Path:
    """Return a sibling temp path that cannot collide with another writer."""
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")


def replace_file_with_retry(temporary: Path, destination: Path, *, attempts: int = 12) -> None:
    """Atomically replace a file, tolerating short-lived Windows file locks."""
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(temporary, destination)
            return
        except OSError as exc:
            last_error = exc
            if exc.errno not in {EACCES, EBUSY, EPERM} and getattr(exc, "winerror", None) not in {5, 32, 33}:
                raise
            if attempt < attempts - 1:
                time.sleep(0.15 * (attempt + 1))
    raise PermissionError(
        f"Could not replace {destination} after {attempts} attempts; another program is still using the file."
    ) from last_error


def save_json_atomic(path: Path, value: Any) -> None:
    """Write JSON safely without exposing partial files to the UI or pipeline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_temp_path(path)
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save_bytes_atomic(path: Path, value: bytes) -> None:
    """Binary counterpart for portable pipeline artifacts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_temp_path(path)
    try:
        with tmp.open("wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


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
