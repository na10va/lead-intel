from __future__ import annotations
"""
zillow/zillow_scraper.py — Scrapes active MLS and FSBO listings in target counties.

OWNER USE ONLY — leads go directly to owner, never to VA.
Never enrich with Skip Sherpa. Never score against the distress model.

Counties: Cuyahoga, Lake, Mahoning (Ohio only — do not expand without owner approval)

Crawl rules:
    - Minimum 3–5 second delay between requests
    - Rotate user agents
    - If blocked: log and evaluate BrightData / RentCast as alternative

CLI:
    python zillow/zillow_scraper.py
"""

import random
import time

from db.client import get_client, insert_row
from utils.logger import get_logger
from zillow.arv_calculator import calc_zestimate_arv, calc_comp_arv
from zillow.deal_scorer import score_listing
from zillow.owner_notify import send_deal_alert

log = get_logger("zillow.scraper")

TARGET_COUNTIES = ["Cuyahoga", "Lake", "Mahoning"]

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

MIN_DELAY_SEC = 3
MAX_DELAY_SEC = 5


def _random_delay() -> None:
    """Sleep a random interval between MIN and MAX delay."""
    time.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC))


def _random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def fetch_listings(county: str) -> list[dict]:
    """Fetch active listings for the target county from Zillow.

    TODO: Implement Playwright scraping with user-agent rotation.
    Zillow TOS restricts automated scraping — use a respectful crawl rate.
    If blocked, evaluate Zillow official API, BrightData, or RentCast.
    Filter for: single family + multi-family, active listings only.
    """
    raise NotImplementedError(f"fetch_listings not yet implemented for {county}")


def parse_listing(raw: dict, county: str) -> dict:
    """Extract structured fields from a raw Zillow listing.

    TODO: Map Zillow fields to zillow_deals schema.
    Capture: address, list_price, beds, baths, sqft, days_on_market,
             listing_type (agent/fsbo), zillow_url, zestimate.
    """
    raise NotImplementedError("parse_listing not yet implemented")


def _is_already_stored(address: str) -> bool:
    """Return True if this address was already scraped today."""
    client = get_client()
    from datetime import date
    response = (
        client.table("zillow_deals")
        .select("id")
        .eq("address", address)
        .gte("created_at", date.today().isoformat())
        .execute()
    )
    return bool(response.data)


def run() -> list[dict]:
    """Scrape all target counties, score listings, and notify owner of deals.

    Returns a list of deal dicts that were surfaced to the owner today.
    """
    all_deals = []

    for county in TARGET_COUNTIES:
        log.info(f"Zillow scraper starting — {county} County")
        try:
            raw_listings = fetch_listings(county)
        except NotImplementedError:
            log.warning(f"fetch_listings not implemented for {county} — skipping")
            continue
        except Exception as e:
            log.error(f"Zillow FETCH failed for {county}: {e}")
            continue

        for raw in raw_listings:
            _random_delay()
            try:
                listing = parse_listing(raw, county)
                address = listing.get("address", "")

                if _is_already_stored(address):
                    continue

                # Calculate both ARV methods
                zestimate = calc_zestimate_arv(listing)
                comp_arv = calc_comp_arv(listing)
                listing["zestimate_arv"] = zestimate
                listing["comp_arv"] = comp_arv

                # Score and filter
                scored = score_listing(listing)
                if not scored:
                    continue  # > 75% of ARV — do not surface

                insert_row("zillow_deals", scored)
                all_deals.append(scored)

            except Exception as e:
                log.error(f"Error processing Zillow listing: {e}")
                continue

    if all_deals:
        send_deal_alert(all_deals)

    log.info(f"Zillow scraper complete — {len(all_deals)} deals surfaced to owner")
    return all_deals


if __name__ == "__main__":
    run()
