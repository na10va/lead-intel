from __future__ import annotations
"""
agents/code_violation_agent.py — Scrapes building/housing code violation records.

Cities supported:
    Cleveland, OH (Cuyahoga County):
        Source:  City of Cleveland Civil Tickets (Building & Housing Dept, Accela-sourced)
        API:     ArcGIS REST — public, no authentication required
        URL:     services3.arcgis.com/dty2kHktVXHrqO8i/.../Civil_Tickets/FeatureServer/0/query
        Fields:  TICKET_ID, FILE_DATE, ISSUE_DATE, TICKET_STATUS, ADDRESS,
                 PARCEL_NUMBER, TICKET_CITATIONS, ADDITIONAL_CITATION_DETAILS, DW_Neighborhood
        Cadence: Weekly updates (Accela system push)
        Notes:   Owner name is NOT present in source — enrichment looks it up via
                 the Cuyahoga County Auditor using PARCEL_NUMBER.

To add a new city: implement a _fetch_<city>() async function and register it
in CITY_SCRAPERS. The 9-step pipeline is shared.

CLI:
    python agents/code_violation_agent.py --city cleveland --county cuyahoga --state OH
    python agents/code_violation_agent.py --city cleveland --county cuyahoga --state OH --days 14
"""

import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv

from db.client import get_client, insert_row, update_row
from enrichment.waterfall import enrich_lead
from routing.va_router import route_lead
from scoring.score import score_lead
from utils.deduper import is_duplicate
from utils.logger import get_logger
from verification.verify_leads import verify_raw_record

load_dotenv()

log = get_logger("code_violation_agent")

DEFAULT_LOOKBACK_DAYS = 7  # matches the weekly source update cadence

# =============================================================================
# Cleveland, OH — ArcGIS REST API (City of Cleveland Civil Tickets)
#
# Dataset: Civil enforcement citations issued by the Dept of Building & Housing
# under the "Residents First" 2024 legislative reforms. Sourced from Accela.
# Confirmed live and public 2026-04-18 — no auth, no rate limiting observed.
# ~13,000 records from Jan 2025 onward; ~200–400 new records per week.
#
# Endpoint: services3.arcgis.com/dty2kHktVXHrqO8i/arcgis/rest/services/
#           Civil_Tickets/FeatureServer/0/query
# Pagination: resultOffset / resultRecordCount (ArcGIS standard)
# Date filter: FILE_DATE >= <epoch_ms>  (FILE_DATE is a Date field in ArcGIS)
# =============================================================================

_CLEVELAND_ARCGIS_URL = (
    "https://services3.arcgis.com/dty2kHktVXHrqO8i/arcgis/rest/services/"
    "Civil_Tickets/FeatureServer/0/query"
)
_ARCGIS_PAGE_SIZE = 1000  # ArcGIS REST default max records per request


async def _fetch_cleveland(since: date) -> list[dict]:
    """Pull Civil Ticket records from the Cleveland ArcGIS REST API.

    Paginates using resultOffset until all records in the lookback window
    are retrieved. Returns a list of raw ArcGIS attribute dicts.
    """
    # ArcGIS REST date filter syntax: TIMESTAMP 'YYYY-MM-DD HH:MM:SS'
    since_str = since.strftime("%Y-%m-%d 00:00:00")
    where_clause = f"FILE_DATE >= TIMESTAMP '{since_str}'"

    results: list[dict] = []
    offset = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            params = {
                "where": where_clause,
                "outFields": (
                    "TICKET_ID,FILE_DATE,ISSUE_DATE,TICKET_STATUS,"
                    "ADDRESS,PARCEL_NUMBER,TICKET_CITATIONS,"
                    "ADDITIONAL_CITATION_DETAILS,DW_Neighborhood"
                ),
                "resultRecordCount": _ARCGIS_PAGE_SIZE,
                "resultOffset": offset,
                "orderByFields": "FILE_DATE DESC",
                "f": "json",
            }

            try:
                resp = await client.get(_CLEVELAND_ARCGIS_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.error(f"Cleveland ArcGIS fetch failed (offset={offset}): {e}")
                break

            if "error" in data:
                log.error(f"Cleveland ArcGIS API error: {data['error']}")
                break

            features = data.get("features", [])
            if not features:
                break

            for feat in features:
                results.append(feat.get("attributes", {}))

            log.debug(f"Cleveland: fetched {len(features)} records at offset={offset}")

            # ArcGIS signals end-of-results when fewer records than page size returned
            if len(features) < _ARCGIS_PAGE_SIZE:
                break

            offset += _ARCGIS_PAGE_SIZE

    log.info(f"Cleveland: fetched {len(results)} civil ticket records since {since}")
    return results


# City scraper registry — register new city scrapers here as they are implemented
CITY_SCRAPERS = {
    "cleveland": _fetch_cleveland,
}


# =============================================================================
# Parse — map ArcGIS attributes to raw_leads schema
# =============================================================================

def _epoch_ms_to_iso(epoch_ms: Optional[int]) -> Optional[str]:
    """Convert ArcGIS millisecond epoch timestamp to ISO date string (YYYY-MM-DD)."""
    if epoch_ms is None:
        return None
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, OSError):
        return None


def parse_violation(attrs: dict, city: str, county: str, state: str) -> dict:
    """Map a raw ArcGIS Civil Tickets attribute record to the raw_leads table schema.

    Owner name is NOT present in the Civil Tickets source. Enrichment (Step 1)
    will look it up from the Cuyahoga County Auditor using PARCEL_NUMBER.
    """
    filing_date = _epoch_ms_to_iso(attrs.get("FILE_DATE"))
    raw_address = (attrs.get("ADDRESS") or "").strip()
    ticket_id = (attrs.get("TICKET_ID") or "").strip() or None

    # Use TICKET_ID as parcel_id — it is the unique Accela record identifier and
    # maps cleanly to the (parcel_id, county, source_type) unique constraint.
    # The actual county parcel number is preserved in raw_data.PARCEL_NUMBER and
    # used by enrichment/public_sources.py to look up the owner via county auditor.
    return {
        "owner_name": "",           # not in source — populated by enrichment
        "property_address": raw_address or None,
        "parcel_id": ticket_id,
        "filing_date": filing_date,
        "raw_data": attrs,
        "source_type": "code_violation",
        "source_name": f"{city.title()} Building & Housing (Civil Tickets)",
        "state": state,
        "county": county.title(),
        "verified_raw": False,
        "verified_enriched": False,
        "enriched": False,
    }


# =============================================================================
# Dedupe — TICKET_ID (Accela) is the idempotency key
# =============================================================================



# =============================================================================
# Main agent — full 9-step pipeline
# =============================================================================

async def _run_async(city: str, county: str, state: str, lookback_days: int) -> None:
    """Async inner loop — runs the full 9-step pipeline for one city."""
    scraper = CITY_SCRAPERS.get(city.lower())
    if not scraper:
        log.error(f"No scraper registered for city: {city}")
        return

    since = date.today() - timedelta(days=lookback_days)
    log.info(
        f"Code violation agent starting — {city.title()}, "
        f"{county.title()} County, {state} (since {since})"
    )

    # 1. FETCH
    try:
        raw_records = await scraper(since)
    except Exception as e:
        log.error(f"FETCH failed for {city}: {e}")
        from maintenance.self_healer import handle_failure
        handle_failure(f"code_violation_{city}", str(e))
        return

    if not raw_records:
        log.warning(
            f"No civil ticket records returned for {city} — "
            "check source URL or expand --days window"
        )
        from maintenance.self_healer import handle_failure
        handle_failure(f"code_violation_{city}", "Zero records returned")
        return

    log.info(f"Processing {len(raw_records)} raw records for {city}")
    new_records = 0
    tier_a_count = 0
    tier_b_count = 0

    for attrs in raw_records:
        try:
            ticket_id = (attrs.get("TICKET_ID") or "").strip()

            # 2. PARSE
            record = parse_violation(attrs, city, county, state)
            if not record.get("property_address"):
                log.debug(f"Skipping record with no address: TICKET_ID={ticket_id}")
                continue

            # 3. DEDUPE — parcel_id is set to TICKET_ID in parse_violation, so this
            # check maps directly to the (parcel_id, county, source_type) DB constraint.
            if is_duplicate(
                county=county.title(),
                source_type="code_violation",
                parcel_id=record.get("parcel_id"),
                property_address=record.get("property_address"),
                owner_name=None,
            ):
                log.debug(f"Duplicate TICKET_ID={ticket_id} — skipping")
                continue

            # 4. STORE
            stored = insert_row("raw_leads", record)
            lead_id = stored.get("id")
            if not lead_id:
                log.error(f"Insert returned no ID for TICKET_ID={ticket_id}")
                continue
            new_records += 1

            # 5. VERIFY (Gate 1 — post-scrape record validation)
            passed_gate1 = verify_raw_record(lead_id)
            if not passed_gate1:
                log.debug(f"Lead {lead_id} failed Gate 1 — skipping enrichment")
                continue

            # 6. ENRICH (waterfall: county auditor → Skip Sherpa → Skip Matrix flag)
            enrich_lead(lead_id)

            # 7. VERIFY (Gate 2 — post-enrichment field validation)
            refreshed = (
                get_client()
                .table("raw_leads")
                .select("*")
                .eq("id", lead_id)
                .single()
                .execute()
                .data
            )
            if not refreshed or not refreshed.get("verified_enriched"):
                log.debug(f"Lead {lead_id} failed Gate 2 — skipping scoring")
                continue

            # 8. SCORE
            result = score_lead(refreshed)
            update_row("raw_leads", lead_id, {
                **result,
                "scored_at": "now()",
            })
            log.info(
                f"Scored: {record.get('property_address')} | "
                f"distress={result['distress_score']} deal={result['deal_score']} "
                f"total={result['score']} tier={result['tier']}"
            )

            # 9. ROUTE
            if result["tier"] == "A":
                tier_a_count += 1
                route_lead(lead_id, result["tier"])
            elif result["tier"] == "B":
                tier_b_count += 1
                route_lead(lead_id, result["tier"])

        except Exception as e:
            log.error(f"Error processing record for {city}: {e}")
            continue

    log.info(
        f"Code violation agent complete — {city.title()}, {county.title()} County | "
        f"{new_records} new records stored | {tier_a_count} Tier A, {tier_b_count} Tier B"
    )


def run(
    city: str,
    county: str,
    state: str = "OH",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> None:
    """Run the code violation agent for one city (synchronous entry point)."""
    asyncio.run(_run_async(city, county, state, lookback_days))


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Code violation agent — Ohio POC")
    parser.add_argument(
        "--city",
        default="cleveland",
        help="City name (default: cleveland)",
    )
    parser.add_argument(
        "--county",
        default="cuyahoga",
        help="County name (default: cuyahoga)",
    )
    parser.add_argument(
        "--state",
        default="OH",
        help="State code (default: OH)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"Lookback days (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    args = parser.parse_args()
    run(args.city.lower(), args.county.lower(), args.state, args.days)
