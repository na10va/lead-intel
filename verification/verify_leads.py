from __future__ import annotations
"""
verification/verify_leads.py — Gate 1: Post-scrape record verification.

Runs after a record is stored, before enrichment begins.
Sets verified_raw=True only if all checks pass.
Failed records are stored but never enriched, scored, or routed.

CLI:
    python verification/verify_leads.py
"""

import argparse
import re
from datetime import date, timedelta

import usaddress

from db.client import get_client, update_row
from utils.logger import get_logger

log = get_logger("verification.verify_leads")

VALID_SOURCE_TYPES = {"probate", "code_violation", "foreclosure", "tax_lien"}
VALID_STATE_CODES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}

# County parcel ID formats — add more as scrapers are built
PARCEL_ID_PATTERNS = {
    "Cuyahoga": r"^\d{3}-\d{2}-\d{3}$",
    "Lake": r"^\d{2}-[A-Z]-\d{3}-[A-Z]-\d{3}(-\d+)?$",
    "Mahoning": r"^\d{2}-\d{3}-\d{4}(-\d+)?$",
}


def verify_raw_record(lead_id: str) -> bool:
    """Run Gate 1 verification on a single raw_leads record.

    Checks: required fields, address parseability, parcel ID format,
    filing date validity, and duplicate signal detection.

    Returns True if all required checks pass (sets verified_raw=True).
    Returns False if any required check fails (sets verified_raw=False).
    """
    client = get_client()
    lead = client.table("raw_leads").select("*").eq("id", lead_id).single().execute().data

    if not lead:
        log.error(f"Lead {lead_id} not found")
        return False

    failures = []

    # Required field checks
    # code_violation records have no owner at scrape time — enrichment fills it in
    if lead.get("source_type") != "code_violation":
        if not (lead.get("owner_name") or "").strip():
            failures.append("missing owner_name")

    property_address = (lead.get("property_address") or "").strip()
    if not property_address:
        # Cuyahoga tax lien PDFs contain no property address — enrichment populates
        # it via parcel ID lookup. Allow through if a parcel_id is present.
        if not (lead.get("parcel_id") or "").strip():
            failures.append("missing property_address")
    elif not _is_parseable_address(property_address):
        failures.append(f"unparseable address: {property_address}")

    state = lead.get("state", "").upper()
    if state not in VALID_STATE_CODES:
        failures.append(f"invalid state code: {state}")

    if not (lead.get("county") or "").strip():
        failures.append("missing county")

    filing_date = lead.get("filing_date")
    if filing_date:
        date_error = _validate_filing_date(filing_date)
        if date_error:
            failures.append(date_error)
    else:
        failures.append("missing filing_date")

    source_type = lead.get("source_type", "")
    if source_type not in VALID_SOURCE_TYPES:
        failures.append(f"source_type '{source_type}' not eligible for Gate 1 (Tier D leads skip verification)")

    # Parcel ID format check (warn only — do not reject)
    # code_violation records use TICKET_ID as parcel_id — skip format check for that source.
    parcel_id = lead.get("parcel_id")
    county = lead.get("county", "")
    if parcel_id and county in PARCEL_ID_PATTERNS and source_type not in ("code_violation", "foreclosure"):
        pattern = PARCEL_ID_PATTERNS[county]
        if not re.match(pattern, parcel_id):
            log.warning(f"Lead {lead_id}: parcel ID '{parcel_id}' format unexpected for {county}")

    if failures:
        notes = "; ".join(failures)
        update_row("raw_leads", lead_id, {
            "verified_raw": False,
            "verification_notes": notes,
        })
        log.warning(f"Lead {lead_id} failed Gate 1: {notes}")
        return False

    # Check for stacked signals (same address in 2+ source types — scoring bonus)
    _check_stacked_signals(lead_id, lead)

    update_row("raw_leads", lead_id, {"verified_raw": True})
    log.info(f"Lead {lead_id} passed Gate 1 verification")
    return True


def _is_parseable_address(address: str) -> bool:
    """Return True if the address contains a street number and street name."""
    try:
        parsed, _ = usaddress.tag(address)
        return "AddressNumber" in parsed and "StreetName" in parsed
    except usaddress.RepeatedLabelError:
        return False


def _validate_filing_date(filing_date) -> str | None:
    """Return an error string if the filing date is invalid, else None."""
    if isinstance(filing_date, str):
        try:
            filing_date = date.fromisoformat(filing_date)
        except ValueError:
            return f"unparseable filing_date: {filing_date}"

    today = date.today()
    if filing_date > today:
        return f"filing_date {filing_date} is in the future"
    if filing_date < today - timedelta(days=180):
        return f"filing_date {filing_date} is older than 180 days"

    return None


def _check_stacked_signals(lead_id: str, lead: dict) -> None:
    """Flag stacked signals (same address or owner in 2+ source types) in raw_data."""
    from utils.deduper import get_stacked_signals

    address = lead.get("property_address", "")
    county = lead.get("county", "")
    if not address or not county:
        return

    stacked = get_stacked_signals(address, county, days=30)
    if len(stacked) >= 2:
        raw_data = lead.get("raw_data") or {}
        raw_data["stacked_signals"] = stacked
        update_row("raw_leads", lead_id, {"raw_data": raw_data})
        log.info(f"Lead {lead_id}: stacked signals detected — {stacked}")


def run_all_unverified() -> None:
    """Run Gate 1 verification on all unverified raw leads.

    Catches both explicit verified_raw=False (agent-inserted leads) and
    verified_raw IS NULL (leads where the field was never set).
    """
    client = get_client()
    response = (
        client.table("raw_leads")
        .select("id")
        .or_("verified_raw.is.null,verified_raw.eq.false")
        .is_("score", "null")
        .execute()
    )
    leads = response.data or []
    log.info(f"Running Gate 1 on {len(leads)} unverified records")

    passed = failed = 0
    for row in leads:
        if verify_raw_record(row["id"]):
            passed += 1
        else:
            failed += 1

    log.info(f"Gate 1 complete — {passed} passed, {failed} failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Gate 1 verification on all unverified leads")
    parser.parse_args()
    run_all_unverified()
