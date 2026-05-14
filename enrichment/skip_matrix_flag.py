from __future__ import annotations
"""
enrichment/skip_matrix_flag.py — Step 3: Flag Tier A leads for manual Skip Matrix run.

IMPORTANT: Do NOT call Skip Matrix API automatically — this is a manual-only service.
This module only flags records and adds them to the Skip Matrix queue Google Sheet.

Triggered for: Tier A leads where Steps 1 + 2 both failed to return a mobile number.

Owner receives a weekly email every Monday at 7 AM listing all leads needing Skip Matrix.
"""

import os

from db.client import get_client, update_row
from utils.logger import get_logger

log = get_logger("enrichment.skip_matrix_flag")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SKIP_MATRIX_TAB = os.getenv("SKIP_MATRIX_SHEET_TAB", "skip_matrix_queue")

SKIP_MATRIX_COLUMNS = [
    "id", "owner_name", "property_address", "county", "state",
    "source_type", "filing_date", "score", "tier", "created_at",
]


def flag_for_skip_matrix(lead_id: str) -> None:
    """Flag a Tier A lead for manual Skip Matrix enrichment.

    Sets skip_matrix_needed=True in raw_leads and appends the lead
    to the skip_matrix_queue Google Sheet tab.
    """
    update_row("raw_leads", lead_id, {"skip_matrix_needed": True})
    log.info(f"Lead {lead_id} flagged for Skip Matrix")

    try:
        _append_to_sheet(lead_id)
    except Exception as e:
        log.error(f"Failed to add lead {lead_id} to Skip Matrix sheet: {e}")


def _append_to_sheet(lead_id: str) -> None:
    """Append a lead row to the skip_matrix_queue Google Sheet tab.

    TODO: Implement gspread append using Application Default Credentials.
    Uses google.auth.default() — no JSON key needed (gcloud auth application-default login).
    """
    import google.auth
    import gspread

    client = get_client()
    lead = client.table("raw_leads").select(", ".join(SKIP_MATRIX_COLUMNS)).eq("id", lead_id).single().execute().data

    if not lead:
        log.error(f"Could not fetch lead {lead_id} for Skip Matrix sheet")
        return

    # TODO: Implement gspread auth and sheet append
    raise NotImplementedError("Google Sheets append not yet implemented")


def get_pending_queue() -> list[dict]:
    """Return all leads currently flagged for Skip Matrix.

    Used by the Monday morning weekly email to the owner.
    """
    client = get_client()
    response = (
        client.table("raw_leads")
        .select(", ".join(SKIP_MATRIX_COLUMNS))
        .eq("skip_matrix_needed", True)
        .order("created_at", desc=True)
        .execute()
    )
    return response.data or []


def get_pending_count() -> int:
    """Return the count of leads awaiting Skip Matrix enrichment."""
    return len(get_pending_queue())
