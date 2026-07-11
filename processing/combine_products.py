from __future__ import annotations

import argparse

from .indexing_pipeline import run_pipeline as run_indexing_pipeline


def run_pipeline(skip_embeddings: bool = False) -> dict:
    step = "match" if skip_embeddings else "all"
    return run_indexing_pipeline(step)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build embeddings and final tuples with the shared pipeline.")
    parser.add_argument("--skip-embeddings", action="store_true")
    args = parser.parse_args()
    print(run_pipeline(args.skip_embeddings))


if __name__ == "__main__":
    main()