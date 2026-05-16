from __future__ import annotations
"""
enrichment/skip_matrix_flag.py — Step 3: Flag Tier A leads for manual Skip Matrix run.

IMPORTANT: Do NOT call Skip Matrix API automatically — this is a manual-only service.
This module only flags records and adds them to the Skip Matrix queue Google Sheet.

Triggered for: Tier A leads where Steps 1 + 2 both failed to return a mobile number.

Owner receives a weekly email every Monday at 7 AM listing all leads needing Skip Matrix.
"""

import os
from pathlib import Path

from db.client import get_client, update_row
from utils.logger import get_logger

log = get_logger("enrichment.skip_matrix_flag")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SKIP_MATRIX_TAB = os.getenv("SKIP_MATRIX_SHEET_TAB", "skip_matrix_queue")

_CREDS_PATH = Path(__file__).parent.parent / "credentials" / "google_service_account.json"

SKIP_MATRIX_COLUMNS = [
    "id", "owner_name", "owner_first_name", "owner_last_name",
    "property_address", "county", "state",
    "source_type", "filing_date", "score", "tier", "created_at",
]

_SHEET_HEADERS = [
    "Lead ID", "Owner Name", "First Name", "Last Name",
    "Property Address", "County", "State",
    "Source Type", "Filing Date", "Score", "Tier", "Ingested At",
]


def _get_worksheet():
    """Return the skip_matrix_queue worksheet, creating it if needed."""
    from google.oauth2.service_account import Credentials
    import gspread

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(str(_CREDS_PATH), scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)

    try:
        ws = spreadsheet.worksheet(SKIP_MATRIX_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SKIP_MATRIX_TAB, rows=2000, cols=len(_SHEET_HEADERS))
        ws.append_row(_SHEET_HEADERS, value_input_option="RAW")
        log.info(f"Created new worksheet: {SKIP_MATRIX_TAB}")

    # Add headers if sheet is empty
    if ws.row_count == 0 or not ws.row_values(1):
        ws.append_row(_SHEET_HEADERS, value_input_option="RAW")

    return ws


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
    """Append a lead row to the skip_matrix_queue Google Sheet tab."""
    if not GOOGLE_SHEET_ID:
        log.warning("GOOGLE_SHEET_ID not set — cannot append to Skip Matrix sheet")
        return
    if not _CREDS_PATH.exists():
        log.warning(f"Google credentials not found at {_CREDS_PATH}")
        return

    db = get_client()
    lead = (
        db.table("raw_leads")
        .select(", ".join(SKIP_MATRIX_COLUMNS))
        .eq("id", lead_id)
        .single()
        .execute()
        .data
    )

    if not lead:
        log.error(f"Could not fetch lead {lead_id} for Skip Matrix sheet")
        return

    row = [str(lead.get(col) or "") for col in SKIP_MATRIX_COLUMNS]

    ws = _get_worksheet()
    ws.append_row(row, value_input_option="RAW")
    log.info(f"Lead {lead_id} appended to Skip Matrix sheet ({SKIP_MATRIX_TAB})")


def get_pending_queue() -> list[dict]:
    """Return all leads currently flagged for Skip Matrix."""
    db = get_client()
    return (
        db.table("raw_leads")
        .select(", ".join(SKIP_MATRIX_COLUMNS))
        .eq("skip_matrix_needed", True)
        .order("score", desc=True)
        .execute()
        .data or []
    )


def get_pending_count() -> int:
    return len(get_pending_queue())


def send_weekly_skip_matrix_email() -> None:
    """Send the Monday 7 AM email to owner listing all leads needing Skip Matrix.

    Includes count, direct sheet link, and top 10 leads by score.
    """
    from routing.notify import send_email

    owner_email = os.getenv("OWNER_EMAIL")
    if not owner_email:
        log.warning("OWNER_EMAIL not set — cannot send Skip Matrix weekly email")
        return

    queue = get_pending_queue()
    count = len(queue)

    if count == 0:
        log.info("No leads in Skip Matrix queue — skipping weekly email")
        return

    sheet_url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"
    top_leads = queue[:10]

    rows = ""
    for lead in top_leads:
        rows += (
            f"<tr>"
            f"<td>{lead.get('owner_name', '')}</td>"
            f"<td>{lead.get('property_address', '')}</td>"
            f"<td>{lead.get('county', '')}</td>"
            f"<td>{lead.get('source_type', '')}</td>"
            f"<td><strong>{lead.get('score', '')}</strong></td>"
            f"<td>{lead.get('filing_date', '')}</td>"
            f"</tr>"
        )

    body = f"""
<h2>Skip Matrix Queue — {count} Tier A Lead(s) Need Manual Enrichment</h2>
<p>These leads scored Tier A but couldn't be phone-traced by Skip Sherpa.
Run them through <strong>Skip Matrix</strong> manually to find mobile numbers.</p>
<p><a href="{sheet_url}" style="font-size:16px;font-weight:bold;">
  📋 Download Full List from Google Sheet →
</a></p>
<h3>Top {len(top_leads)} by Score</h3>
<table border="1" cellpadding="6">
<thead>
<tr>
  <th>Owner</th><th>Address</th><th>County</th>
  <th>Source</th><th>Score</th><th>Filing Date</th>
</tr>
</thead>
<tbody>{rows}</tbody>
</table>
<p style="color:#999;font-size:12px;">
After uploading results back, the pipeline will re-enrich and re-score automatically.
</p>
"""

    result = send_email(
        to=owner_email,
        subject=f"[Skip Matrix] {count} Tier A lead(s) need phone tracing — week of {_monday_date()}",
        html_body=body,
    )
    if result:
        log.info(f"Skip Matrix weekly email sent — {count} leads in queue")


def _monday_date() -> str:
    from datetime import date
    return date.today().isoformat()
