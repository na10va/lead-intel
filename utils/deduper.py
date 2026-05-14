"""
utils/deduper.py — Deduplication logic for raw_leads.

Every agent MUST call is_duplicate() before inserting a new record.
This is a hard rule from CLAUDE.md — never skip deduplication.

Two strategies are used, tried in order:
  1. Parcel ID match — exact match on (parcel_id, county, source_type).
     Fast and precise when parcel IDs are available.
  2. Fuzzy address + owner match — used when parcel ID is missing.
     Matches on normalized property_address + owner_name within the same county.

If either check returns a match, the record is a duplicate and must be skipped.

Usage:
    from utils.deduper import is_duplicate

    if is_duplicate(parcel_id="123-45-678", county="Cuyahoga", source_type="probate"):
        log.info("Duplicate — skipping")
    else:
        # safe to insert
"""

import re
from typing import List, Optional

from db.client import get_client
from utils.logger import get_logger

log = get_logger("deduper")


def _normalize_address(address: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace for fuzzy comparison."""
    address = address.lower()
    address = re.sub(r"[^\w\s]", "", address)   # remove punctuation
    address = re.sub(r"\s+", " ", address).strip()
    return address


def is_duplicate(
    county: str,
    source_type: str,
    parcel_id: Optional[str] = None,
    property_address: Optional[str] = None,
    owner_name: Optional[str] = None,
) -> bool:
    """Check whether a lead already exists in raw_leads.

    Strategy 1 — parcel ID (preferred):
        Runs when parcel_id is provided.
        Queries for an exact match on (parcel_id, county, source_type).

    Strategy 2 — address + owner (fallback):
        Runs when parcel_id is None and both property_address and owner_name
        are provided. Normalizes both strings and checks for an exact match
        on the normalized values within the same county and source_type.

    Returns True if a duplicate is found, False if safe to insert.
    """
    client = get_client()

    # ------------------------------------------------------------------
    # Strategy 1 — parcel ID match
    # ------------------------------------------------------------------
    if parcel_id:
        response = (
            client.table("raw_leads")
            .select("id")
            .eq("parcel_id", parcel_id)
            .eq("county", county)
            .eq("source_type", source_type)
            .limit(1)
            .execute()
        )
        if response.data:
            log.debug(
                f"Duplicate (parcel ID match): {parcel_id} | {county} | {source_type}"
            )
            return True

    # ------------------------------------------------------------------
    # Strategy 2 — normalized address + owner name
    # ------------------------------------------------------------------
    if property_address and owner_name:
        norm_address = _normalize_address(property_address)
        norm_owner = owner_name.lower().strip()

        # Pull all records for this county + source_type and compare normalized values.
        # Intentionally avoids a LIKE query to prevent partial matches.
        response = (
            client.table("raw_leads")
            .select("id, property_address, owner_name")
            .eq("county", county)
            .eq("source_type", source_type)
            .execute()
        )

        for row in response.data or []:
            existing_address = _normalize_address(row.get("property_address") or "")
            existing_owner = (row.get("owner_name") or "").lower().strip()

            if existing_address == norm_address and existing_owner == norm_owner:
                log.debug(
                    f"Duplicate (address+owner match): '{property_address}' | "
                    f"'{owner_name}' | {county} | {source_type}"
                )
                return True

    return False


def get_stacked_signals(
    property_address: str,
    county: str,
    days: int = 30,
) -> List[str]:
    """Return a list of source_types that share the same address within `days` days.

    Used by the scoring engine to detect stacked signals (same address appearing
    in multiple source types within 30 days — a scoring bonus per CLAUDE.md).

    Args:
        property_address: Full property address to look up.
        county:           County name.
        days:             Lookback window in days (default 30).

    Returns a list of source_type strings found for this address, e.g.
    ["probate", "tax_lien"]. Empty list if no stacked signals.
    """
    client = get_client()
    norm_address = _normalize_address(property_address)

    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    response = (
        client.table("raw_leads")
        .select("source_type, property_address, filing_date")
        .eq("county", county)
        .gte("filing_date", cutoff)
        .execute()
    )

    matched_types: List[str] = []
    for row in response.data or []:
        if _normalize_address(row.get("property_address") or "") == norm_address:
            source = row.get("source_type")
            if source and source not in matched_types:
                matched_types.append(source)

    return matched_types
