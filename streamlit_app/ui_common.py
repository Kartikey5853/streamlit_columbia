from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import streamlit as st

from processing.config import load_config
from processing.platform_paths import BASE_DIR, JSON_DIR, dated_log_path, log_path
from processing.process_status import get_site_status, stop_site, mark_started, update_site_status


_LOGO_PATH = Path(r"C:\Users\kartikey\AppData\Local\Temp\codex-clipboard-af1cf7b5-ca36-4066-85d4-9eab04eddb4b.png")


def apply_theme() -> None:
    """Apply the shared Columbia dark/cyan shell to every Streamlit page."""
    st.markdown(
        """
        <style>
        :root { --columbia-cyan: #08b9f2; --columbia-blue: #0877b9; --ink: #070b10; --panel: #101820; --line: #1b3341; }
        .stApp { background: #070b10; color: #e6f6fb; }
        [data-testid="stHeader"] { background: rgba(5, 8, 12, .82); border-bottom: 1px solid rgba(8,185,242,.18); }
        [data-testid="stSidebar"] { background: #070d12; border-right: 1px solid #164355; }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color: #b6dce9; }
        h1, h2, h3 { color: #f1fbff !important; letter-spacing: -.02em; }
        h1 { font-size: 2.35rem !important; }
        .block-container { padding-top: 3.25rem; max-width: 1500px; }
        [data-testid="stMetric"] { background: #101820; border: 1px solid #1c5267; border-radius: 14px; padding: 14px 16px; }
        [data-testid="stMetricLabel"] { color: #7fc4d9 !important; }
        [data-testid="stMetricValue"] { color: #e9fbff !important; }
        .stButton > button { background: #0877b9; color: #fff; border: 1px solid #1597c7; border-radius: 9px; font-weight: 700; }
        .stButton > button:hover { background: #0b8bc4; color: #fff; border-color: #22b9e8; }
        [data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div { background: #0d171e; border: 1px solid #285365; border-radius: 9px; color: #e8fbff; }
        [data-baseweb="input"]:focus-within { border-color: var(--columbia-cyan); }
        [data-testid="stExpander"] { background: rgba(12,25,33,.8); border: 1px solid #1b4455; border-radius: 12px; }
        [data-testid="stAlert"] { border-radius: 10px; }
        [data-testid="stSidebarNav"] { border-top: 1px solid #29414b; border-bottom: 1px solid #29414b; padding: 14px 0; margin-top: 8px; }
        [data-testid="stSidebarNav"] a, [data-testid="stSidebarNav"] button { min-height: 48px; margin: 6px 10px; padding: 12px 14px; border: 1px solid #173c4d; border-radius: 9px; color: #b8e9f5 !important; font-size: 1rem; font-weight: 700; background: #0b151b; }
        [data-testid="stSidebarNav"] a:hover, [data-testid="stSidebarNav"] button:hover { background: #102b38; border-color: #08b9f2; color: #ffffff !important; }
        [data-testid="stSidebarNav"] a[aria-current="page"] { background: #0b4f6d; border-color: #08b9f2; color: #ffffff !important; }
        [data-testid="stSidebar"] img[alt="logo"] { width: 100% !important; max-width: 270px !important; height: auto !important; object-fit: contain; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    logo = str(_LOGO_PATH) if _LOGO_PATH.exists() else None
    with st.sidebar:
        if logo:
            # st.logo occupies Streamlit's brand slot, which is rendered
            # above the built-in Home/page navigation.
            st.logo(logo, size="large")
        else:
            st.markdown("<div style='color:#08b9f2;font-size:1.35rem;font-weight:800;padding:8px 0'>◆ COLUMBIA</div>", unsafe_allow_html=True)


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


def fast_scraper_command(headless: bool) -> list[str]:
    command = [python_cmd(), "-m", "scrapers.fast_runner"]
    command.append("--headless" if headless else "--headed")
    return command


def refresh_command(site: str, headless: bool) -> list[str]:
    command = [python_cmd(), "-m", "processing.refresh_prices", site]
    if headless:
        command.append("--headless")
    else:
        command.append("--headed")
    return command


def amazon_v3_refresh_command(headless: bool, tabs: int = 5) -> list[str]:
    command = [python_cmd(), "-m", "processing.amazon_v3_refresh_prices", "--tabs", str(max(1, tabs))]
    command.append("--headless" if headless else "--headed")
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
    with st.expander("Live logs", expanded=False):
        st.markdown("**Tail Logs (latest 100 lines)**")
        st.code(tail_log(site, 100), language="text")
        log_file = log_path(site)
        if log_file.exists():
            try:
                log_bytes = log_file.read_bytes()
                st.download_button("Download raw logs", data=log_bytes, file_name=log_file.name)
            except Exception:
                st.button("Download raw logs")


def render_operational_console(site: str, output_folder: Path | None = None) -> None:
    """Small operational panel used by the two scraper pages."""
    enable_auto_refresh(3)
    status = get_site_status(site)
    st.caption(status.get("message") or "Idle")
    with st.expander("Live logs", expanded=False):
        st.code(tail_log(site, 160) or "No log output yet.", language="text")
    folder = output_folder or JSON_DIR
    if st.button("Open output folder", key=f"open_{site}"):
        try:
            if os.name == "nt":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            st.error(f"Could not open {folder}: {exc}")


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
