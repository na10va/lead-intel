"""
scoring/tier_d_stacker.py — Nightly Tier D stacking re-score pass.

Problem this solves:
    When a Tier D lead (divorce, eviction, bankruptcy) is ingested for an address
    that already has a primary lead (probate, foreclosure, code_violation, tax_lien),
    the primary lead was scored without the +4 Tier D stacking bonus. This job runs
    after the main pipeline and retroactively applies that bonus by re-scoring primary
    leads whose address now has Tier D signals they didn't have at scoring time.

How it works:
    For each Tier D source type:
    1. Pull all Tier D leads with a property address from Supabase.
    2. For each, find primary leads at the same county + normalized address.
    3. Re-score each primary lead with the full current stacked_sources context.
    4. If the score changed, update raw_leads and route if tier improved to A or B.

Structured as four source-type functions (divorce, eviction, bankruptcy, fsbo) so
each can be tracked and logged independently. All four are called from run_all().

FSBO note: FSBO leads are never enriched and never have property addresses in
raw_leads (they go to the Zillow Deal Finder). The fsbo stacker is a no-op stub
kept here for completeness — it logs and returns immediately.

CLI:
    python scoring/tier_d_stacker.py
    python scoring/tier_d_stacker.py --source divorce
    python scoring/tier_d_stacker.py --dry-run
"""

from __future__ import annotations

import argparse
from typing import Optional

from db.client import get_client, update_row
from routing.va_router import route_lead
from scoring.score import score_lead
from utils.deduper import _normalize_address, get_stacked_signals
from utils.logger import get_logger

log = get_logger("tier_d_stacker")

TIER_D_SOURCES = ["divorce", "eviction", "bankruptcy", "fsbo"]
PRIMARY_SOURCES = ["probate", "foreclosure", "code_violation", "tax_lien"]

# Use a 365-day lookback for stacking — Tier D signals accumulate over time
# and should stack even when the primary lead is months old.
STACKING_LOOKBACK_DAYS = 365


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _get_primary_leads_for_address(address: str, county: str) -> list[dict]:
    """Return all primary leads at this address in this county, excluding Tier A.

    Tier A leads are already at maximum routing — re-scoring them is wasted work.
    """
    client = get_client()
    response = (
        client.table("raw_leads")
        .select("*")
        .eq("county", county)
        .in_("source_type", PRIMARY_SOURCES)
        .neq("tier", "A")
        .not_.is_("property_address", "null")
        .execute()
    )

    norm_target = _normalize_address(address)
    return [
        row for row in (response.data or [])
        if _normalize_address(row.get("property_address") or "") == norm_target
    ]


def _rescore_with_stack(lead: dict) -> Optional[dict]:
    """Re-score a primary lead with current stacked signals.

    Returns the new scoring result dict if the score changed, None if unchanged.
    """
    address = lead.get("property_address")
    county = lead.get("county")
    if not address or not county:
        return None

    stacked_sources = get_stacked_signals(address, county, days=STACKING_LOOKBACK_DAYS)
    result = score_lead({**lead, "stacked_sources": stacked_sources})

    old_score = lead.get("score") or 0
    old_tier = lead.get("tier", "D")

    if result["score"] != old_score or result["tier"] != old_tier:
        return result
    return None


# ---------------------------------------------------------------------------
# Per-source stacker functions
# ---------------------------------------------------------------------------

def check_divorce_stacking(dry_run: bool = False) -> int:
    """Re-score primary leads that now have a divorce signal stacked at their address."""
    return _run_stacker("divorce", dry_run)


def check_eviction_stacking(dry_run: bool = False) -> int:
    """Re-score primary leads that now have an eviction signal stacked at their address."""
    return _run_stacker("eviction", dry_run)


def check_bankruptcy_stacking(dry_run: bool = False) -> int:
    """Re-score primary leads that now have a bankruptcy signal stacked at their address."""
    return _run_stacker("bankruptcy", dry_run)


def check_fsbo_stacking(dry_run: bool = False) -> int:
    """FSBO leads flow to the Zillow Deal Finder, not raw_leads — no stacking to apply."""
    log.info("FSBO stacker: FSBO leads use Zillow pipeline, no raw_leads addresses to stack")
    return 0


def _run_stacker(tier_d_source: str, dry_run: bool = False) -> int:
    """Core logic: pull Tier D leads of this source type, find stacked primary leads,
    re-score and upgrade.

    Returns count of primary leads whose score changed.
    """
    client = get_client()

    response = (
        client.table("raw_leads")
        .select("property_address, county")
        .eq("source_type", tier_d_source)
        .not_.is_("property_address", "null")
        .execute()
    )

    tier_d_leads = response.data or []
    if not tier_d_leads:
        log.info(f"{tier_d_source} stacker: no leads with addresses — skipping")
        return 0

    log.info(f"{tier_d_source} stacker: checking {len(tier_d_leads)} {tier_d_source} leads for primary stacks")

    upgraded = 0
    checked_primary_ids: set[str] = set()

    for td in tier_d_leads:
        address = td.get("property_address")
        county = td.get("county")
        if not address:
            continue

        primary_leads = _get_primary_leads_for_address(address, county)

        for primary in primary_leads:
            lead_id = primary.get("id")
            if not lead_id or lead_id in checked_primary_ids:
                continue
            checked_primary_ids.add(lead_id)

            new_result = _rescore_with_stack(primary)
            if not new_result:
                continue

            old_tier = primary.get("tier", "D")
            new_tier = new_result["tier"]

            log.info(
                f"[{tier_d_source} stack] {primary.get('owner_name')} | {address} | "
                f"{county} | {old_tier} → {new_tier} | "
                f"score {primary.get('score', '?')} → {new_result['score']}"
                + (" [DRY RUN]" if dry_run else "")
            )

            if not dry_run:
                update_row("raw_leads", lead_id, {**new_result, "scored_at": "now()"})
                upgraded += 1

                # Route if tier improved to A or B for the first time
                if new_tier in ("A", "B") and old_tier not in ("A", "B"):
                    route_lead(lead_id, new_tier)
            else:
                upgraded += 1  # count even in dry run for reporting

    log.info(f"{tier_d_source} stacker: {upgraded} primary leads updated")
    return upgraded


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_all(dry_run: bool = False) -> None:
    """Run all four Tier D stacking checks in order."""
    log.info(f"Tier D stacker starting {'(DRY RUN) ' if dry_run else ''}")
    total = 0
    for fn in [check_divorce_stacking, check_eviction_stacking,
               check_bankruptcy_stacking, check_fsbo_stacking]:
        try:
            total += fn(dry_run=dry_run)
        except Exception as e:
            log.error(f"Stacker failed for {fn.__name__}: {e}")
    log.info(f"Tier D stacker complete — {total} total primary leads updated")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tier D stacking re-score pass")
    parser.add_argument(
        "--source",
        choices=TIER_D_SOURCES,
        help="Run stacker for one specific source type only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would change without writing to Supabase",
    )
    args = parser.parse_args()

    _fn_map = {
        "divorce": check_divorce_stacking,
        "eviction": check_eviction_stacking,
        "bankruptcy": check_bankruptcy_stacking,
        "fsbo": check_fsbo_stacking,
    }

    if args.source:
        _fn_map[args.source](dry_run=args.dry_run)
    else:
        run_all(dry_run=args.dry_run)
