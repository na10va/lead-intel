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

import json
import random
import re
import time
from datetime import date

import requests

from db.client import get_client, insert_row
from utils.logger import get_logger
from zillow.arv_calculator import calc_zestimate_arv, calc_comp_arv
from zillow.deal_scorer import score_listing
from zillow.owner_notify import send_deal_alert

log = get_logger("zillow.scraper")

TARGET_COUNTIES = ["Cuyahoga", "Lake", "Mahoning"]

# Only these columns exist in the zillow_deals table — strip extras before insert
_ZILLOW_DEALS_COLUMNS = {
    "address", "county", "list_price", "zestimate_arv", "comp_arv",
    "pct_of_comp_arv", "beds", "baths", "sqft", "days_on_market",
    "listing_type", "arv_conflict", "label", "zillow_url", "alerted_owner",
}

# County URL slugs for Zillow search
COUNTY_SLUG = {
    "Cuyahoga": "cuyahoga-county-oh",
    "Lake":     "lake-county-oh",
    "Mahoning": "mahoning-county-oh",
}

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

MIN_DELAY_SEC = 3
MAX_DELAY_SEC = 5
MAX_PAGES = 25  # 25 pages × 40 listings = up to 1,000 per county


def _random_delay() -> None:
    time.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC))


def _random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def _get_headers() -> dict:
    return {
        "User-Agent": _random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _extract_next_data(html: str) -> dict:
    """Extract the __NEXT_DATA__ JSON embedded in Zillow's HTML."""
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def _find_list_results(data: dict) -> list[dict]:
    """Navigate the __NEXT_DATA__ tree to find listResults, trying multiple paths."""
    # Try common paths in order — Zillow updates their structure occasionally
    candidate_paths = [
        ["props", "pageProps", "componentProps", "searchPageState", "cat1", "searchResults", "listResults"],
        ["props", "pageProps", "initialData", "searchPageState", "cat1", "searchResults", "listResults"],
        ["props", "initialData", "searchPageState", "cat1", "searchResults", "listResults"],
        ["props", "pageProps", "searchPageState", "cat1", "searchResults", "listResults"],
    ]
    for path in candidate_paths:
        node = data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, list) and node:
            return node

    # Fallback: recursive search for the key
    return _deep_search(data, "listResults") or []


def _find_total_pages(data: dict) -> int:
    """Extract total page count from Zillow's search state."""
    # Try to find totalPages anywhere in the tree
    result = _deep_search(data, "totalPages")
    if isinstance(result, int):
        return result
    return 1


def _deep_search(node, target_key: str):
    """Recursively search for the first occurrence of target_key in a nested structure."""
    if isinstance(node, dict):
        if target_key in node:
            return node[target_key]
        for v in node.values():
            found = _deep_search(v, target_key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _deep_search(item, target_key)
            if found is not None:
                return found
    return None


def fetch_listings(county: str) -> list[dict]:
    """Fetch active for-sale listings for the target county from Zillow.

    Uses Zillow's __NEXT_DATA__ JSON embedded in search result pages.
    Paginates up to MAX_PAGES. Returns raw listing dicts from Zillow's schema.
    """
    slug = COUNTY_SLUG.get(county)
    if not slug:
        raise ValueError(f"Unknown county: {county}")

    base_url = f"https://www.zillow.com/{slug}/houses/"
    all_raw = []
    session = requests.Session()

    for page in range(1, MAX_PAGES + 1):
        url = base_url if page == 1 else f"{base_url}{page}_p/"
        log.debug(f"Zillow fetch: {county} page {page} — {url}")

        try:
            resp = session.get(url, headers=_get_headers(), timeout=30)
        except requests.RequestException as e:
            log.error(f"Zillow request failed ({county} page {page}): {e}")
            break

        if resp.status_code == 403:
            log.warning(f"Zillow blocked request — {county} page {page} (403). Stopping pagination.")
            break
        if resp.status_code != 200:
            log.warning(f"Zillow returned {resp.status_code} for {county} page {page}")
            break

        data = _extract_next_data(resp.text)
        if not data:
            log.warning(f"No __NEXT_DATA__ found for {county} page {page} — Zillow may have changed structure")
            break

        page_listings = _find_list_results(data)
        if not page_listings:
            log.debug(f"No listings on {county} page {page} — end of results")
            break

        all_raw.extend(page_listings)
        log.debug(f"  Got {len(page_listings)} listings (running total: {len(all_raw)})")

        total_pages = _find_total_pages(data)
        if page >= total_pages:
            break

        _random_delay()

    log.info(f"Zillow fetch complete — {county}: {len(all_raw)} raw listings")
    return all_raw


def _clean_price(val) -> int | None:
    """Convert Zillow price to integer — handles int, float, or '$X,XXX' string."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    cleaned = re.sub(r"[^\d]", "", str(val))
    return int(cleaned) if cleaned else None


def parse_listing(raw: dict, county: str) -> dict:
    """Map a raw Zillow listing dict to the zillow_deals schema.

    Zillow's listResults items use camelCase keys. We map to our snake_case schema
    and detect listing type (agent vs fsbo) from the Zillow status/flags.
    """
    hd = raw.get("hdpData", {}).get("homeInfo", {}) or {}

    # Price — prefer the explicit unformatted field, fall back to formatted string
    list_price = _clean_price(raw.get("unformattedPrice")) or _clean_price(raw.get("price"))

    # Address — Zillow includes full formatted address in the top-level field
    address = raw.get("address", "") or hd.get("streetAddress", "")
    if not address:
        address = ", ".join(filter(None, [
            hd.get("streetAddress", ""),
            hd.get("city", ""),
            hd.get("state", ""),
            hd.get("zipcode", ""),
        ]))

    beds_raw = raw.get("beds") or hd.get("bedrooms")
    baths_raw = raw.get("baths") or hd.get("bathrooms")
    sqft_raw = raw.get("area") or hd.get("livingArea")

    # FSBO detection — Zillow uses several different indicators
    listing_type_raw = (
        raw.get("listingType", "")
        or raw.get("statusType", "")
        or hd.get("listingTypeDimension", "")
    ).upper()
    is_fsbo = any(x in listing_type_raw for x in ("BY_OWNER", "FOR_SALE_BY_OWNER", "FSBO"))
    listing_type = "fsbo" if is_fsbo else "agent"

    detail_url = raw.get("detailUrl", "")
    zillow_url = f"https://www.zillow.com{detail_url}" if detail_url.startswith("/") else detail_url

    return {
        "address": address.strip(),
        "county": county,
        "list_price": list_price,
        "zestimate": raw.get("zestimate") or hd.get("zestimate"),
        "beds": int(beds_raw) if beds_raw is not None else None,
        "baths": float(baths_raw) if baths_raw is not None else None,
        "sqft": int(sqft_raw) if sqft_raw is not None else None,
        "days_on_market": raw.get("daysOnZillow") or hd.get("daysOnZillow"),
        "listing_type": listing_type,
        "zillow_url": zillow_url,
        "zipcode": hd.get("zipcode", ""),
        "property_type": hd.get("homeType", "SINGLE_FAMILY"),
    }


def _is_already_stored(address: str) -> bool:
    """Return True if this address was already scraped today."""
    client = get_client()
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
        except Exception as e:
            log.error(f"Zillow FETCH failed for {county}: {e}")
            continue

        county_deals = 0
        for raw in raw_listings:
            _random_delay()
            try:
                listing = parse_listing(raw, county)
                address = listing.get("address", "")

                if not address or not listing.get("list_price"):
                    continue

                if _is_already_stored(address):
                    continue

                # Calculate both ARV methods
                listing["zestimate_arv"] = calc_zestimate_arv(listing)
                listing["comp_arv"] = calc_comp_arv(listing)

                # Score and filter — returns None if > 75% ARV
                scored = score_listing(listing)
                if not scored:
                    continue

                row_to_insert = {k: v for k, v in scored.items() if k in _ZILLOW_DEALS_COLUMNS}
                insert_row("zillow_deals", row_to_insert)
                all_deals.append(scored)
                county_deals += 1

            except Exception as e:
                log.error(f"Error processing Zillow listing ({county}): {e}")
                continue

        log.info(f"  {county}: {county_deals} deals surfaced (≤75% ARV)")

    if all_deals:
        send_deal_alert(all_deals)

    log.info(f"Zillow scraper complete — {len(all_deals)} total deals surfaced to owner")
    return all_deals


if __name__ == "__main__":
    run()
