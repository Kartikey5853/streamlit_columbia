from __future__ import annotations

import argparse
import json
from pathlib import Path

from .catalog_engine import build_final_tuples
from .platform_paths import FINAL_TUPLES


def build_tuples(output: Path = FINAL_TUPLES) -> dict:
    return build_final_tuples(output)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build final EAN tuples with one shared visual index.")
    parser.add_argument("--output", default=str(FINAL_TUPLES))
    args = parser.parse_args()
    payload = build_tuples(Path(args.output))
    print(json.dumps(payload.get("summary", {}), indent=2))


if __name__ == "__main__":
    main()