"""
enrichment/county_auditor.py — Step 2: Pull assessed value + last sale from county auditor.

For each Ohio county, queries the public GIS ArcGIS REST API to fetch:
    estimated_value   — county market value assessment (USD)
    last_sale_date    — date of last recorded deed transfer
    last_sale_price   — price paid at last transfer (USD)

Equity is NOT calculated here — county auditors don't expose mortgage balances.
Records updated here will have equity_unknown=True; the scoring model gives those
+8 neutral points and scores the remaining deal axis from estimated_value and
last_sale_date (years_held). This alone can push a Tier C lead into Tier B.

Sources (all public GIS REST APIs, no auth required):

    Cuyahoga  gis.cuyahogacounty.gov  Open_Data_Parcels ArcGIS MapServer
              Parcel lookup:  parcelpin = '{8-digit-no-hyphens}'
              Address lookup: par_addr_all LIKE '{number}%{street}%'
              Value field:    certified_tax_total / 0.35  (assessed → market)
              Sale fields:    transfer_date (Unix ms), sales_amount

    Lake      gis.lakecountyohio.gov  Parcels_AppraisedValues_Publish FeatureServer
              Parcel lookup:  PIN_NODASH = '{parcel_id_stripped}'
              Value field:    A_VAL_TOTAL (direct appraised/market value — no division)
              Sale fields:    A_SALE_DATE (Unix ms), A_SALE_AMOUNT

    Mahoning  gisapp.mahoningcountyoh.gov  PUBLIC_WEBSITE_CADASTRAL MapServer
              Step 1: address lookup in layer 37 (Address Relate) → PARCEL_ID
              Step 2: assessment lookup in layer 38 (Assessment Relate) using PARCEL_ID
              Value formula: (LandASsessment + ImprASsessment) / 0.35  (most recent year)
              Sale fields:    not available from this GIS source (null)

CLI:
    # Dry run — 5 leads per county, print results, no DB writes
    python enrichment/county_auditor.py --sample 5

    # Full batch — all counties
    python enrichment/county_auditor.py --all

    # Single county
    python enrichment/county_auditor.py --all --county cuyahoga
    python enrichment/county_auditor.py --all --county lake
    python enrichment/county_auditor.py --all --county mahoning
"""

import argparse
import re
import time
from datetime import date, datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

from db.client import get_client, update_row
from scoring.score import score_lead
from utils.logger import get_logger

load_dotenv()
log = get_logger("enrichment.county_auditor")

# Polite delay between requests — county GIS servers aren't built for high throughput
REQUEST_DELAY_S = 1.0


# =============================================================================
# Shared helpers
# =============================================================================

def _parse_ohio_dollar(text: str) -> Optional[int]:
    """Parse '$120,000' or '120000.0' to int. Returns None on failure."""
    cleaned = re.sub(r"[^\d.]", "", str(text).strip())
    try:
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def _parse_ohio_date(text: str) -> Optional[date]:
    """Parse common Ohio date formats: MM/DD/YYYY or YYYY-MM-DD."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(str(text).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _unix_ms_to_date(ms: Optional[int]) -> Optional[date]:
    """Convert a Unix millisecond timestamp to a Python date. Returns None if ms is None."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
    except (ValueError, OSError, OverflowError):
        return None


def _extract_address_parts(address: str) -> tuple[str, str]:
    """Split '10904 Shale Ave' or '1650 5th Ave' into ('10904', 'SHALE') / ('1650', '5TH').

    Handles both alphabetic and ordinal (5th, 112th) street names.
    Returns (street_number, first_word_of_street) in uppercase.
    Both are empty strings if the address can't be parsed.
    """
    m = re.match(r"^(\d+)\s+([A-Za-z0-9]+)", address.strip())
    if not m:
        return "", ""
    return m.group(1), m.group(2).upper()


# =============================================================================
# Cuyahoga County — gis.cuyahogacounty.gov Open_Data_Parcels
# =============================================================================

CUYAHOGA_GIS_URL = (
    "https://gis.cuyahogacounty.gov/server/rest/services/"
    "Open_Data_Parcels/MapServer/0/query"
)
CUYAHOGA_OUT_FIELDS = "parcelpin,certified_tax_total,sales_amount,transfer_date,par_addr_all"

# Real Cuyahoga parcel IDs: XXX-XX-XXX (e.g. 019-12-089)
# Code violation case IDs: CT26003660 — these need address-based lookup
_CUYAHOGA_PARCEL_RE = re.compile(r"^\d{3}-\d{2}-\d{3}$")


def _cuyahoga_query(where_clause: str) -> Optional[dict]:
    """Execute a query against the Cuyahoga Open_Data_Parcels GIS and return the first feature."""
    params = {
        "where": where_clause,
        "outFields": CUYAHOGA_OUT_FIELDS,
        "f": "json",
    }
    try:
        resp = requests.get(CUYAHOGA_GIS_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Cuyahoga GIS request failed ({where_clause!r}): {e}")
        return None

    features = data.get("features") or []
    if not features:
        return None
    return features[0]["attributes"]


def lookup_cuyahoga_parcel(parcel_id: str) -> Optional[dict]:
    """Look up a Cuyahoga parcel by parcel_id (XXX-XX-XXX). Strips hyphens for the GIS query."""
    pin = re.sub(r"[^0-9]", "", parcel_id)  # "019-12-089" → "01912089"
    attrs = _cuyahoga_query(f"parcelpin='{pin}'")
    if attrs is None:
        log.warning(f"Cuyahoga parcel not found in GIS: {parcel_id} (pin={pin})")
        return None

    assessed = attrs.get("certified_tax_total")
    if not assessed:
        log.warning(f"Cuyahoga parcel {parcel_id}: certified_tax_total missing or zero")
        return None

    market_value = int(float(assessed) / 0.35)
    sale_date    = _unix_ms_to_date(attrs.get("transfer_date"))
    sale_price   = attrs.get("sales_amount")
    if sale_price is not None:
        sale_price = int(float(sale_price))

    par_addr = (attrs.get("par_addr_all") or "").strip() or None

    log.info(f"Cuyahoga {parcel_id}: assessed=${int(assessed):,} → market=${market_value:,}  "
             f"last_sale={sale_date}  price={sale_price}")
    return {
        "estimated_value": market_value,
        "last_sale_date":  str(sale_date) if sale_date else None,
        "last_sale_price": sale_price,
        "_gis_address":    par_addr,  # written back to property_address if lead has none
    }


def lookup_cuyahoga_address(address: str) -> Optional[dict]:
    """Look up a Cuyahoga property by address (used for code violation leads without parcel_id)."""
    number, street = _extract_address_parts(address)
    if not number or not street:
        log.warning(f"Cuyahoga address lookup: could not parse address '{address}'")
        return None

    where = f"par_addr_all LIKE '{number}%{street}%'"
    attrs = _cuyahoga_query(where)
    if attrs is None:
        log.warning(f"Cuyahoga address not found in GIS: '{address}'")
        return None

    assessed = attrs.get("certified_tax_total")
    if not assessed:
        log.warning(f"Cuyahoga address '{address}': certified_tax_total missing or zero")
        return None

    market_value = int(float(assessed) / 0.35)
    sale_date    = _unix_ms_to_date(attrs.get("transfer_date"))
    sale_price   = attrs.get("sales_amount")
    if sale_price is not None:
        sale_price = int(float(sale_price))

    log.info(f"Cuyahoga '{address}': assessed=${int(assessed):,} → market=${market_value:,}  "
             f"last_sale={sale_date}")
    return {
        "estimated_value": market_value,
        "last_sale_date":  str(sale_date) if sale_date else None,
        "last_sale_price": sale_price,
    }


def enrich_cuyahoga(lead: dict) -> Optional[dict]:
    """Enrich a Cuyahoga lead with county auditor GIS data."""
    parcel_id = (lead.get("parcel_id") or "").strip()
    address   = lead.get("geocoded_address") or lead.get("property_address") or ""

    if _CUYAHOGA_PARCEL_RE.match(parcel_id):
        result = lookup_cuyahoga_parcel(parcel_id)
        if result:
            # If the lead has no property_address, write back the GIS address now
            gis_addr = result.pop("_gis_address", None)
            if gis_addr and not address:
                from db.client import update_row
                update_row("raw_leads", lead["id"], {"property_address": gis_addr})
                lead["property_address"] = gis_addr  # update in-memory so waterfall sees it
                log.info(f"Cuyahoga {parcel_id}: wrote GIS address → '{gis_addr}'")
            return result

    # Fall back to address search (required for code violation leads with case IDs)
    if address:
        return lookup_cuyahoga_address(address)

    log.warning(f"Cuyahoga lead {lead['id']}: no usable parcel_id or address — skipping")
    return None


# =============================================================================
# Lake County — gis.lakecountyohio.gov Parcels_AppraisedValues_Publish
# =============================================================================

LAKE_GIS_URL = (
    "https://gis.lakecountyohio.gov/arcgis/rest/services/"
    "Auditor/Parcels_AppraisedValues_Publish/FeatureServer/0/query"
)
LAKE_OUT_FIELDS = "PIN,PIN_NODASH,A_VAL_TOTAL,A_SALE_DATE,A_SALE_AMOUNT,A_YEAR_BUILT"


def lookup_lake_parcel(parcel_id: str) -> Optional[dict]:
    """Look up a Lake County parcel via the GIS Parcels_AppraisedValues service."""
    pin_nodash = re.sub(r"[^A-Za-z0-9]", "", parcel_id)  # strip hyphens/dashes
    params = {
        "where":     f"PIN_NODASH='{pin_nodash}'",
        "outFields": LAKE_OUT_FIELDS,
        "f":         "json",
    }
    try:
        resp = requests.get(LAKE_GIS_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Lake County GIS request failed for {parcel_id}: {e}")
        return None

    features = data.get("features") or []
    if not features:
        log.warning(f"Lake County parcel not found in GIS: {parcel_id} (pin_nodash={pin_nodash})")
        return None

    attrs = features[0]["attributes"]
    market_value = attrs.get("A_VAL_TOTAL")  # appraised value — already in market dollars
    if not market_value:
        log.warning(f"Lake parcel {parcel_id}: A_VAL_TOTAL missing or zero")
        return None

    market_value = int(float(market_value))
    sale_date    = _unix_ms_to_date(attrs.get("A_SALE_DATE"))
    sale_price   = attrs.get("A_SALE_AMOUNT")
    if sale_price is not None:
        sale_price = int(float(sale_price))

    log.info(f"Lake {parcel_id}: market=${market_value:,}  last_sale={sale_date}  price={sale_price}")
    return {
        "estimated_value": market_value,
        "last_sale_date":  str(sale_date) if sale_date else None,
        "last_sale_price": sale_price,
    }


def enrich_lake(lead: dict) -> Optional[dict]:
    """Enrich a Lake County lead with county auditor GIS data."""
    parcel_id = (lead.get("parcel_id") or "").strip()
    if not parcel_id:
        log.warning(f"Lake lead {lead['id']}: no parcel_id — skipping")
        return None
    return lookup_lake_parcel(parcel_id)


# =============================================================================
# Mahoning County — gisapp.mahoningcountyoh.gov PUBLIC_WEBSITE_CADASTRAL
# =============================================================================
#
# Two-step lookup:
#   Layer 37 (Address Relate): address → PARCEL_ID
#   Layer 38 (Assessment Relate): PARCEL_ID → assessed values (multiple historical rows)
#
# A_VAL_TOTAL is NOT available here; assessed value = LandASsessment + ImprASsessment.
# Market value = total_assessed / 0.35  (Ohio standard).
# Last sale date/price are not available from these layers.
#

MAHONING_BASE  = "https://gisapp.mahoningcountyoh.gov/ArcGIS/rest/services/PUBLIC_WEBSITE_CADASTRAL/MapServer"
MAHONING_ADDR  = f"{MAHONING_BASE}/37/query"   # Address Relate
MAHONING_ASSD  = f"{MAHONING_BASE}/38/query"   # Assessment Relate


def _mahoning_address_to_parcel(address: str) -> Optional[str]:
    """Query layer 37 to get PARCEL_ID from a property address."""
    number, street = _extract_address_parts(address)
    if not number or not street:
        log.warning(f"Mahoning address lookup: could not parse address '{address}'")
        return None

    params = {
        "where":     f"MVP_ADDRESS LIKE '{number}%{street}%'",
        "outFields": "PARCEL_ID,MVP_ADDRESS",
        "f":         "json",
    }
    try:
        resp = requests.get(MAHONING_ADDR, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Mahoning address→parcel lookup failed for '{address}': {e}")
        return None

    features = data.get("features") or []
    if not features:
        log.warning(f"Mahoning: no parcel found for address '{address}'")
        return None

    # Multiple results possible (e.g. two adjacent lots with same street number).
    # Take the first — the address LIKE filter is already quite specific.
    parcel_id = features[0]["attributes"].get("PARCEL_ID")
    log.info(f"Mahoning address '{address}' → parcel {parcel_id} "
             f"(matched '{features[0]['attributes'].get('MVP_ADDRESS','')}')")
    return parcel_id


def _mahoning_parcel_to_value(parcel_id: str) -> Optional[int]:
    """Query layer 38 to get assessed value for a Mahoning parcel. Returns market value int."""
    params = {
        "where":     f"mpropertynumber='{parcel_id}'",
        "outFields": "mpropertynumber,LandTYear,LandASsessment,ImprASsessment",
        "f":         "json",
    }
    try:
        resp = requests.get(MAHONING_ASSD, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Mahoning assessment lookup failed for parcel {parcel_id}: {e}")
        return None

    features = data.get("features") or []
    if not features:
        log.warning(f"Mahoning: no assessment records for parcel {parcel_id}")
        return None

    # Multiple rows for different tax years — pick the most recent
    rows = [f["attributes"] for f in features]
    rows.sort(key=lambda r: int(r.get("LandTYear") or 0), reverse=True)
    best = rows[0]

    land  = float(best.get("LandASsessment") or 0)
    impr  = float(best.get("ImprASsessment") or 0)
    total_assessed = land + impr
    if total_assessed <= 0:
        log.warning(f"Mahoning parcel {parcel_id}: zero or missing assessed values (year={best.get('LandTYear')})")
        return None

    market_value = int(total_assessed / 0.35)
    log.info(f"Mahoning {parcel_id}: assessed=${int(total_assessed):,} (year={best.get('LandTYear')}) → market=${market_value:,}")
    return market_value


def lookup_mahoning_address(address: str) -> Optional[dict]:
    """Two-step Mahoning lookup: address → parcel_id → assessed value → market value."""
    parcel_id = _mahoning_address_to_parcel(address)
    if not parcel_id:
        return None

    market_value = _mahoning_parcel_to_value(parcel_id)
    if market_value is None:
        return None

    return {
        "estimated_value": market_value,
        "last_sale_date":  None,   # not available from this GIS source
        "last_sale_price": None,
    }


def enrich_mahoning(lead: dict) -> Optional[dict]:
    """Enrich a Mahoning County lead with county auditor GIS data."""
    address = lead.get("geocoded_address") or lead.get("property_address") or ""
    if not address:
        log.warning(f"Mahoning lead {lead['id']}: no address — skipping")
        return None
    return lookup_mahoning_address(address)


# =============================================================================
# Dispatch
# =============================================================================

_COUNTY_HANDLERS = {
    "cuyahoga": enrich_cuyahoga,
    "lake":     enrich_lake,
    "mahoning": enrich_mahoning,
}


def enrich_lead(lead: dict) -> Optional[dict]:
    """Route a lead to the correct county handler. Returns update dict or None."""
    county = (lead.get("county") or "").lower()
    handler = _COUNTY_HANDLERS.get(county)
    if handler is None:
        log.warning(f"No county auditor handler for county='{county}' (lead {lead['id']})")
        return None
    return handler(lead)


# =============================================================================
# Re-scoring helper
# =============================================================================

def rescore_lead(client, lead_id: str) -> None:
    """Re-fetch the updated lead from Supabase and write new score + tier."""
    try:
        lead = (
            client.table("raw_leads")
            .select("*")
            .eq("id", lead_id)
            .single()
            .execute()
            .data
        )
        if not lead:
            return
        result = score_lead(lead)
        update_row("raw_leads", lead_id, {**result, "scored_at": "now()"})
        log.info(
            f"Re-scored {lead_id}: distress={result['distress_score']} "
            f"deal={result['deal_score']} total={result['score']} tier={result['tier']}"
        )
    except Exception as e:
        log.error(f"Re-score failed for {lead_id}: {e}")


# =============================================================================
# Sample run
# =============================================================================

def run_sample(n_per_county: int = 5, county_filter: Optional[str] = None) -> None:
    """Dry-run county auditor lookups on n leads per county. No DB writes."""
    client = get_client()
    counties = [county_filter.title()] if county_filter else ["Cuyahoga", "Lake", "Mahoning"]

    for county in counties:
        leads = (
            client.table("raw_leads")
            .select("id, parcel_id, property_address, geocoded_address, county, owner_name, source_type")
            .eq("county", county)
            .is_("estimated_value", "null")
            .limit(n_per_county)
            .execute()
            .data or []
        )

        print(f"\n{'═'*90}")
        print(f"  COUNTY AUDITOR SAMPLE — {county.upper()} ({len(leads)} leads)")
        print(f"{'═'*90}")

        for lead in leads:
            result = enrich_lead(lead)
            time.sleep(REQUEST_DELAY_S)

            print(f"\n  Lead {lead['id'][:8]}...  source={lead.get('source_type')}")
            print(f"    parcel:    {lead.get('parcel_id') or 'none'}")
            print(f"    address:   {lead.get('property_address') or 'none'}")
            print(f"    geocoded:  {lead.get('geocoded_address') or 'not yet geocoded'}")
            if result:
                print(f"    ✓ est_value:      ${result['estimated_value']:,}")
                print(f"    ✓ last_sale_date:  {result.get('last_sale_date') or 'n/a'}")
                print(f"    ✓ last_sale_price: ${result['last_sale_price']:,}" if result.get("last_sale_price") else "    ✓ last_sale_price: n/a")
            else:
                print("    ✗ No data returned — check logs above for reason")

        print(f"\n{'─'*90}")
        print(f"  Dry run complete for {county}. No DB writes.")

    print(f"\n  Run with --all to process the full database.\n")


# =============================================================================
# Full batch run
# =============================================================================

def run_batch(county_filter: Optional[str] = None) -> None:
    """Enrich all leads missing estimated_value from county auditor. Re-scores after each write.

    Pagination strategy: always query from offset 0. Successful records set estimated_value
    (non-null) and drop out of the filter naturally. Failed records are marked
    auditor_attempted=true in verification_notes and excluded by the OR filter, preventing
    an infinite loop when the county GIS can't resolve a parcel.
    """
    counties = [county_filter.title()] if county_filter else ["Cuyahoga", "Lake", "Mahoning"]

    total_processed = 0
    total_succeeded = 0
    tier_changes    = {"A": 0, "B": 0, "C": 0, "D": 0}

    for county in counties:
        page_size = 200

        while True:
            client = get_client()  # fresh client per page to avoid HTTP/2 stale connections

            # Always query from offset 0. Successful records drop out (estimated_value set).
            # Failed records are marked auditor_attempted=true and excluded by the OR filter.
            leads = (
                client.table("raw_leads")
                .select("id, parcel_id, property_address, geocoded_address, county, owner_name, score, tier, verification_notes")
                .eq("county", county)
                .is_("estimated_value", "null")
                .or_("verification_notes.is.null,verification_notes.not.like.*auditor_attempted=true*")
                .limit(page_size)
                .execute()
                .data or []
            )
            if not leads:
                break

            log.info(f"{county}: enriching page of {len(leads)} leads")

            page_succeeded = 0
            for lead in leads:
                old_tier = lead.get("tier")
                result   = enrich_lead(lead)

                if result:
                    result["equity_unknown"] = True
                    try:
                        update_row("raw_leads", lead["id"], result)
                        total_succeeded += 1
                        page_succeeded  += 1
                        rescore_lead(client, lead["id"])
                        updated = (
                            client.table("raw_leads")
                            .select("tier")
                            .eq("id", lead["id"])
                            .single()
                            .execute()
                            .data or {}
                        )
                        new_tier = updated.get("tier")
                        if new_tier and new_tier != old_tier:
                            tier_changes[new_tier] = tier_changes.get(new_tier, 0) + 1
                            log.info(f"Tier upgrade: lead {lead['id'][:8]} {old_tier} → {new_tier}")
                    except Exception as e:
                        log.error(f"DB write failed for lead {lead['id']}: {e}")
                else:
                    # Mark as attempted so it's excluded from future pages
                    note = (lead.get("verification_notes") or "") + " | auditor_attempted=true"
                    try:
                        update_row("raw_leads", lead["id"], {"verification_notes": note.strip(" | ")})
                    except Exception as e:
                        log.error(f"Failed to mark auditor_attempted for {lead['id']}: {e}")

                total_processed += 1
                time.sleep(REQUEST_DELAY_S)

            # If the whole page failed, every record was just marked — next query will return 0
            if page_succeeded == 0:
                log.warning(f"{county}: full page of {len(leads)} leads could not be enriched — stopping county")
                break

    log.info(
        f"County auditor enrichment complete — "
        f"{total_succeeded}/{total_processed} enriched. "
        f"Tier upgrades: A={tier_changes['A']} B={tier_changes['B']} "
        f"C={tier_changes['C']} D={tier_changes['D']}"
    )


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="County auditor property value enrichment")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sample", type=int, metavar="N",
                       help="Dry run: look up N leads per county, print results, no DB writes")
    group.add_argument("--all", action="store_true",
                       help="Full batch: enrich all leads missing estimated_value")
    parser.add_argument("--county", choices=["cuyahoga", "lake", "mahoning"],
                        help="Limit to one county")
    args = parser.parse_args()

    if args.sample:
        run_sample(n_per_county=args.sample, county_filter=args.county)
    elif args.all:
        run_batch(county_filter=args.county)
