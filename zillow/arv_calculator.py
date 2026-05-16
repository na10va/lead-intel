from __future__ import annotations
"""
zillow/arv_calculator.py — Calculates ARV two ways for each Zillow listing.

Method 1 — Zestimate ARV: pull Zillow's Zestimate for the property (fast reference).
Method 2 — Comp-Based ARV: median $/sqft of last 90 days of sold comps × subject sqft.
    Comp criteria: same zip, beds ±1, baths ±1, sqft ±20%, same property type.

If the two methods diverge by more than 15%, set arv_conflict=True.
"""

import json
import random
import re
import time

import requests

from utils.logger import get_logger

log = get_logger("zillow.arv_calculator")

COMP_LOOKBACK_DAYS = 90
ARV_CONFLICT_THRESHOLD = 0.15  # 15%
MIN_COMPS = 3

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


def _headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
    }


def _extract_next_data(html: str) -> dict:
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


def _deep_search(node, target_key: str):
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


def _clean_price(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    cleaned = re.sub(r"[^\d]", "", str(val))
    return int(cleaned) if cleaned else None


def calc_zestimate_arv(listing: dict) -> int | None:
    """Return the Zestimate for the subject property.

    The Zestimate is captured during scraping and stored in listing["zestimate"].
    Returns None if not available.
    """
    zestimate = listing.get("zestimate")
    if zestimate and isinstance(zestimate, (int, float)) and zestimate > 0:
        return int(zestimate)
    return None


def fetch_sold_comps(listing: dict) -> list[dict]:
    """Fetch sold comparables from Zillow's recently-sold search for the subject's zip.

    Filters results to:
        - Sold within last 90 days (Zillow pre-filters by "recently_sold" URL)
        - Beds within ±1 of subject
        - Baths within ±1 of subject
        - Sqft within ±20% of subject
        - Same property type (flexible — SINGLE_FAMILY matches SINGLE_FAMILY)

    Returns a list of dicts with sale_price and sqft for each qualifying comp.
    Returns empty list if the zipcode is missing or Zillow is unreachable.
    """
    zipcode = listing.get("zipcode", "").strip()
    if not zipcode:
        log.debug(f"No zipcode for {listing.get('address')} — cannot fetch comps")
        return []

    subject_beds = listing.get("beds")
    subject_baths = listing.get("baths")
    subject_sqft = listing.get("sqft")
    subject_type = listing.get("property_type", "SINGLE_FAMILY")

    url = f"https://www.zillow.com/homes/recently_sold/{zipcode}_rb/"
    log.debug(f"Fetching sold comps for zip {zipcode} from {url}")

    try:
        time.sleep(random.uniform(2.0, 3.5))
        resp = requests.get(url, headers=_headers(), timeout=30)
    except requests.RequestException as e:
        log.warning(f"Comp fetch request failed for zip {zipcode}: {e}")
        return []

    if resp.status_code != 200:
        log.warning(f"Comp fetch returned {resp.status_code} for zip {zipcode}")
        return []

    data = _extract_next_data(resp.text)
    raw_results = _deep_search(data, "listResults") or []

    comps = []
    for item in raw_results:
        hd = item.get("hdpData", {}).get("homeInfo", {}) or {}

        # Sale price
        sale_price = _clean_price(item.get("unformattedPrice")) or _clean_price(item.get("price"))
        if not sale_price or sale_price <= 0:
            continue

        # Sqft
        sqft_raw = item.get("area") or hd.get("livingArea")
        if not sqft_raw:
            continue
        sqft = int(sqft_raw)
        if sqft <= 0:
            continue

        # Bed/bath/sqft/type filters
        comp_beds = item.get("beds") or hd.get("bedrooms")
        comp_baths = item.get("baths") or hd.get("bathrooms")
        comp_type = hd.get("homeType", "SINGLE_FAMILY")

        if subject_beds is not None and comp_beds is not None:
            if abs(int(comp_beds) - int(subject_beds)) > 1:
                continue

        if subject_baths is not None and comp_baths is not None:
            if abs(float(comp_baths) - float(subject_baths)) > 1:
                continue

        if subject_sqft is not None and subject_sqft > 0:
            sqft_delta = abs(sqft - subject_sqft) / subject_sqft
            if sqft_delta > 0.20:
                continue

        # Loose property type match — both must be residential
        residential = {"SINGLE_FAMILY", "MULTI_FAMILY", "TOWNHOUSE", "CONDO"}
        if subject_type in residential and comp_type not in residential:
            continue

        comps.append({"sale_price": sale_price, "sqft": sqft})

    log.debug(f"  Found {len(comps)} qualifying comps for zip {zipcode}")
    return comps


def calc_comp_arv(listing: dict) -> int | None:
    """Calculate comp-based ARV using median $/sqft of recent sold comps.

    Returns the comp ARV in USD, or None if insufficient comp data.
    """
    subject_sqft = listing.get("sqft")
    if not subject_sqft or subject_sqft <= 0:
        log.debug(f"No sqft for {listing.get('address')} — cannot calc comp ARV")
        return None

    try:
        comps = fetch_sold_comps(listing)
    except Exception as e:
        log.error(f"Error fetching comps for {listing.get('address')}: {e}")
        return None

    if len(comps) < MIN_COMPS:
        log.debug(f"Insufficient comps ({len(comps)}) for {listing.get('address')} — comp ARV unavailable")
        return None

    ppsf_values = sorted(
        c["sale_price"] / c["sqft"]
        for c in comps
        if c.get("sqft", 0) > 0
    )

    if not ppsf_values:
        return None

    mid = len(ppsf_values) // 2
    median_ppsf = (
        (ppsf_values[mid - 1] + ppsf_values[mid]) / 2
        if len(ppsf_values) % 2 == 0
        else ppsf_values[mid]
    )

    return int(median_ppsf * subject_sqft)


def check_arv_conflict(zestimate_arv: int | None, comp_arv: int | None) -> bool:
    """Return True if the two ARV methods diverge by more than 15%."""
    if not zestimate_arv or not comp_arv:
        return False
    divergence = abs(zestimate_arv - comp_arv) / max(zestimate_arv, comp_arv)
    return divergence > ARV_CONFLICT_THRESHOLD
