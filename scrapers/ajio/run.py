import argparse
import asyncio

from scrapers.site_ean_runner import scrape_site


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--ean", action="append")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    print(asyncio.run(scrape_site("ajio", args.headless, args.ean, args.limit)))
