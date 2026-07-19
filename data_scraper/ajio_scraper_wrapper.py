"""
AJIO Scraper Wrapper
============================================================

Uses:
- Installed Google Chrome
- Dedicated persistent Chrome profile
- Chrome Remote Debugging / CDP
- Existing ajio_scraper.js

FIRST RUN:
1. Run this Python file.
2. Chrome opens automatically.
3. Log into Google if you want.
4. The script waits 59 seconds.
5. AJIO scraper starts.

FUTURE RUNS:
- The same Chrome profile is reused.
- Login/cookies/browser state are remembered.

IMPORTANT:
- Keep ajio_scraper.js in the same folder as this file.
- Close any existing C:\\ChromeDebugProfile Chrome window
  before starting a new run.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


# ================================================================
# BASE PATHS
# ================================================================

BASE_DIR = Path(__file__).resolve().parent

JS_SCRAPER_PATH = BASE_DIR / "ajio_scraper.js"

OUTPUT_FILE = BASE_DIR / "ajio_columbia_products.json"


# ================================================================
# CHROME CONFIG
# ================================================================

# Installed Google Chrome executable.
CHROME_PATH = Path(
    r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)

# Dedicated Chrome profile.
#
# This is NOT your normal Chrome Default profile.
#
# Chrome will create this folder automatically if it doesn't exist.
# Log into Google once in this profile and the login will persist.
CHROME_USER_DATA_DIR = Path(
    r"C:\ChromeDebugProfile"
)

# Remote debugging.
CDP_HOST = "127.0.0.1"
CDP_PORT = 9222

CDP_URL = f"http://{CDP_HOST}:{CDP_PORT}"

CDP_VERSION_URL = (
    f"http://{CDP_HOST}:{CDP_PORT}/json/version"
)


# ================================================================
# AJIO CONFIG
# ================================================================

AJIO_URL = "https://www.ajio.com"


# ================================================================
# TIMING
# ================================================================

# How long to wait for Chrome's debugging port.
CHROME_START_TIMEOUT_SECONDS = 30

# How long AJIO gets to load before JS injection.
AJIO_LOAD_WAIT_SECONDS = 59

# How often Python checks JS scraper status.
POLL_INTERVAL_SECONDS = 5

# Maximum amount of time for complete AJIO scraping.
MAX_SCRAPER_WAIT_SECONDS = 60 * 60


# ================================================================
# LOGGING
# ================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger(
    "ajio_wrapper"
)


# ================================================================
# LOAD JAVASCRIPT
# ================================================================

def load_js_scraper() -> str:
    """
    Load the existing working ajio_scraper.js.
    """

    if not JS_SCRAPER_PATH.exists():

        raise FileNotFoundError(
            f"Could not find JS scraper:\n"
            f"{JS_SCRAPER_PATH}\n\n"
            "Place ajio_scraper.js in the same folder "
            "as this Python file."
        )

    return JS_SCRAPER_PATH.read_text(
        encoding="utf-8"
    )


# ================================================================
# CHECK CHROME
# ================================================================

def validate_chrome() -> None:
    """
    Verify installed Google Chrome exists.
    """

    if not CHROME_PATH.exists():

        raise FileNotFoundError(
            f"Google Chrome was not found at:\n"
            f"{CHROME_PATH}\n\n"
            "Update CHROME_PATH in this script if Chrome "
            "is installed somewhere else."
        )


# ================================================================
# CHECK CDP
# ================================================================

def is_cdp_available() -> bool:
    """
    Check whether Chrome remote debugging is available.
    """

    try:

        with urllib.request.urlopen(
            CDP_VERSION_URL,
            timeout=1,
        ) as response:

            return response.status == 200

    except Exception:

        return False


# ================================================================
# START CHROME
# ================================================================

def start_chrome(headless: bool = False) -> subprocess.Popen | None:
    """
    Start installed Google Chrome with:

    - Dedicated persistent profile
    - Remote debugging enabled
    - AJIO opened automatically

    If Chrome is already listening on port 9222,
    no new Chrome process is launched.
    """

    validate_chrome()


    # ------------------------------------------------------------
    # If Chrome debugging is already running, reuse it.
    # ------------------------------------------------------------

    if is_cdp_available():

        log.info(
            "Chrome debugging session already running."
        )

        log.info(
            "Reusing existing Chrome session."
        )

        return None


    # ------------------------------------------------------------
    # Create profile directory if needed.
    # ------------------------------------------------------------

    CHROME_USER_DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


    log.info(
        "Starting installed Google Chrome..."
    )

    log.info(
        f"Chrome executable: {CHROME_PATH}"
    )

    log.info(
        "Persistent Chrome profile: "
        f"{CHROME_USER_DATA_DIR}"
    )


    command = [
        str(CHROME_PATH),

        f"--remote-debugging-port={CDP_PORT}",

        f"--remote-debugging-address={CDP_HOST}",

        f"--user-data-dir={CHROME_USER_DATA_DIR}",

        "--start-maximized",

        "--no-first-run",

        "--no-default-browser-check",
    ]
    if headless:
        command.append("--headless=new")
    command.append(AJIO_URL)


    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


    log.info(
        "Chrome process started."
    )


    return process


# ================================================================
# WAIT FOR CHROME
# ================================================================

def wait_for_cdp() -> None:
    """
    Wait until Chrome exposes its remote debugging endpoint.
    """

    log.info(
        "Waiting for Chrome debugging port..."
    )


    start_time = time.monotonic()


    while (
        time.monotonic() - start_time
        < CHROME_START_TIMEOUT_SECONDS
    ):

        if is_cdp_available():

            log.info(
                "Chrome debugging port is ready."
            )

            return


        time.sleep(1)


    raise RuntimeError(
        f"Chrome started, but remote debugging did not "
        f"become available at:\n"
        f"{CDP_URL}\n\n"
        f"Try closing the Chrome window using "
        f"{CHROME_USER_DATA_DIR} and run the script again."
    )


# ================================================================
# CONNECT PLAYWRIGHT
# ================================================================

def connect_to_chrome(playwright):
    """
    Connect Playwright to the externally launched
    Google Chrome instance.
    """

    log.info(
        "Connecting Playwright to Chrome..."
    )


    browser = (
        playwright
        .chromium
        .connect_over_cdp(
            CDP_URL
        )
    )


    log.info(
        "Playwright connected to Chrome."
    )


    return browser


# ================================================================
# FIND AJIO TAB
# ================================================================

def find_or_open_ajio_page(browser):
    """
    Find an existing AJIO tab.

    If none exists, open one.
    """

    for context in browser.contexts:

        for page in context.pages:

            if "ajio.com" in page.url.lower():

                log.info(
                    f"Found AJIO tab: {page.url}"
                )

                return page


    # ------------------------------------------------------------
    # No AJIO tab found.
    # ------------------------------------------------------------

    if not browser.contexts:

        raise RuntimeError(
            "Chrome connected successfully, "
            "but no browser context was found."
        )


    context = browser.contexts[0]


    log.info(
        "No AJIO tab found."
    )

    log.info(
        "Opening new AJIO tab..."
    )


    page = context.new_page()


    page.goto(
        AJIO_URL,
        wait_until="domcontentloaded",
        timeout=60_000,
    )


    return page


# ================================================================
# BROWSER CONSOLE LOGGING
# ================================================================

def setup_console_logging(page) -> None:
    """
    Forward browser console logs to Python.
    """

    def handle_console(msg):

        try:

            log.info(
                "[browser console] "
                f"{msg.text}"
            )

        except Exception:

            pass


    page.on(
        "console",
        handle_console,
    )


# ================================================================
# AJIO API LOGGING
# ================================================================

def setup_api_logging(page) -> None:
    """
    Log responses from the AJIO Columbia API.

    This only observes responses.
    It does not intercept or modify requests.
    """

    def handle_response(response):

        try:

            if (
                "/api/category/columbia"
                in response.url
            ):

                log.info(
                    "[AJIO API] "
                    f"{response.status} | "
                    f"{response.url}"
                )

        except Exception:

            pass


    page.on(
        "response",
        handle_response,
    )


# ================================================================
# RESET SCRAPER STATE
# ================================================================

def reset_scraper_state(page) -> None:
    """
    Clear previous scraper state before injecting JS.
    """

    page.evaluate(
        """
        () => {

            window.__AJIO_PRODUCTS__ = [];

            window.__AJIO_LAST_PAGE__ = null;

            window.__AJIO_SCRAPER_DONE__ = false;

            window.__AJIO_SCRAPER_ERROR__ = null;

        }
        """
    )


# ================================================================
# WAIT FOR SCRAPER
# ================================================================

def wait_for_scraper(page) -> bool:
    """
    Monitor the AJIO JS scraper.

    Expected variables:

    window.__AJIO_PRODUCTS__
    window.__AJIO_LAST_PAGE__
    window.__AJIO_SCRAPER_DONE__
    window.__AJIO_SCRAPER_ERROR__
    """

    start_time = time.monotonic()


    while (
        time.monotonic() - start_time
        < MAX_SCRAPER_WAIT_SECONDS
    ):

        try:

            state = page.evaluate(
                """
                () => ({

                    done:
                        window.__AJIO_SCRAPER_DONE__
                        === true,

                    count:
                        Array.isArray(
                            window.__AJIO_PRODUCTS__
                        )
                            ? window.__AJIO_PRODUCTS__.length
                            : 0,

                    lastPage:
                        window.__AJIO_LAST_PAGE__
                        ?? null,

                    error:
                        window.__AJIO_SCRAPER_ERROR__
                        ?? null

                })
                """
            )


        except Exception as error:

            log.warning(
                "Could not read scraper state: "
                f"{error}"
            )

            time.sleep(
                POLL_INTERVAL_SECONDS
            )

            continue


        count = state.get(
            "count",
            0,
        )

        last_page = state.get(
            "lastPage",
        )

        done = state.get(
            "done",
            False,
        )

        error = state.get(
            "error",
        )


        log.info(
            "Scraping status | "
            f"products: {count} | "
            f"last page: {last_page}"
        )


        if error:

            log.warning(
                "AJIO JS reported: "
                f"{error}"
            )


        if done:

            log.info(
                "AJIO JS scraper signaled completion."
            )

            return True


        time.sleep(
            POLL_INTERVAL_SECONDS
        )


    return False


# ================================================================
# EXTRACT PRODUCTS
# ================================================================

def extract_products(page) -> list[dict]:
    """
    Read products collected by ajio_scraper.js.
    """

    try:

        products = page.evaluate(
            """
            () => {

                if (
                    Array.isArray(
                        window.__AJIO_PRODUCTS__
                    )
                ) {

                    return window.__AJIO_PRODUCTS__;

                }

                return [];

            }
            """
        )


        if isinstance(
            products,
            list,
        ):

            return products


    except Exception as error:

        log.error(
            "Could not extract products: "
            f"{error}"
        )


    return []


# ================================================================
# SAVE PRODUCTS
# ================================================================

def save_products(products: list[dict], output_file: Path = OUTPUT_FILE) -> None:
    """
    Save products to JSON.
    """

    with output_file.open(
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            products,
            file,
            indent=2,
            ensure_ascii=False,
        )


    log.info(
        f"Saved {len(products)} products "
        f"to {output_file}"
    )


# ================================================================
# RUN AJIO SCRAPER
# ================================================================

def run(headless: bool = False) -> list[dict]:
    """
    Main AJIO scraper wrapper.
    """

    # ------------------------------------------------------------
    # Load existing working JavaScript.
    # ------------------------------------------------------------

    js_code = load_js_scraper()


    log.info(
        "Loaded AJIO scraper script "
        f"({len(js_code)} characters)."
    )


    # ------------------------------------------------------------
    # Start external installed Chrome.
    # ------------------------------------------------------------

    start_chrome(headless=headless)


    # ------------------------------------------------------------
    # Wait until CDP is ready.
    # ------------------------------------------------------------

    wait_for_cdp()


    # ------------------------------------------------------------
    # Connect Playwright.
    # ------------------------------------------------------------

    with sync_playwright() as p:

        browser = connect_to_chrome(
            p
        )


        # --------------------------------------------------------
        # Find AJIO.
        # --------------------------------------------------------

        page = find_or_open_ajio_page(
            browser
        )


        # --------------------------------------------------------
        # Set up logging.
        # --------------------------------------------------------

        setup_console_logging(
            page
        )


        setup_api_logging(
            page
        )


        # --------------------------------------------------------
        # Wait for initial DOM.
        # --------------------------------------------------------

        try:

            page.wait_for_load_state(
                "domcontentloaded",
                timeout=60_000,
            )

        except Exception:

            log.warning(
                "DOM load-state wait timed out. "
                "Continuing with 59-second wait."
            )


        # ========================================================
        # WAIT 59 SECONDS
        # ========================================================

        log.info(
            "========================================"
        )

        log.info(
            "AJIO is open."
        )

        log.info(
            f"Waiting {AJIO_LOAD_WAIT_SECONDS} seconds "
            "before injecting scraper..."
        )

        log.info(
            "On the FIRST RUN, you can log into Google "
            "in this Chrome profile during this time."
        )

        log.info(
            "Your login will remain saved in "
            f"{CHROME_USER_DATA_DIR}."
        )

        log.info(
            "========================================"
        )


        page.wait_for_timeout(
            AJIO_LOAD_WAIT_SECONDS
            * 1000
        )


        log.info(
            "59-second wait complete."
        )


        # ========================================================
        # RESET SCRAPER STATE
        # ========================================================

        log.info(
            "Resetting AJIO scraper state..."
        )


        reset_scraper_state(
            page
        )


        # ========================================================
        # INJECT EXISTING JS
        #
        # EXACTLY ONCE.
        # ========================================================

        log.info(
            "Injecting ajio_scraper.js ONCE..."
        )


        page.add_script_tag(
            content=js_code
        )


        log.info(
            "AJIO scraper injected."
        )


        # ========================================================
        # WAIT FOR JS
        # ========================================================

        completed = wait_for_scraper(
            page
        )


        if not completed:

            log.warning(
                "Maximum scraper wait time reached."
            )

            log.warning(
                "Saving all products collected so far."
            )


        # ========================================================
        # EXTRACT PRODUCTS
        # ========================================================

        products = extract_products(
            page
        )


        log.info(
            f"Retrieved {len(products)} products."
        )


        # IMPORTANT:
        #
        # We intentionally DO NOT call:
        #
        # browser.close()
        #
        # because this is an externally launched Chrome.
        #
        # The Chrome window remains open after scraping.


        return products


# ================================================================
# MAIN
# ================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AJIO scraper wrapper.")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headed", action="store_false", dest="headless")
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE)
    parser.set_defaults(headless=False)
    args = parser.parse_args()

    try:

        products = run(headless=args.headless)


        save_products(
            products, args.output
        )


        log.info(
            "========================================"
        )

        log.info(
            "AJIO SCRAPING COMPLETE"
        )

        log.info(
            f"Total products: {len(products)}"
        )

        log.info(
            f"Output: {OUTPUT_FILE}"
        )

        log.info(
            "========================================"
        )


    except KeyboardInterrupt:

        log.warning(
            "Scraper stopped by user."
        )

        sys.exit(1)


    except Exception as error:

        log.exception(
            "AJIO wrapper failed: "
            f"{error}"
        )

        sys.exit(1)


if __name__ == "__main__":

    main()
