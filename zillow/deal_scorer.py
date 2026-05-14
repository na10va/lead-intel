from __future__ import annotations
"""
zillow/deal_scorer.py — Scores Zillow listings against the 75% ARV rule.

Labels:
    ≤ 65% of comp ARV → "Deep Value"   (call today)
    66–70%            → "On Target"    (call today)
    71–75%            → "Worth a Look" (call this week)
    > 75%             → do not surface

Sort output ascending by % of ARV (best deals first).
"""

from zillow.arv_calculator import check_arv_conflict
from utils.logger import get_logger

log = get_logger("zillow.deal_scorer")

SURFACE_THRESHOLD = 0.75    # surface listings at or below 75% of comp ARV
DEEP_VALUE_MAX = 0.65
ON_TARGET_MAX = 0.70


def score_listing(listing: dict) -> dict | None:
    """Score a single listing. Returns enriched listing dict or None if > 75% ARV.

    Args:
        listing: A dict with at minimum list_price, comp_arv, zestimate_arv, county.

    Returns the listing dict with pct_of_comp_arv, label, arv_conflict added.
    Returns None if list_price > 75% of comp_arv (do not surface).
    """
    list_price = listing.get("list_price")
    comp_arv = listing.get("comp_arv")
    zestimate_arv = listing.get("zestimate_arv")

    if not list_price or not comp_arv:
        log.debug(f"Missing list_price or comp_arv for {listing.get('address')} — skipping")
        return None

    pct = list_price / comp_arv
    if pct > SURFACE_THRESHOLD:
        return None

    label = _assign_label(pct)
    arv_conflict = check_arv_conflict(zestimate_arv, comp_arv)

    return {
        **listing,
        "pct_of_comp_arv": round(pct * 100, 1),
        "label": label,
        "arv_conflict": arv_conflict,
        "alerted_owner": False,
    }


def _assign_label(pct: float) -> str:
    if pct <= DEEP_VALUE_MAX:
        return "Deep Value"
    elif pct <= ON_TARGET_MAX:
        return "On Target"
    else:
        return "Worth a Look"


def sort_deals(deals: list[dict]) -> list[dict]:
    """Sort deals ascending by pct_of_comp_arv (best deals first)."""
    return sorted(deals, key=lambda d: d.get("pct_of_comp_arv", 999))
