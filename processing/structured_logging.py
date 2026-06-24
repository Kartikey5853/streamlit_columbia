from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from .platform_paths import dated_log_path, log_path


class StructuredScraperFormatter(logging.Formatter):
    def __init__(self, scraper: str):
        super().__init__()
        self.scraper = scraper

    def format(self, record: logging.LogRecord) -> str:
        ean = getattr(record, "ean", None) or "-"
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "scraper": getattr(record, "scraper", None) or self.scraper,
            "EAN": str(ean),
            "status": record.levelname,
            "message": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=False)


def get_scraper_logger(scraper: str, log_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(f"price_intel.{scraper}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = StructuredScraperFormatter(scraper)
    latest_path = log_file or log_path(scraper)
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    # If the existing latest file was last modified on a previous day, truncate it
    try:
        if latest_path.exists():
            mtime = datetime.fromtimestamp(latest_path.stat().st_mtime).date()
            if mtime != date.today():
                latest_path.write_text("", encoding="utf-8")
    except Exception:
        pass
    # Ensure handlers flush immediately after each emit so downstream UIs
    # (Streamlit, tail readers) see logs in real time.
    class FlushFileHandler(logging.FileHandler):
        def emit(self, record: logging.LogRecord) -> None:
            super().emit(record)
            try:
                self.flush()
            except Exception:
                pass

    class FlushStreamHandler(logging.StreamHandler):
        def emit(self, record: logging.LogRecord) -> None:
            super().emit(record)
            try:
                self.flush()
            except Exception:
                pass

    # Use append mode; file was truncated above if it belonged to a previous day.
    file_handler = FlushFileHandler(latest_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    dated_path = dated_log_path(scraper, date.today().isoformat())
    dated_path.parent.mkdir(parents=True, exist_ok=True)
    dated_handler = FlushFileHandler(dated_path, encoding="utf-8")
    dated_handler.setFormatter(formatter)
    stream_handler = FlushStreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(dated_handler)
    logger.addHandler(stream_handler)
    return logger


def log_event(logger: logging.Logger, level: int, ean: str | None, message: str) -> None:
    logger.log(level, message, extra={"ean": ean or "-"})
    # Ensure all attached handlers flush so readers see the message immediately.
    try:
        for h in logger.handlers:
            try:
                h.flush()
            except Exception:
                pass
    except Exception:
        pass
