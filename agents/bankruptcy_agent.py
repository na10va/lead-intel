from __future__ import annotations
"""
agents/bankruptcy_agent.py — Tier D: pulls Chapter 7 & 13 filings from Ohio Northern District.

IMPORTANT: Tier D — stored only. Never routed or notified on its own.
Only adds scoring value when stacked with a Tier A/B/C signal on the same property.

Sources (Ohio Northern Bankruptcy District — covers Cuyahoga, Lake, Mahoning):

  CourtListener RECAP (primary — free):
    REST API v4: https://www.courtlistener.com/api/rest/v4/dockets/
    Court code: ohnb
    Auth: Authorization: Token {COURTLISTENER_API_KEY} in .env
    Coverage: PACER documents contributed voluntarily via RECAP browser extension.
              May lag real-time by hours. Best for daily batch queries.
    Chapters:  Returned as integer field on each docket record.
    Address:   Debtor full address NOT in docket API. Party name only.
               Address resolved downstream by enrichment/public_sources.py.

  PACER PCL API (fallback — requires account):
    Register at: https://pacer.uscourts.gov (free to register)
    Login:  POST https://pacer.login.uscourts.gov/services/cso-auth
    Search: GET  https://pcl.uscourts.gov/pcl/api/cases
    Returns: debtor name, city, state, chapter, filing date — no per-page charge for search.
             Document pages (petition, schedules) cost $0.10/page — never fetched here.
    Quarterly billing: accounts under $30/quarter pay nothing (75% of users).
    Credentials: PACER_USERNAME + PACER_PASSWORD in .env
    Triggered: when CourtListener returns 0 new cases, OR PACER_USERNAME is set (supplement).

Address resolution:
  CourtListener provides debtor name only (not address) without fetching petition documents.
  PACER PCL search returns city + state, which allows county assignment.
  Full street address is resolved by enrichment/public_sources.py via county auditor name lookup.
  Records without a full address have property_address=None and address_unknown=True in raw_data.

County assignment:
  Ohio Northern District covers ~40+ Ohio counties — not just our 3 targets.
  PACER city/state is used to confirm Cuyahoga / Lake / Mahoning.
  CourtListener records without city: stored with county="Unknown" for enrichment to resolve.
  Records in non-target counties are skipped and not stored.

Deduplication:
  Uses case_number as parcel_id (only unique natural key for bankruptcy records).
  Deduper Strategy 1 (parcel_id match) checks (case_number, county, "bankruptcy").

Filing cadence:
  Runs daily at 7:00 AM EST. Queries filings from past 2 days to catch CourtListener lag.

CLI:
    python agents/bankruptcy_agent.py --district ohio_northern
    python agents/bankruptcy_agent.py --district ohio_northern --since 2026-04-01
"""

import argparse
import os
import time
from datetime import date, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv

from db.client import insert_row
from utils.deduper import is_duplicate
from utils.logger import get_logger

load_dotenv()

log = get_logger("bankruptcy_agent")

# =============================================================================
# Constants
# =============================================================================

CL_BASE = "https://www.courtlistener.com/api/rest/v4"
PACER_LOGIN_URL = "https://pacer.login.uscourts.gov/services/cso-auth"
PCL_SEARCH_URL = "https://pcl.uscourts.gov/pcl/api/cases"

TARGET_CHAPTERS = {7, 13}
DEFAULT_LOOKBACK_DAYS = 2   # catch CourtListener RECAP lag

DISTRICTS: dict[str, dict] = {
    "ohio_northern": {
        "name": "Ohio Northern Bankruptcy District",
        "cl_court_code": "ohnb",
        "pacer_court_id": "ohnb",
        "target_counties": ["Cuyahoga", "Lake", "Mahoning"],
    }
}

# Cities used to assign county when debtor city is known (PACER PCL returns city/state).
# Covers incorporated cities + major townships in each county.
# SELECTORS: update if city assignments are wrong after live testing.
_COUNTY_CITIES: dict[str, set[str]] = {
    "Cuyahoga": {
        "cleveland", "cleveland heights", "lakewood", "parma", "euclid",
        "garfield heights", "maple heights", "shaker heights", "east cleveland",
        "westlake", "north olmsted", "strongsville", "berea", "brook park",
        "solon", "bedford", "bedford heights", "south euclid", "university heights",
        "richmond heights", "highland heights", "lyndhurst", "mayfield heights",
        "brecksville", "independence", "seven hills", "north royalton",
        "broadview heights", "olmsted falls", "fairview park", "rocky river",
        "bay village", "avon lake", "avon", "north ridgeville", "middleburg heights",
    },
    "Lake": {
        "mentor", "painesville", "willoughby", "eastlake", "wickliffe", "madison",
        "perry", "fairport harbor", "kirtland", "willoughby hills", "grand river",
        "concord", "leroy", "chardon", "mentor on the lake",
    },
    "Mahoning": {
        "youngstown", "boardman", "austintown", "canfield", "poland", "struthers",
        "campbell", "girard", "liberty", "hubbard", "niles", "vienna",
        "lowellville", "new middletown", "north jackson",
    },
}


def _city_to_county(city: str) -> Optional[str]:
    """Map a debtor city name to one of the three target Ohio counties.

    Returns None if the city is not in a target county (non-target district cities).
    """
    city_lower = city.lower().strip()
    for county, cities in _COUNTY_CITIES.items():
        if city_lower in cities:
            return county
    return None


# =============================================================================
# CourtListener RECAP — primary free source
# =============================================================================

def _cl_headers() -> dict:
    api_key = os.getenv("COURTLISTENER_API_KEY", "")
    if not api_key:
        log.warning("COURTLISTENER_API_KEY not set — requests may be rate-limited")
    return {"Authorization": f"Token {api_key}"} if api_key else {}


def _cl_fetch_dockets(court_code: str, since: date) -> list[dict]:
    """Fetch recent bankruptcy dockets from CourtListener RECAP archive.

    Queries the /dockets/ endpoint filtered by court + filing date.
    Handles CourtListener's cursor-based next/previous pagination.
    Filters client-side for chapters 7 and 13 — no server-side chapter filter needed.
    """
    headers = _cl_headers()
    url = f"{CL_BASE}/dockets/"
    params: dict = {
        "court": court_code,
        "date_filed__gte": since.isoformat(),
        "page_size": 100,
        "order_by": "date_filed",
    }

    results: list[dict] = []
    page_num = 0

    while url:
        try:
            resp = requests.get(
                url,
                headers=headers,
                params=params if page_num == 0 else {},  # params encoded in next URL
                timeout=20,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error(f"CourtListener dockets request failed: {e}")
            break

        data = resp.json()
        batch = data.get("results", [])

        for docket in batch:
            chapter = docket.get("chapter")
            if chapter in TARGET_CHAPTERS:
                results.append(docket)

        url = data.get("next")  # None when no more pages
        page_num += 1
        time.sleep(0.2)  # 5 req/s limit for authenticated CourtListener

    log.info(f"CourtListener: fetched {len(results)} Ch7/13 dockets for {court_code} since {since}")
    return results


def _cl_fetch_debtor_name(docket_id: int) -> str:
    """Extract debtor name from CourtListener parties endpoint for a given docket.

    Returns the name of the first debtor party found, or empty string if unavailable.
    CourtListener party records include name but typically not a full street address.
    """
    headers = _cl_headers()
    try:
        resp = requests.get(
            f"{CL_BASE}/parties/",
            headers=headers,
            params={"docket": docket_id, "page_size": 20},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return ""

    for party in resp.json().get("results", []):
        party_types = [pt.get("name", "").lower() for pt in party.get("party_types", [])]
        if any("debtor" in pt for pt in party_types):
            return party.get("name", "").strip()

    return ""


def _parse_cl_case_name(case_name: str) -> str:
    """Extract debtor name from CourtListener case_name field.

    Format is typically "In re: DEBTOR NAME" or "In re DEBTOR NAME".
    Falls back to the raw case_name if the prefix isn't present.
    """
    lower = case_name.lower()
    for prefix in ("in re: ", "in re "):
        if lower.startswith(prefix):
            return case_name[len(prefix):].strip()
    return case_name.strip()


def fetch_courtlistener(district_info: dict, since: date) -> list[dict]:
    """Fetch and structure Chapter 7 + 13 filings from CourtListener RECAP.

    Returns a list of normalized record dicts ready for parse_bankruptcy().
    Debtor name sourced from case_name field (primary) or parties endpoint (fallback).
    City/state: not available from CourtListener API — county set to "Unknown".
    """
    court_code = district_info["cl_court_code"]
    dockets = _cl_fetch_dockets(court_code, since)

    records: list[dict] = []
    for d in dockets:
        case_name = d.get("case_name", "") or ""
        debtor_name = _parse_cl_case_name(case_name)

        # If case_name parsing didn't yield a clean name, try parties endpoint
        if not debtor_name or debtor_name.lower().startswith("in re"):
            debtor_name = _cl_fetch_debtor_name(d["id"]) or debtor_name
            time.sleep(0.2)

        records.append({
            "_source": "courtlistener",
            "_case_number": d.get("docket_number", ""),
            "_chapter": d.get("chapter"),
            "_filing_date": d.get("date_filed", ""),
            "_debtor_name": debtor_name,
            "_city": "",       # CourtListener does not expose debtor city
            "_state": "OH",
            "_cl_url": f"https://www.courtlistener.com{d.get('absolute_url', '')}",
        })

    return records


# =============================================================================
# PACER PCL API — fallback (requires PACER_USERNAME + PACER_PASSWORD in .env)
# =============================================================================

def _pacer_login() -> Optional[str]:
    """Authenticate with PACER and return the nextGenCSO session token.

    Returns None if credentials are missing or login fails.
    PACER PCL search is free — no per-page charge for case list queries.

    SELECTORS: verify response JSON structure after first live test.
    Official docs: https://pacer.uscourts.gov/help/pacer/pacer-case-locator-pcl-api-user-guide
    """
    username = os.getenv("PACER_USERNAME", "")
    password = os.getenv("PACER_PASSWORD", "")
    if not username or not password:
        return None

    try:
        resp = requests.post(
            PACER_LOGIN_URL,
            json={"loginId": username, "password": password, "clientCode": ""},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"PACER login failed: {e}")
        return None

    data = resp.json()
    # SELECTORS: verify exact key path after live test — may be nested differently
    token = (
        data.get("nextGenCSO")
        or data.get("loginResult", {}).get("nextGenCSO")
        or data.get("login_result", {}).get("nextGenCSO")
    )
    if not token:
        log.error(f"PACER login response missing nextGenCSO token: {list(data.keys())}")
    return token


def _pacer_search_chapter(
    token: str, court_id: str, chapter: int, since: date
) -> list[dict]:
    """Search PACER PCL for Ohio Northern bankruptcy cases of one chapter type.

    The PCL case list endpoint is free — only document pages cost $0.10/page.
    Returns raw PACER case dicts. Call once for ch7, once for ch13.

    SELECTORS: date format and field names must be verified against live PACER API.
    PCL API User Guide: https://pacer.uscourts.gov/help/pacer/pacer-case-locator-pcl-api-user-guide
    """
    headers = {
        "X-NEXT-GEN-CSO": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    records: list[dict] = []
    page = 0

    while True:
        params = {
            "court": court_id,
            "chapter": chapter,
            "dateFiledFrom": since.strftime("%m/%d/%Y"),   # PACER uses MM/DD/YYYY
            "dateFiledTo": date.today().strftime("%m/%d/%Y"),
            "page": page,
            "size": 100,
        }
        try:
            resp = requests.get(
                PCL_SEARCH_URL,
                headers=headers,
                params=params,
                timeout=20,
            )
            if resp.status_code == 204:
                break
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error(f"PACER PCL search failed (chapter {chapter}, page {page}): {e}")
            break

        data = resp.json()
        # SELECTORS: PCL may nest results under "content", "cases", or return a flat list
        cases = data if isinstance(data, list) else (
            data.get("content") or data.get("cases") or data.get("results") or []
        )
        if not cases:
            break

        records.extend(cases)
        if len(cases) < 100:
            break
        page += 1
        time.sleep(0.5)

    return records


def fetch_pacer(district_info: dict, since: date) -> list[dict]:
    """Fetch Chapter 7 + 13 filings from PACER PCL API.

    Returns a list of normalized record dicts ready for parse_bankruptcy().
    PACER PCL includes city + state for county assignment. Full street address
    requires fetching the petition document ($0.10/page) — not done here.
    """
    token = _pacer_login()
    if not token:
        log.warning("PACER credentials missing or login failed — skipping PACER fetch")
        return []

    court_id = district_info["pacer_court_id"]
    raw_records: list[dict] = []
    for chapter in TARGET_CHAPTERS:
        batch = _pacer_search_chapter(token, court_id, chapter, since)
        log.info(f"PACER PCL: chapter {chapter} — {len(batch)} records")
        raw_records.extend(batch)

    records: list[dict] = []
    for r in raw_records:
        # SELECTORS: field names must be verified against live PACER PCL response
        last = r.get("lastName", r.get("last_name", ""))
        first = r.get("firstName", r.get("first_name", ""))
        middle = r.get("middleName", r.get("middle_name", ""))
        debtor_name = " ".join(filter(None, [first, middle, last])).strip()
        if not debtor_name:
            debtor_name = r.get("caseName", r.get("case_name", ""))

        records.append({
            "_source": "pacer",
            "_case_number": r.get("caseNumber", r.get("case_number", "")),
            "_chapter": r.get("chapter"),
            "_filing_date": r.get("dateFiled", r.get("date_filed", r.get("dateOpen", ""))),
            "_debtor_name": debtor_name,
            "_city": r.get("city", ""),
            "_state": r.get("state", "OH"),
            "_cl_url": "",
        })

    return records


# =============================================================================
# Parse — map raw records to raw_leads schema
# =============================================================================

def parse_bankruptcy(raw: dict, district_info: dict) -> Optional[dict]:
    """Map a raw bankruptcy record (CourtListener or PACER) to the raw_leads schema.

    filing_date: from PACER/CourtListener filing date field.
    owner_name: debtor name — used for county auditor lookup during enrichment.
    property_address: None (not available without fetching petition document).
    parcel_id: set to case_number — used as dedup key (unique per case).
    county: assigned from debtor city (PACER only); "Unknown" if city unavailable.

    Returns None if the debtor city is known but maps to a non-target county.
    """
    case_number = raw.get("_case_number", "").strip()
    debtor_name = raw.get("_debtor_name", "").strip()
    city = raw.get("_city", "").strip()
    chapter = raw.get("_chapter")
    filing_date_str = raw.get("_filing_date", "")

    if not debtor_name:
        return None

    # County assignment — skip if city is in a non-target county
    county: str
    if city:
        assigned = _city_to_county(city)
        if assigned is None:
            log.debug(f"Skipping bankruptcy — city '{city}' not in target counties")
            return None
        county = assigned
    else:
        county = "Unknown"  # enrichment resolves via auditor name search

    # Normalize filing date to ISO format
    filing_date: str
    if filing_date_str:
        # Handle PACER MM/DD/YYYY and CourtListener YYYY-MM-DD formats
        try:
            from datetime import datetime
            if "/" in filing_date_str:
                filing_date = datetime.strptime(filing_date_str, "%m/%d/%Y").date().isoformat()
            else:
                filing_date = filing_date_str[:10]
        except ValueError:
            filing_date = date.today().isoformat()
    else:
        filing_date = date.today().isoformat()

    return {
        "owner_name": debtor_name,
        "property_address": None,
        "parcel_id": case_number or None,
        "filing_date": filing_date,
        "source_type": "bankruptcy",
        "source_name": f"PACER — {district_info['name']}",
        "state": raw.get("_state", "OH"),
        "county": county,
        "raw_data": {
            "case_number": case_number,
            "chapter": chapter,
            "debtor_city": city,
            "source": raw.get("_source"),
            "courtlistener_url": raw.get("_cl_url", ""),
            "address_unknown": True,  # full address requires petition document fetch
        },
        "verified_raw": False,
        "verified_enriched": False,
        "enriched": False,
    }


# =============================================================================
# Main agent — Tier D store-only pipeline (no enrichment / scoring / routing)
# =============================================================================

def run(district: str, since: Optional[date] = None) -> None:
    """Fetch Chapter 7 + 13 bankruptcy filings and store as Tier D leads.

    Pipeline: FETCH → PARSE → DEDUPE → STORE.
    No enrichment, scoring, or routing — Tier D is stored only.

    Strategy:
      1. Try CourtListener RECAP (free, API key optional but recommended).
      2. If CourtListener returns 0 new filings AND PACER credentials are set,
         run PACER PCL as a supplement (catches RECAP lag for same-day filings).
      3. If PACER credentials are set regardless, run PACER as a parallel supplement
         to maximize coverage (PCL case-list queries are free).
    """
    district_info = DISTRICTS.get(district)
    if not district_info:
        log.error(f"Unknown district: {district}. Valid: {list(DISTRICTS.keys())}")
        return

    if since is None:
        since = date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)

    log.info(
        f"Bankruptcy agent starting — {district_info['name']} | "
        f"chapters: {sorted(TARGET_CHAPTERS)} | since: {since}"
    )

    # 1. FETCH — CourtListener primary
    cl_records = fetch_courtlistener(district_info, since)

    # PACER supplement: run if credentials are configured (catches RECAP lag)
    pacer_records: list[dict] = []
    if os.getenv("PACER_USERNAME"):
        pacer_records = fetch_pacer(district_info, since)
        if pacer_records:
            log.info(f"PACER supplement: {len(pacer_records)} additional records")

    raw_records = cl_records + pacer_records

    if not raw_records:
        log.warning(
            f"Bankruptcy agent: 0 records returned for {district_info['name']} since {since}. "
            "This may be normal (no new filings) or may indicate a source issue."
        )
        return

    log.info(f"Processing {len(raw_records)} raw records ({len(cl_records)} CL, {len(pacer_records)} PACER)")
    new_records = 0
    skipped_county = 0

    for raw in raw_records:
        try:
            # 2. PARSE
            record = parse_bankruptcy(raw, district_info)
            if record is None:
                skipped_county += 1
                continue

            if not record.get("owner_name"):
                continue

            # 3. DEDUPE — case_number stored as parcel_id; county may be "Unknown"
            # Dedup by case_number across all counties (case numbers are globally unique)
            dup_county = record["county"] if record["county"] != "Unknown" else "Ohio Northern"
            if is_duplicate(
                county=dup_county,
                source_type="bankruptcy",
                parcel_id=record.get("parcel_id"),
                owner_name=record.get("owner_name"),
            ):
                continue

            # 4. STORE — Tier D: set tier directly, no scoring or routing
            record["tier"] = "D"
            insert_row("raw_leads", record)
            new_records += 1

        except Exception as e:
            log.error(f"Error processing bankruptcy record: {e}")
            continue

    log.info(
        f"Bankruptcy agent complete — {district_info['name']} | "
        f"{new_records} new Tier D records stored | "
        f"{skipped_county} skipped (non-target county)"
    )


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bankruptcy agent (Tier D) — Ohio Northern District",
        epilog=(
            "Primary: CourtListener RECAP (free, set COURTLISTENER_API_KEY). "
            "Fallback: PACER PCL (set PACER_USERNAME + PACER_PASSWORD — free under $30/qtr). "
            "Debtors in non-target counties (not Cuyahoga/Lake/Mahoning) are skipped."
        ),
    )
    parser.add_argument(
        "--district",
        default="ohio_northern",
        help="PACER district key (default: ohio_northern)",
    )
    parser.add_argument(
        "--since",
        type=lambda s: date.fromisoformat(s),
        help="Fetch filings since this date (ISO format: YYYY-MM-DD). Default: 2 days ago.",
    )
    args = parser.parse_args()
    run(args.district, since=args.since)
