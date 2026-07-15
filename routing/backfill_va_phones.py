"""
routing/backfill_va_phones.py — Backfill phone numbers into existing VA sheet rows
that were routed before enrichment completed.

Reads all rows from the va_queue tab, finds any where phone_1 is blank,
matches them to enriched leads in Supabase by owner_name + property_address,
and writes the phone/email columns in a single batch update.

Usage:
    python routing/backfill_va_phones.py [--dry-run]
"""

import argparse
import os

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
import gspread

from db.client import get_client
from utils.logger import get_logger

load_dotenv()

log = get_logger("routing.backfill_va_phones")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
VA_QUEUE_TAB    = os.getenv("VA_QUEUE_SHEET_TAB", "va_queue")
_SA_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/google_service_account.json")
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column indices in the VA sheet (0-based, matching VA_COLUMNS order)
COL_OWNER_NAME  = 0
COL_PROPERTY    = 3
COL_PHONE_1     = 6
COL_PHONE_1_DNC = 7
COL_PHONE_2     = 8
COL_PHONE_2_DNC = 9
COL_PHONE_3     = 10
COL_EMAIL       = 11


def _col_letter(idx: int) -> str:
    """Convert 0-based column index to sheet column letter (A, B, ..., Z, AA, ...)."""
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def run(dry_run: bool = False) -> None:
    creds = Credentials.from_service_account_file(_SA_FILE, scopes=_SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    ws = sh.worksheet(VA_QUEUE_TAB)

    all_rows = ws.get_all_values()
    if not all_rows:
        log.info("VA sheet is empty — nothing to backfill")
        return

    header = all_rows[0]
    data_rows = all_rows[1:]  # row index 0 = header, data starts at spreadsheet row 2

    # Find rows where phone_1 cell is blank
    rows_needing_phones = []
    for i, row in enumerate(data_rows):
        # Pad short rows so index access is safe
        while len(row) <= COL_PHONE_1:
            row.append("")
        if not row[COL_PHONE_1].strip():
            sheet_row_num = i + 2  # 1-based, +1 for header
            owner = row[COL_OWNER_NAME].strip()
            address = row[COL_PROPERTY].strip()
            if owner:
                rows_needing_phones.append((sheet_row_num, owner, address))

    log.info(f"VA sheet: {len(data_rows)} data rows, {len(rows_needing_phones)} missing phone_1")

    if not rows_needing_phones:
        log.info("All rows already have phone_1 — nothing to backfill")
        return

    # Fetch all enriched Tier A/B leads with phones from Supabase
    client = get_client()
    db_leads = []
    offset = 0
    while True:
        batch = (
            client.table("raw_leads")
            .select("owner_name,property_address,phone_1,phone_1_dnc,phone_2,phone_2_dnc,phone_3,owner_email")
            .eq("enriched", True)
            .eq("routed_to_va", True)
            .in_("tier", ["A", "B"])
            .not_.is_("phone_1", "null")
            .range(offset, offset + 999)
            .execute()
            .data or []
        )
        if not batch:
            break
        db_leads.extend(batch)
        offset += 1000
        if len(batch) < 1000:
            break

    log.info(f"DB: {len(db_leads)} enriched Tier A/B leads with phone_1")

    # Build lookup by normalised owner_name → lead
    def _norm(s: str) -> str:
        return " ".join((s or "").upper().split())

    db_by_name: dict[str, dict] = {}
    for lead in db_leads:
        key = _norm(lead.get("owner_name") or "")
        if key:
            db_by_name[key] = lead

    # Also build address lookup as fallback
    db_by_addr: dict[str, dict] = {}
    for lead in db_leads:
        key = _norm(lead.get("property_address") or "")
        if key:
            db_by_addr[key] = lead

    # Match sheet rows to DB leads and build batch update
    updates: list[dict] = []  # {row: int, phone_1: str, ...}
    matched = 0
    unmatched = 0

    for sheet_row, owner, address in rows_needing_phones:
        lead = db_by_name.get(_norm(owner)) or db_by_addr.get(_norm(address))
        if not lead:
            log.debug(f"Row {sheet_row}: no DB match for '{owner}' / '{address}'")
            unmatched += 1
            continue

        matched += 1
        updates.append({
            "row":         sheet_row,
            "phone_1":     lead.get("phone_1") or "",
            "phone_1_dnc": "TRUE" if lead.get("phone_1_dnc") else "",
            "phone_2":     lead.get("phone_2") or "",
            "phone_2_dnc": "TRUE" if lead.get("phone_2_dnc") else "",
            "phone_3":     lead.get("phone_3") or "",
            "owner_email": lead.get("owner_email") or "",
        })

    log.info(f"Matched {matched} rows, unmatched {unmatched}")

    if not updates:
        log.info("No updates to apply")
        return

    if dry_run:
        log.info("Dry run — would update:")
        for u in updates[:10]:
            log.info(f"  row {u['row']}: phone_1={u['phone_1']!r} phone_2={u['phone_2']!r}")
        if len(updates) > 10:
            log.info(f"  ... and {len(updates) - 10} more")
        return

    # Build gspread batch_update payload
    phone_cols = [COL_PHONE_1, COL_PHONE_1_DNC, COL_PHONE_2, COL_PHONE_2_DNC, COL_PHONE_3, COL_EMAIL]
    phone_fields = ["phone_1", "phone_1_dnc", "phone_2", "phone_2_dnc", "phone_3", "owner_email"]

    cell_updates = []
    for u in updates:
        for col_idx, field in zip(phone_cols, phone_fields):
            col_letter = _col_letter(col_idx)
            cell_updates.append({
                "range": f"{col_letter}{u['row']}",
                "values": [[u[field]]],
            })

    # gspread batch_update accepts up to 500 ranges per call
    BATCH_SIZE = 500
    for i in range(0, len(cell_updates), BATCH_SIZE):
        ws.batch_update(cell_updates[i:i + BATCH_SIZE], value_input_option="USER_ENTERED")
        log.info(f"Wrote batch {i // BATCH_SIZE + 1} ({min(i + BATCH_SIZE, len(cell_updates))} cells so far)")

    log.info(f"Backfill complete — {matched} rows updated in VA sheet")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill phone numbers into VA sheet rows")
    parser.add_argument("--dry-run", action="store_true", help="Preview matches without writing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
