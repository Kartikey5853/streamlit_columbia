"""Portable import/export for the files needed to restore pipeline state."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .platform_paths import (
    CANONICAL_MAPPING,
    CLIP_INDEX,
    EMBEDDING_CACHE_PKL,
    FINAL_TUPLES,
    FINAL_TUPLES_MANIFEST,
    METADATA_PKL,
    VISUAL_INDEX_MANIFEST,
)
from .product_store import ensure_final_tuple_identity


ARTIFACTS: dict[str, Path] = {
    "clip.index": CLIP_INDEX,
    "metadata.pkl": METADATA_PKL,
    "clip_embedding_cache.pkl": EMBEDDING_CACHE_PKL,
    "visual_index_manifest.json": VISUAL_INDEX_MANIFEST,
    "final_tuples.json": FINAL_TUPLES,
    "final_tuples_manifest.json": FINAL_TUPLES_MANIFEST,
    "canonical_product_mapping.json": CANONICAL_MAPPING,
}
# Older project exports did not contain the mapping.  It can be safely rebuilt
# from imported tuples and current scraper metadata without invoking CLIP.
REQUIRED_ARTIFACTS = {"clip.index", "metadata.pkl", "final_tuples.json"}


def export_pipeline_artifacts() -> bytes:
    # Existing projects can export immediately; this writes the small mapping
    # migration without rebuilding embeddings or matching.
    ensure_final_tuple_identity()
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, path in ARTIFACTS.items():
            if path.exists():
                archive.writestr(name, path.read_bytes())
    return buffer.getvalue()


def import_pipeline_artifacts(contents: bytes) -> dict:
    with ZipFile(BytesIO(contents)) as archive:
        members = {Path(info.filename).name: info for info in archive.infolist() if not info.is_dir()}
        missing = sorted(REQUIRED_ARTIFACTS - set(members))
        if missing:
            raise ValueError("Archive is missing required pipeline artifacts: " + ", ".join(missing))
        imported: list[str] = []
        for name, path in ARTIFACTS.items():
            info = members.get(name)
            if info is None:
                continue
            data = archive.read(info)
            if not data:
                raise ValueError(f"Artifact is empty: {name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            imported.append(name)
    if "canonical_product_mapping.json" not in imported:
        ensure_final_tuple_identity()
    return {"imported": imported, "missing_optional": sorted(set(ARTIFACTS) - set(imported))}
