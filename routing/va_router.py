"""
routing/va_router.py — Routes Tier A and B leads to the VA queue Google Sheet.

VA queue columns:
    owner_name, property_address, county, state, phone_1, phone_2, phone_3,
    owner_email, score, tier, source_type, filing_date, created_at

Tier A: email to owner + VA queue (same day); summary SMS sent by agent after run
Tier B: email to owner + VA queue (within 48 hours)
Tier C/D: Never routed here.

Auth: Service account JSON key at GOOGLE_SERVICE_ACCOUNT_FILE (.env).
Sheet must be shared with the service account email as Editor.
"""

import os
from typing import Optional

from google.oauth2.service_account import Credentials
import gspread

from db.client import get_client, update_row
from routing.notify import send_email
from utils.logger import get_logger

log = get_logger("routing.va_router")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
VA_QUEUE_TAB    = os.getenv("VA_QUEUE_SHEET_TAB", "va_queue")
DNC_REVIEW_TAB  = os.getenv("DNC_REVIEW_SHEET_TAB", "dnc_review")
OWNER_EMAIL = os.getenv("OWNER_EMAIL")
_SA_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/google_service_account.json")
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_gc: Optional[gspread.Client] = None
# Tracks which sheet tabs have already had their header row confirmed this session.
# Prevents re-reading the full sheet on every append (avoids Sheets read quota).
_headers_confirmed: set = set()


def _get_gspread_client() -> gspread.Client:
    """Return a cached gspread client authenticated via service account."""
    global _gc
    if _gc is None:
        creds = Credentials.from_service_account_file(_SA_FILE, scopes=_SCOPES)
        _gc = gspread.authorize(creds)
    return _gc

VA_COLUMNS = [
    "owner_name", "api_owner_first_name", "api_owner_last_name",
    "property_address", "county", "state",
    "phone_1", "phone_1_dnc", "phone_2", "phone_2_dnc", "phone_3",
    "owner_email", "score", "tier", "source_type", "filing_date", "created_at",
]

DNC_REVIEW_COLUMNS = [
    "owner_name", "api_owner_first_name", "api_owner_last_name",
    "property_address", "county", "state",
    "phone_1", "phone_1_dnc", "phone_2", "phone_2_dnc", "phone_3",
    "owner_email", "score", "tier", "source_type", "filing_date", "created_at",
    "dnc_review_note",
]


def route_lead(lead_id: str, tier: str, notify: bool = True) -> bool:
    """Route a Tier A or B lead to the VA queue and alert the owner.

    Args:
        lead_id: UUID of the raw_leads row.
        tier:    "A" or "B"
        notify:  If False, skip individual email/SMS alerts (use for bulk backlog runs).

    Returns True if routing succeeded (VA sheet written).
    """
    if tier not in ("A", "B"):
        log.warning(f"route_lead called with tier={tier} — only A/B are routed")
        return False

    client = get_client()
    lead = client.table("raw_leads").select("*").eq("id", lead_id).single().execute().data

    if not lead:
        log.error(f"Lead {lead_id} not found")
        return False

    # If every phone we have is on the DNC list, hold for manual review instead
    # of routing to the VA. The VA must not call registered DNC numbers without
    # prior business relationship (FCC rule).
    p1_dnc = lead.get("phone_1_dnc")
    p2_dnc = lead.get("phone_2_dnc")
    has_phone = lead.get("phone_1") or lead.get("phone_2")
    all_dnc = has_phone and (p1_dnc is True) and (p2_dnc is True or not lead.get("phone_2"))

    if all_dnc:
        log.warning(
            f"Lead {lead_id} Tier {tier}: all phones on DNC — routing to dnc_review tab, not VA queue"
        )
        try:
            _append_to_dnc_review_sheet(lead)
        except Exception as e:
            log.error(f"Failed to write lead {lead_id} to DNC review sheet: {e}")
        if notify:
            _send_dnc_review_email(lead, tier)
        update_row("raw_leads", lead_id, {
            "routed_to_va": False,
            "alerted":      not notify,  # mark alerted only if we actually notified
            "routed_at":    "now()",
        })
        return True

    success = True

    # Write to VA Google Sheet
    try:
        _append_to_va_sheet(lead)
    except Exception as e:
        log.error(f"Failed to write lead {lead_id} to VA sheet: {e}")
        success = False

    # Send individual email/SMS only when notify=True (skipped for bulk backlog runs)
    if notify:
        if tier == "A":
            email_ok = _send_tier_a_email(lead)
            success = success and email_ok
        elif tier == "B":
            email_ok = _send_tier_b_email(lead)
            success = success and email_ok

    if success:
        update_row("raw_leads", lead_id, {
            "routed_to_va": True,
            "alerted":      notify,
            "routed_at":    "now()",
        })
        log.info(f"Lead {lead_id} routed — Tier {tier}")
    else:
        log.warning(f"Lead {lead_id} routing partially failed — routed_to_va not set")

    return success


def _get_or_init_worksheet(tab: str, columns: list) -> gspread.Worksheet:
    """Return the worksheet for `tab`, creating it and writing the header if needed.

    Header check is done at most once per tab per process run — subsequent calls
    skip the `get_all_values()` read to stay within Google Sheets read quota.
    """
    global _headers_confirmed
    gc = _get_gspread_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)

    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=5000, cols=len(columns))

    if tab not in _headers_confirmed:
        existing = ws.get_all_values()
        has_content = any(any(cell for cell in row) for row in existing)
        has_header = has_content and existing[0] == columns
        if not has_header:
            if not has_content:
                ws.append_row(columns, value_input_option="RAW")
            else:
                ws.insert_row(columns, index=1, value_input_option="RAW")
        _headers_confirmed.add(tab)

    return ws


def route_backlog(leads: list[dict]) -> tuple[int, int]:
    """Bulk-route a list of enriched lead dicts to the VA sheet. No individual emails/SMS.

    Respects DNC rules: leads where all phones are DNC go to dnc_review tab instead.
    Marks routed_to_va=True (or False+routed_at for DNC holds) in bulk.

    Returns (routed_count, dnc_held_count).
    """
    client = get_client()
    va_leads, dnc_leads = [], []

    for lead in leads:
        tier = lead.get("tier")
        if tier not in ("A", "B", "C"):
            continue
        p1_dnc = lead.get("phone_1_dnc")
        p2_dnc = lead.get("phone_2_dnc")
        has_phone = lead.get("phone_1") or lead.get("phone_2")
        all_dnc = has_phone and (p1_dnc is True) and (p2_dnc is True or not lead.get("phone_2"))
        if all_dnc:
            dnc_leads.append(lead)
        else:
            va_leads.append(lead)

    if va_leads:
        _append_many_to_va_sheet(va_leads)
        ids = [l["id"] for l in va_leads]
        for i in range(0, len(ids), 100):
            client.table("raw_leads").update(
                {"routed_to_va": True, "routed_at": "now()"}
            ).in_("id", ids[i:i+100]).execute()
        log.info(f"Bulk routed {len(va_leads)} leads to VA sheet")

    if dnc_leads:
        ws = _get_or_init_worksheet(DNC_REVIEW_TAB, DNC_REVIEW_COLUMNS)
        rows = []
        for lead in dnc_leads:
            row_data = {col: str(lead.get(col) or "") for col in DNC_REVIEW_COLUMNS}
            row_data["dnc_review_note"] = "All phones on DNC registry — manual review required"
            rows.append([row_data[col] for col in DNC_REVIEW_COLUMNS])
        for i in range(0, len(rows), 500):
            ws.append_rows(rows[i:i+500], value_input_option="USER_ENTERED")
        ids = [l["id"] for l in dnc_leads]
        for i in range(0, len(ids), 100):
            client.table("raw_leads").update(
                {"routed_to_va": True, "routed_at": "now()"}
            ).in_("id", ids[i:i+100]).execute()
        log.warning(f"{len(dnc_leads)} leads held in DNC review tab")

    return len(va_leads), len(dnc_leads)


def _append_to_va_sheet(lead: dict) -> None:
    """Append one lead row to the va_queue tab (single-lead path for daily operations)."""
    ws = _get_or_init_worksheet(VA_QUEUE_TAB, VA_COLUMNS)
    row = [str(lead.get(col) or "") for col in VA_COLUMNS]
    ws.append_rows([row], value_input_option="USER_ENTERED")


def _append_many_to_va_sheet(leads: list[dict]) -> None:
    """Bulk-append multiple leads to the va_queue tab in one API call.

    Use this for backlog imports. Avoids per-row Sheets read quota exhaustion.
    Writes in chunks of 500 rows to stay within Sheets payload limits.
    """
    ws = _get_or_init_worksheet(VA_QUEUE_TAB, VA_COLUMNS)
    rows = [[str(lead.get(col) or "") for col in VA_COLUMNS] for lead in leads]
    chunk_size = 500
    for i in range(0, len(rows), chunk_size):
        ws.append_rows(rows[i:i + chunk_size], value_input_option="USER_ENTERED")
        log.info(f"Wrote rows {i+1}–{min(i+chunk_size, len(rows))} of {len(rows)} to VA sheet")


def _append_to_dnc_review_sheet(lead: dict) -> None:
    """Append one lead to the dnc_review tab with a DNC warning note."""
    ws = _get_or_init_worksheet(DNC_REVIEW_TAB, DNC_REVIEW_COLUMNS)
    row_data = {col: str(lead.get(col) or "") for col in DNC_REVIEW_COLUMNS}
    row_data["dnc_review_note"] = "All phones on DNC registry — manual review required before VA contact"
    ws.append_rows([[row_data[col] for col in DNC_REVIEW_COLUMNS]], value_input_option="USER_ENTERED")


def _send_dnc_review_email(lead: dict, tier: str) -> bool:
    """Alert the owner that a Tier A/B lead is held for DNC review."""
    owner = lead.get("owner_name") or (
        " ".join(filter(None, [lead.get("api_owner_first_name"), lead.get("api_owner_last_name")]))
    ) or "Unknown"
    subject = (
        f"[DNC REVIEW] Tier {tier} lead held — {owner} — "
        f"{lead.get('county', '')}, {lead.get('state', '')} — Score {lead.get('score', '?')}"
    )
    phones = " | ".join(filter(None, [lead.get("phone_1"), lead.get("phone_2"), lead.get("phone_3")]))
    body = f"""
<h2>Tier {tier} Lead — DNC Review Required</h2>
<p>This lead scored Tier {tier} but all skip-traced phone numbers are registered on the
national DNC list. It has been placed in the <strong>dnc_review</strong> tab for your
review and has <strong>not</strong> been sent to the VA queue.</p>
<table border="1" cellpadding="6">
<tr><td>Owner (DB)</td><td>{lead.get('owner_name', '')}</td></tr>
<tr><td>Owner (Skip Sherpa)</td><td>{lead.get('api_owner_first_name', '')} {lead.get('api_owner_last_name', '')}</td></tr>
<tr><td>Address</td><td>{lead.get('property_address', '')}</td></tr>
<tr><td>County / State</td><td>{lead.get('county', '')}, {lead.get('state', '')}</td></tr>
<tr><td>Phone(s)</td><td>{phones or 'Not found'}</td></tr>
<tr><td>Phone 1 DNC</td><td>{lead.get('phone_1_dnc', '')}</td></tr>
<tr><td>Phone 2 DNC</td><td>{lead.get('phone_2_dnc', '')}</td></tr>
<tr><td>Email</td><td>{lead.get('owner_email') or 'Not found'}</td></tr>
<tr><td>Score</td><td>{lead.get('score', '?')} — Tier {tier}</td></tr>
<tr><td>Source</td><td>{lead.get('source_type', '')} / {lead.get('source_name', '')}</td></tr>
<tr><td>Filing Date</td><td>{lead.get('filing_date', '')}</td></tr>
</table>
<p><em>Review this lead manually before any outreach. Do not forward to the VA.</em></p>
"""
    return send_email(to=OWNER_EMAIL, subject=subject, html_body=body)


def _send_tier_a_email(lead: dict) -> bool:
    """Send full Tier A lead details email to the owner."""
    subject = (
        f"[TIER A LEAD] {lead.get('owner_name', 'Unknown')} — "
        f"{lead.get('county', '')}, {lead.get('state', '')} — Score {lead.get('score', '?')}"
    )
    body = _build_lead_email(lead, tier="A")
    return send_email(to=OWNER_EMAIL, subject=subject, html_body=body)


def _send_tier_b_email(lead: dict) -> bool:
    """Send full Tier B lead details email to the owner."""
    subject = (
        f"[TIER B LEAD] {lead.get('owner_name', 'Unknown')} — "
        f"{lead.get('county', '')}, {lead.get('state', '')} — Score {lead.get('score', '?')}"
    )
    body = _build_lead_email(lead, tier="B")
    return send_email(to=OWNER_EMAIL, subject=subject, html_body=body)


def _build_lead_email(lead: dict, tier: str) -> str:
    """Build HTML email body with full lead details."""
    api_owner = " ".join(filter(None, [
        lead.get("api_owner_first_name"), lead.get("api_owner_last_name")
    ]))

    def _phone_row(num_key: str, dnc_key: str, label: str) -> str:
        num = lead.get(num_key) or ""
        if not num:
            return ""
        dnc = lead.get(dnc_key)
        flag = " <strong style='color:red'>[DNC]</strong>" if dnc else ""
        return f"<tr><td>{label}</td><td>{num}{flag}</td></tr>"

    return f"""
<h2>Tier {tier} Lead — {lead.get('source_type', '').replace('_', ' ').title()}</h2>
<table border="1" cellpadding="6">
<tr><td>Owner (DB)</td><td>{lead.get('owner_name', 'Unknown')}</td></tr>
{'<tr><td>Owner (Skip Sherpa)</td><td>' + api_owner + '</td></tr>' if api_owner else ''}
<tr><td>Address</td><td>{lead.get('property_address', '')}</td></tr>
<tr><td>County / State</td><td>{lead.get('county', '')}, {lead.get('state', '')}</td></tr>
{_phone_row('phone_1', 'phone_1_dnc', 'Phone 1')}
{_phone_row('phone_2', 'phone_2_dnc', 'Phone 2')}
{_phone_row('phone_3', None, 'Phone 3')}
<tr><td>Email</td><td>{lead.get('owner_email') or 'Not found'}</td></tr>
<tr><td>Filing Date</td><td>{lead.get('filing_date', '')}</td></tr>
<tr><td>Source</td><td>{lead.get('source_name', '')}</td></tr>
<tr><td>Score</td><td>{lead.get('score', '?')} (Distress: {lead.get('distress_score', '?')} | Deal: {lead.get('deal_score', '?')})</td></tr>
<tr><td>Tier</td><td>{tier}</td></tr>
<tr><td>Estimated Value</td><td>${lead.get('estimated_value', 0):,}</td></tr>
<tr><td>Estimated Equity</td><td>{lead.get('estimated_equity_pct', 'Unknown')}%</td></tr>
</table>
"""
