"""
enrichment/cleanup_limbo.py — One-time cleanup for two limbo categories.

Task 1 — 2,994 pending Cuyahoga records:
  These have estimated_value=NULL and verification_notes containing prior
  enrichment notes (e.g. "Enrichment: no mobile found") but no
  auditor_attempted=true marker. The auditor batch tried them but the
  mark-failed DB writes timed out. This script re-attempts the GIS lookup
  and, if still not found, writes the marker so they're excluded from
  future batch runs.

Task 2 — 2,420 unscored records (tier IS NULL):
  All have parcel IDs in non-standard Cuyahoga ranges (78x/79x/68x) or
  Lake County special-use parcel series (5550/9990), confirmed absent from
  the public GIS. Addresses are either null or city-only (no street number).
  For each: attempt GIS reverse-lookup by parcel → if a real address and
  value come back, write them and score; otherwise set tier='D' and append
  'no_address' to verification_notes so they're routed correctly.

CLI:
    python enrichment/cleanup_limbo.py --task1
    python enrichment/cleanup_limbo.py --task2
    python enrichment/cleanup_limbo.py --all
"""

import argparse
import re
import time
from typing import Optional

import requests
from dotenv import load_dotenv

from db.client import get_client, update_row
from enrichment.county_auditor import (
    CUYAHOGA_GIS_URL, CUYAHOGA_OUT_FIELDS,
    LAKE_GIS_URL, LAKE_OUT_FIELDS,
    _unix_ms_to_date, _CUYAHOGA_PARCEL_RE,
)
from scoring.score import score_lead
from utils.logger import get_logger

load_dotenv()
log = get_logger("enrichment.cleanup_limbo")

REQUEST_DELAY_S = 0.5
PAGE_SIZE       = 200


# ---------------------------------------------------------------------------
# GIS helpers (mirrors county_auditor.py but returns address too)
# ---------------------------------------------------------------------------

def _cuyahoga_gis_lookup(pin8: str) -> Optional[dict]:
    """Query Cuyahoga GIS by 8-digit pin. Returns dict with address+value or None."""
    try:
        resp = requests.get(
            CUYAHOGA_GIS_URL,
            params={"where": f"parcelpin='{pin8}'",
                    "outFields": CUYAHOGA_OUT_FIELDS + ",par_addr_all",
                    "f": "json"},
            timeout=10,
        )
        resp.raise_for_status()
        feats = resp.json().get("features") or []
    except Exception as e:
        log.warning(f"Cuyahoga GIS request failed for pin {pin8}: {e}")
        return None

    if not feats:
        return None

    a = feats[0]["attributes"]
    assessed = a.get("certified_tax_total")
    if not assessed:
        return None

    return {
        "address":         a.get("par_addr_all") or "",
        "estimated_value": int(float(assessed) / 0.35),
        "last_sale_date":  str(_unix_ms_to_date(a.get("transfer_date"))) if a.get("transfer_date") else None,
        "last_sale_price": int(float(a["sales_amount"])) if a.get("sales_amount") else None,
    }


def _lake_gis_lookup(pin_nodash: str) -> Optional[dict]:
    """Query Lake County GIS by PIN_NODASH. Returns dict with address+value or None."""
    try:
        resp = requests.get(
            LAKE_GIS_URL,
            params={"where":     f"PIN_NODASH='{pin_nodash}'",
                    "outFields": LAKE_OUT_FIELDS + ",G_FULLADDRESS",
                    "f":         "json"},
            timeout=10,
        )
        resp.raise_for_status()
        feats = resp.json().get("features") or []
    except Exception as e:
        log.warning(f"Lake GIS request failed for pin {pin_nodash}: {e}")
        return None

    if not feats:
        return None

    a = feats[0]["attributes"]
    market_value = a.get("A_VAL_TOTAL")
    if not market_value:
        return None

    return {
        "address":         a.get("G_FULLADDRESS") or "",
        "estimated_value": int(float(market_value)),
        "last_sale_date":  str(_unix_ms_to_date(a.get("A_SALE_DATE"))) if a.get("A_SALE_DATE") else None,
        "last_sale_price": int(float(a["A_SALE_AMOUNT"])) if a.get("A_SALE_AMOUNT") else None,
    }


def _mark_auditor_attempted(lead_id: str, existing_notes: str) -> None:
    note = (existing_notes or "") + " | auditor_attempted=true"
    for attempt in range(3):
        try:
            update_row("raw_leads", lead_id, {"verification_notes": note.strip(" | ")})
            return
        except Exception as e:
            if attempt == 2:
                log.error(f"Failed to mark auditor_attempted for {lead_id} after 3 tries: {e}")
            else:
                time.sleep(2)


# ---------------------------------------------------------------------------
# Task 1 — Clear 2,994 pending Cuyahoga records
# ---------------------------------------------------------------------------

def run_task1() -> None:
    """Re-attempt GIS lookup for Cuyahoga records stuck without auditor_attempted marker.

    Always queries from offset 0. After each record is either enriched or marked,
    it drops out of the filter. Stops when the filter returns 0 results.
    """
    log.info("Task 1: clearing 2,994 pending Cuyahoga records")
    enriched = 0
    marked   = 0

    while True:
        client = get_client()
        leads = (
            client.table("raw_leads")
            .select("id, parcel_id, property_address, geocoded_address, verification_notes, score, tier")
            .eq("county", "Cuyahoga")
            .is_("estimated_value", "null")
            .or_("verification_notes.is.null,verification_notes.not.like.*auditor_attempted=true*")
            .limit(PAGE_SIZE)
            .execute()
            .data or []
        )
        if not leads:
            log.info("Task 1: no more pending records — done")
            break

        log.info(f"Task 1: processing batch of {len(leads)}")
        page_enriched = 0

        for lead in leads:
            parcel_id = (lead.get("parcel_id") or "").strip()
            notes     = lead.get("verification_notes") or ""
            result    = None

            if _CUYAHOGA_PARCEL_RE.match(parcel_id):
                pin8   = re.sub(r"[^0-9]", "", parcel_id)
                result = _cuyahoga_gis_lookup(pin8)

            if result:
                result["equity_unknown"] = True
                try:
                    update_row("raw_leads", lead["id"], result)
                    # Re-score
                    full = client.table("raw_leads").select("*").eq("id", lead["id"]).single().execute().data
                    if full:
                        scored = score_lead(full)
                        update_row("raw_leads", lead["id"], {**scored, "scored_at": "now()"})
                    enriched     += 1
                    page_enriched += 1
                    log.info(f"Task 1: enriched {lead['id'][:8]} parcel={parcel_id}")
                except Exception as e:
                    log.error(f"Task 1: DB write failed for {lead['id']}: {e}")
            else:
                _mark_auditor_attempted(lead["id"], notes)
                marked += 1

            time.sleep(REQUEST_DELAY_S)

        # No break on all-failures: every failure was just marked, so the next
        # query will naturally return fewer records. Loop until the query is empty.

    log.info(f"Task 1 complete — enriched={enriched}  marked={marked}")


# ---------------------------------------------------------------------------
# Task 2 — Fix 2,420 unscored records (tier IS NULL)
# ---------------------------------------------------------------------------

def run_task2() -> None:
    """Reverse-lookup addresses for unscored leads, score them, or assign tier=D.

    For each lead with tier=NULL:
      - Cuyahoga: try GIS by parcel pin → address + value
      - Lake:     try GIS by PIN_NODASH → address + value
      - If GIS returns address+value: write fields and run score_lead()
      - If GIS returns nothing: set tier='D', append 'no_address' to notes
    """
    log.info("Task 2: resolving unscored records")
    resolved  = 0
    scored_d  = 0
    forced_d  = 0

    while True:
        client = get_client()
        # Always query from offset 0: each record processed gets tier set (non-null)
        # and drops out of this filter naturally.
        leads = (
            client.table("raw_leads")
            .select("id, parcel_id, property_address, geocoded_address, county, source_type, "
                    "filing_date, score, tier, verification_notes, verified_raw, estimated_value")
            .is_("tier", "null")
            .limit(PAGE_SIZE)
            .execute()
            .data or []
        )
        if not leads:
            break

        log.info(f"Task 2: processing batch of {len(leads)} unscored leads")

        for lead in leads:
            county    = (lead.get("county") or "").lower()
            parcel_id = (lead.get("parcel_id") or "").strip()
            notes     = lead.get("verification_notes") or ""
            result    = None

            # --- Attempt GIS reverse-lookup ---
            if county == "cuyahoga" and _CUYAHOGA_PARCEL_RE.match(parcel_id):
                pin8   = re.sub(r"[^0-9]", "", parcel_id)
                result = _cuyahoga_gis_lookup(pin8)

            elif county == "lake" and parcel_id:
                pin_nodash = re.sub(r"[^A-Za-z0-9]", "", parcel_id)
                result     = _lake_gis_lookup(pin_nodash)

            if result and result.get("estimated_value"):
                # We got value data (and possibly an address)
                updates = {
                    "estimated_value": result["estimated_value"],
                    "last_sale_date":  result.get("last_sale_date"),
                    "last_sale_price": result.get("last_sale_price"),
                    "equity_unknown":  True,
                }
                # If GIS returned a real street address, write it back
                gis_addr = (result.get("address") or "").strip()
                if gis_addr and re.match(r"^\d+\s+\S+", gis_addr):
                    updates["property_address"] = gis_addr
                    updates["geocoded_address"]  = gis_addr

                try:
                    update_row("raw_leads", lead["id"], updates)
                    full = client.table("raw_leads").select("*").eq("id", lead["id"]).single().execute().data
                    if full:
                        scored = score_lead(full)
                        update_row("raw_leads", lead["id"], {**scored, "scored_at": "now()"})
                        log.info(f"Task 2: resolved {lead['id'][:8]} county={county} → "
                                 f"value=${result['estimated_value']:,}  tier={scored.get('tier')}")
                    resolved += 1
                    if scored.get("tier") == "D":
                        scored_d += 1
                except Exception as e:
                    log.error(f"Task 2: DB write failed for {lead['id']}: {e}")

            else:
                # GIS lookup failed — force tier=D
                new_notes = (notes + " | no_address").strip(" | ") if "no_address" not in notes else notes
                try:
                    # Score with what we have (deals axis will get equity_unknown=+8)
                    full = client.table("raw_leads").select("*").eq("id", lead["id"]).single().execute().data
                    if full:
                        scored = score_lead(full)
                        update_row("raw_leads", lead["id"], {
                            **scored,
                            "scored_at":         "now()",
                            "verification_notes": new_notes,
                        })
                        log.info(f"Task 2: forced D for {lead['id'][:8]} county={county} "
                                 f"parcel={parcel_id}  score={scored.get('score')}")
                    forced_d += 1
                except Exception as e:
                    log.error(f"Task 2: failed to force tier=D for {lead['id']}: {e}")

            time.sleep(REQUEST_DELAY_S)

    log.info(f"Task 2 complete — resolved={resolved} (scored_D={scored_d})  forced_D={forced_d}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cleanup limbo leads")
    parser.add_argument("--task1", action="store_true", help="Clear 2,994 pending Cuyahoga auditor records")
    parser.add_argument("--task2", action="store_true", help="Resolve 2,420 unscored (tier=NULL) records")
    parser.add_argument("--all",   action="store_true", help="Run both tasks")
    args = parser.parse_args()

    if args.all or args.task1:
        run_task1()
    if args.all or args.task2:
        run_task2()
    if not (args.all or args.task1 or args.task2):
        parser.print_help()
