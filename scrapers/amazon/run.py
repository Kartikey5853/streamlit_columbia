from scrapers.run_existing import run_site


if __name__ == "__main__":
    raise SystemExit(run_site("amazon", headless=False, passthrough=[]))
