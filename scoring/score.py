"""
scoring/score.py — Two-axis lead scoring model.

Axis 1: Distress Score (0–50) — how motivated is the seller?
Axis 2: Deal Score (0–50)     — how good are the economics?
Combined: 0–100, determines tier A/B/C/D and routing action.

Tier thresholds:
    A: 70–100  → SMS + email to owner, route to VA same day
    B: 45–69   → Email to owner, route to VA within 48 hours
    C: 20–44   → Store only, weekly review
    D: 0–19    → Store only, never routed

Usage:
    from scoring.score import score_lead, assign_tier

    result = score_lead(lead)
    # result = {"distress_score": 28, "deal_score": 35, "score": 63, "tier": "B"}

CLI (scores all unscored leads in Supabase):
    python scoring/score.py
"""

import argparse
import re
from collections import defaultdict
from datetime import date, timedelta

from db.client import get_client, update_row
from utils.logger import get_logger

log = get_logger("scoring")


def _normalize_addr_key(address: str, county: str = "") -> str:
    """Return a 'COUNTY:STREETNUM FIRST_STREETWORD' key for cross-source address stacking.

    Handles directional prefixes (E, W, N, S) and numbered streets (5TH, 112TH).
    Scoped to county to prevent false matches across counties with the same street.
    Examples:
        "3856 E 112TH ST"    → "CUYAHOGA:3856 E 112TH"
        "33915 REDBRIDGE LN" → "CUYAHOGA:33915 REDBRIDGE"
        "1650 5th Avenue"    → "MAHONING:1650 5TH"
    """
    if not address:
        return ""
    addr = address.upper().strip()
    m = re.match(r"(\d+)\s+((?:[NSEW]\s+)?[\dA-Z]+)", addr)
    if not m:
        return ""
    key = f"{m.group(1)} {m.group(2).strip()}"
    return f"{county.upper()}:{key}" if county else key

# ---------------------------------------------------------------------------
# Tier thresholds
# ---------------------------------------------------------------------------
TIER_A_MIN = 40
TIER_B_MIN = 35
TIER_C_MIN = 20


# ---------------------------------------------------------------------------
# Axis 1 — Distress Score (0–50)
# ---------------------------------------------------------------------------

def calc_distress_score(lead: dict) -> int:
    """Calculate the distress axis score (0–50) for a lead.

    Scores based on source type, stacked signals, recency, and Tier D bonuses.
    Tier D signals (divorce, eviction, bankruptcy) cap at 12 points and never
    trigger routing on their own — they only add value when stacked.
    """
    score = 0
    source_type = lead.get("source_type", "")
    filing_date = lead.get("filing_date")
    stacked_sources = lead.get("stacked_sources", [])  # list of source_types from deduper.get_stacked_signals()

    # Primary signal points
    primary_points = {
        "probate": 15,
        "foreclosure": 10,
        "code_violation": 10,
        "tax_lien": 8,
    }
    if source_type in primary_points:
        score += primary_points[source_type]

    # Vacancy and out-of-state flags
    if lead.get("vacant_flag"):
        score += 5
    if lead.get("owner_out_of_state"):
        score += 5

    # Stacked signal bonus — 2+ primary signals on the same property
    primary_sources = {"probate", "foreclosure", "code_violation", "tax_lien"}
    stacked_primary = [s for s in stacked_sources if s in primary_sources and s != source_type]
    if stacked_primary:
        score += 5  # multiple signals stacked bonus

    # Freshness bonus — filed within last 30 days
    if filing_date:
        if isinstance(filing_date, str):
            try:
                filing_date = date.fromisoformat(filing_date)
            except ValueError:
                filing_date = None
        if filing_date and filing_date >= date.today() - timedelta(days=30):
            score += 3

    # Tier D stack bonuses — only add value when a primary signal is already present
    has_primary = source_type in primary_sources or bool(stacked_primary)
    if has_primary:
        tier_d_bonus = 0
        if "divorce" in stacked_sources:
            tier_d_bonus += 4
        if "eviction" in stacked_sources:
            tier_d_bonus += 4
        if "bankruptcy" in stacked_sources:
            tier_d_bonus += 4
        # Tier D signals cap at 12 bonus points total
        score += min(tier_d_bonus, 12)

    return min(score, 50)


# ---------------------------------------------------------------------------
# Axis 2 — Deal Score (0–50)
# ---------------------------------------------------------------------------

def calc_deal_score(lead: dict) -> int:
    """Calculate the deal economics axis score (0–50) for a lead."""
    score = 0

    equity_pct = lead.get("estimated_equity_pct")
    equity_unknown = lead.get("equity_unknown", False)
    estimated_value = lead.get("estimated_value")
    last_sale_date = lead.get("last_sale_date")

    # Equity score
    if equity_unknown or equity_pct is None:
        score += 8  # neutral — do not penalize missing data
    elif equity_pct > 50:
        score += 25
    elif equity_pct >= 30:
        score += 20
    elif equity_pct >= 15:
        score += 12
    else:
        score += 5  # equity < 15%

    # Last sale age
    if last_sale_date:
        if isinstance(last_sale_date, str):
            try:
                last_sale_date = date.fromisoformat(last_sale_date)
            except ValueError:
                last_sale_date = None
        if last_sale_date:
            years_held = (date.today() - last_sale_date).days / 365.25
            if years_held > 10:
                score += 10
            elif years_held >= 5:
                score += 6

    # Property value — Ohio sweet spot
    if estimated_value:
        if 75_000 <= estimated_value <= 300_000:
            score += 10
        elif 300_000 < estimated_value <= 500_000:
            score += 5
        # < $75K or > $500K: +0

    return min(score, 50)


# ---------------------------------------------------------------------------
# Combined scoring and tier assignment
# ---------------------------------------------------------------------------

def assign_tier(combined_score: int) -> str:
    """Return tier string A/B/C/D based on combined score."""
    if combined_score >= TIER_A_MIN:
        return "A"
    elif combined_score >= TIER_B_MIN:
        return "B"
    elif combined_score >= TIER_C_MIN:
        return "C"
    else:
        return "D"


_TIER_D_SOURCES = {"divorce", "eviction", "bankruptcy", "fsbo"}


def score_lead(lead: dict) -> dict:
    """Score a single lead dict and return scoring results.

    Args:
        lead: A raw_leads row dict. Must include at minimum:
              source_type, filing_date, estimated_equity_pct,
              estimated_value, last_sale_date.

    Returns a dict with keys: distress_score, deal_score, score, tier.
    """
    distress = calc_distress_score(lead)
    deal = calc_deal_score(lead)
    combined = distress + deal
    tier = assign_tier(combined)

    # Tier D source types never route on their own — cap at Tier C even if
    # deal economics alone push the score above the Tier A/B threshold.
    if lead.get("source_type") in _TIER_D_SOURCES and distress == 0:
        if tier in ("A", "B"):
            tier = "C"

    return {
        "distress_score": distress,
        "deal_score": deal,
        "score": combined,
        "tier": tier,
    }


# ---------------------------------------------------------------------------
# Batch scoring — runs on all unscored leads in Supabase
# ---------------------------------------------------------------------------

def _build_stacking_maps(client) -> tuple[dict, dict]:
    """Fetch all leads and build parcel_id and address stacking maps.

    Returns:
        parcel_map: {parcel_id: set(source_types)}
        addr_map:   {county:streetnum_streetword: set(source_types)}
    """
    parcel_map: dict = defaultdict(set)
    addr_map: dict   = defaultdict(set)
    offset = 0
    while True:
        batch = (
            client.table("raw_leads")
            .select("parcel_id,property_address,county,source_type")
            .range(offset, offset + 999)
            .execute()
            .data or []
        )
        if not batch:
            break
        for l in batch:
            src = l.get("source_type") or ""
            if not src:
                continue
            p = (l.get("parcel_id") or "").strip()
            if p:
                parcel_map[p].add(src)
            ak = _normalize_addr_key(l.get("property_address", ""), l.get("county", ""))
            if ak:
                addr_map[ak].add(src)
        offset += 1000
        if len(batch) < 1000:
            break
    return dict(parcel_map), dict(addr_map)


def run_batch_scoring(rescore: bool = False) -> None:
    """Score verified leads in raw_leads, resolving cross-source stacking first.

    Args:
        rescore: If True, re-score all verified leads including already-scored ones.
                 If False (default), only score leads where score IS NULL.

    Cross-source stacking: before scoring each lead, look up whether the same
    property appears under a different source_type (matched by parcel_id or
    normalized address). Stacked signals are passed to score_lead() via the
    stacked_sources field so the multi-signal bonus applies correctly.
    """
    client = get_client()

    log.info("Building cross-source stacking maps...")
    parcel_map, addr_map = _build_stacking_maps(client)
    log.info(f"Stacking maps ready: {len(parcel_map)} parcel keys, {len(addr_map)} address keys")

    all_leads = []
    offset = 0
    while True:
        q = client.table("raw_leads").select("*").eq("verified_raw", True)
        if not rescore:
            q = q.is_("score", "null")
        batch = q.range(offset, offset + 999).execute().data or []
        if not batch:
            break
        all_leads.extend(batch)
        offset += 1000
        if len(batch) < 1000:
            break

    log.info(f"Found {len(all_leads)} leads to score (rescore={rescore})")

    scored = 0
    tier_changes = 0
    for lead in all_leads:
        try:
            src = lead.get("source_type") or ""
            stacked: set = set()

            p = (lead.get("parcel_id") or "").strip()
            if p and p in parcel_map:
                stacked |= parcel_map[p] - {src}

            ak = _normalize_addr_key(lead.get("property_address", ""), lead.get("county", ""))
            if ak and ak in addr_map:
                stacked |= addr_map[ak] - {src}

            lead["stacked_sources"] = list(stacked)

            old_tier = lead.get("tier")
            result = score_lead(lead)
            update_row("raw_leads", lead["id"], {**result, "scored_at": "now()"})
            scored += 1

            if rescore and old_tier != result["tier"]:
                tier_changes += 1
                log.info(
                    f"Tier change {lead['id'][:8]}: {old_tier} → {result['tier']} "
                    f"score={result['score']} stacked={list(stacked)}"
                )
        except Exception as e:
            log.error(f"Failed to score lead {lead.get('id')}: {e}")

    log.info(f"Scoring complete — {scored}/{len(all_leads)} scored  tier_changes={tier_changes}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score all unscored leads")
    parser.add_argument("--dry-run", action="store_true", help="Score without writing to Supabase")
    parser.add_argument("--rescore", action="store_true", help="Re-score all leads, including already-scored ones")
    args = parser.parse_args()

    if args.dry_run:
        log.info("Dry run mode — no writes to Supabase")
    else:
        run_batch_scoring(rescore=args.rescore)
