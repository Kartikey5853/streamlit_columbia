from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import streamlit as st

from processing.config import load_config
from processing.platform_paths import BASE_DIR, dated_log_path, log_path
from processing.process_status import get_site_status, stop_site, mark_started, update_site_status


def enable_auto_refresh(seconds: int = 3) -> None:
    st.markdown(
        f"<script>setTimeout(() => window.location.reload(), {seconds * 1000});</script>",
        unsafe_allow_html=True,
    )


def python_cmd() -> str:
    configured = load_config().get("python_executable")
    return configured or sys.executable


def managed_scraper_command(site: str, headless: bool) -> list[str]:
    command = [python_cmd(), "-m", "scrapers.managed_runner", site]
    if headless:
        command.append("--headless")
    return command


def refresh_command(site: str, headless: bool) -> list[str]:
    command = [python_cmd(), "-m", "processing.refresh_prices", site]
    if headless:
        command.append("--headless")
    else:
        command.append("--headed")
    return command


def start_process(site: str, command: list[str]) -> None:
    status = get_site_status(site)
    if status.get("running"):
        st.warning(f"{site} is already running.")
        return
    # Start the scraper as a background process and mark it started immediately
    process = subprocess.Popen(
        command,
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    try:
        mark_started(site, process.pid, "Started")
        update_site_status(site, {"child_pid": None, "message": "Starting"})
    except Exception:
        pass
    st.success(f"Started {site} (PID {process.pid}).")


def stop_process(site: str) -> None:
    status = get_site_status(site)
    if not status.get("running"):
        st.info(f"{site} is not running.")
        return
    stop_site(site)
    st.warning(f"Stopped {site}.")


def status_badge(site: str) -> dict:
    status = get_site_status(site)
    if status.get("running"):
        st.success(f"Running - PID {status.get('pid')}")
    else:
        st.info(status.get("message") or "Stopped")
    return status


def tail_log(site: str, lines: int = 160) -> str:
    path = log_path(site)
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def render_live_panel(site: str) -> None:
    # Auto-refresh the Streamlit page so logs update in near-real-time.
    enable_auto_refresh(3)
    status = status_badge(site)
    cols = st.columns(4)
    cols[0].metric("Current EAN", status.get("current_ean") or "-")
    cols[1].metric("Success", int(status.get("success_count") or 0))
    cols[2].metric("Failures", int(status.get("failure_count") or 0))
    cols[3].metric("Warnings", int(status.get("warning_count") or 0))
    if status.get("running"):
        st.progress(0.5, text="Scraper running")
    else:
        st.progress(1.0 if "Exited" in str(status.get("message")) else 0.0, text=status.get("message") or "Idle")
    st.subheader("Live logs")
    row = st.container()
    # Tail panel (latest 100 lines)
    with row:
        st.markdown("**Tail Logs (latest 100 lines)**")
        st.code(tail_log(site, 100), language="text")
    # View raw logs button -> offer download of current log file
    log_file = log_path(site)
    if log_file.exists():
        try:
            log_bytes = log_file.read_bytes()
            st.download_button(f"View Raw Logs", data=log_bytes, file_name=log_file.name)
        except Exception:
            st.button("View Raw Logs")


def controls(site: str, headless: bool, allow_refresh: bool = True) -> None:
    command = managed_scraper_command(site, headless)
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        if st.button("Start", key=f"start_{site}", use_container_width=True):
            start_process(site, command)
    with col2:
        if st.button("Stop", key=f"stop_{site}", use_container_width=True):
            stop_process(site)
    with col3:
        if allow_refresh and st.button("Price refresh", key=f"refresh_{site}", use_container_width=True):
            start_process(f"{site}_refresh", refresh_command(site, headless))
    with col4:
        if allow_refresh and st.button("Stop refresh", key=f"stop_refresh_{site}", use_container_width=True):
            stop_process(f"{site}_refresh")
    if allow_refresh:
        refresh_status = get_site_status(f"{site}_refresh")
        if refresh_status.get("running"):
            st.caption(f"Price refresh running - PID {refresh_status.get('pid')}")


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))
