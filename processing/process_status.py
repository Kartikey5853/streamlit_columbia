from __future__ import annotations

import os
import signal
import subprocess
import threading
import shutil
from datetime import date, datetime
from typing import Any

from .json_store import load_json, save_json_atomic
from .platform_paths import PROCESS_STATUS, dated_json_path, dated_log_path, latest_json_path, log_path


_LOCK = threading.Lock()


DEFAULT_SITE_STATUS = {
    "running": False,
    "pid": None,
    "child_pid": None,
    "current_ean": None,
    "success_count": 0,
    "failure_count": 0,
    "warning_count": 0,
    "started_at": None,
    "updated_at": None,
    "message": "",
}


def load_status() -> dict[str, Any]:
    value = load_json(PROCESS_STATUS, {})
    return value if isinstance(value, dict) else {}


def save_status(status: dict[str, Any]) -> None:
    save_json_atomic(PROCESS_STATUS, status)


def get_site_status(site: str) -> dict[str, Any]:
    status = load_status()
    record = {**DEFAULT_SITE_STATUS, **status.get(site, {})}
    if record.get("running") and record.get("pid") and not pid_is_running(int(record["pid"])):
        record["running"] = False
        record["message"] = "Process exited"
        update_site_status(site, record)
    return record


def update_site_status(site: str, fields: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        status = load_status()
        record = {**DEFAULT_SITE_STATUS, **status.get(site, {})}
        record.update(fields)
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        status[site] = record
        save_status(status)
        return record


def mark_started(site: str, pid: int, message: str = "Running") -> None:
    update_site_status(site, {
        "running": True,
        "pid": pid,
        "child_pid": None,
        "current_ean": None,
        "success_count": 0,
        "failure_count": 0,
        "warning_count": 0,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "message": message,
    })


def mark_stopped(site: str, message: str = "Stopped") -> None:
    update_site_status(site, {
        "running": False,
        "pid": None,
        "child_pid": None,
        "current_ean": None,
        "message": message,
    })


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate_pid(pid: int | None) -> None:
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def stop_site(site: str) -> None:
    record = get_site_status(site)
    terminate_pid(record.get("child_pid"))
    terminate_pid(record.get("pid"))
    today = date.today().isoformat()
    for source, destination in [
        (log_path(site), dated_log_path(site, today)),
        (latest_json_path(site), dated_json_path(site, today)),
    ]:
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    mark_stopped(site, "Stopped by user")
