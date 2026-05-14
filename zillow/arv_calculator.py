from __future__ import annotations
"""
zillow/arv_calculator.py — Calculates ARV two ways for each Zillow listing.

Method 1 — Zestimate ARV: pull Zillow's Zestimate for the property (fast reference).
Method 2 — Comp-Based ARV: median $/sqft of last 90 days of sold comps × subject sqft.
    Comp criteria: same zip, beds ±1, baths ±1, sqft ±20%, same property type.

If the two methods diverge by more than 15%, set arv_conflict=True.
"""

from utils.logger import get_logger

log = get_logger("zillow.arv_calculator")

COMP_LOOKBACK_DAYS = 90
ARV_CONFLICT_THRESHOLD = 0.15  # 15%


def calc_zestimate_arv(listing: dict) -> int | None:
    """Return the Zestimate for the subject property.

    TODO: Extract Zestimate from the scraped listing data.
    Zillow includes Zestimate in the listing JSON for most properties.
    Returns None if not available.
    """
    zestimate = listing.get("zestimate")
    if zestimate and isinstance(zestimate, (int, float)):
        return int(zestimate)
    return None


def fetch_sold_comps(listing: dict) -> list[dict]:
    """Fetch sold comparables from Zillow for the subject property.

    Criteria:
        - Same zip code
        - Sold within last 90 days
        - Beds within ±1 of subject
        - Baths within ±1 of subject
        - Sqft within ±20% of subject
        - Same property type

    TODO: Implement Playwright or API fetch of Zillow sold comps.
    """
    raise NotImplementedError("fetch_sold_comps not yet implemented")


def calc_comp_arv(listing: dict) -> int | None:
    """Calculate comp-based ARV using median $/sqft of recent sold comps.

    Returns the comp ARV in USD, or None if insufficient comp data.
    """
    subject_sqft = listing.get("sqft")
    if not subject_sqft or subject_sqft <= 0:
        log.warning(f"No sqft for {listing.get('address')} — cannot calc comp ARV")
        return None

    try:
        comps = fetch_sold_comps(listing)
    except NotImplementedError:
        log.debug("fetch_sold_comps not yet implemented — skipping comp ARV")
        return None
    except Exception as e:
        log.error(f"Error fetching comps for {listing.get('address')}: {e}")
        return None

    if len(comps) < 3:
        log.warning(f"Insufficient comps ({len(comps)}) for {listing.get('address')} — comp ARV unreliable")
        return None

    price_per_sqft_values = []
    for comp in comps:
        comp_price = comp.get("sale_price")
        comp_sqft = comp.get("sqft")
        if comp_price and comp_sqft and comp_sqft > 0:
            price_per_sqft_values.append(comp_price / comp_sqft)

    if not price_per_sqft_values:
        return None

    price_per_sqft_values.sort()
    mid = len(price_per_sqft_values) // 2
    if len(price_per_sqft_values) % 2 == 0:
        median_ppsf = (price_per_sqft_values[mid - 1] + price_per_sqft_values[mid]) / 2
    else:
        median_ppsf = price_per_sqft_values[mid]

    return int(median_ppsf * subject_sqft)


def check_arv_conflict(zestimate_arv: int | None, comp_arv: int | None) -> bool:
    """Return True if the two ARV methods diverge by more than 15%."""
    if not zestimate_arv or not comp_arv:
        return False
    divergence = abs(zestimate_arv - comp_arv) / max(zestimate_arv, comp_arv)
    return divergence > ARV_CONFLICT_THRESHOLD
