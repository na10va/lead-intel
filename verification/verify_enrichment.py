"""
verification/verify_enrichment.py — Gate 2: Post-enrichment field validation.

Runs after enrichment completes. Only records where verified_enriched=True
proceed to scoring and routing.

CLI:
    python verification/verify_enrichment.py
"""

import argparse
import re

from db.client import get_client, update_row
from utils.logger import get_logger

log = get_logger("verification.verify_enrichment")

E164_PATTERN = re.compile(r"^\+1\d{10}$")


def verify_enriched_record(lead_id: str) -> bool:
    """Run Gate 2 verification on a single enriched raw_leads record.

    Validates phone format, address parseability, and financial data ranges.
    Sets verified_enriched=True only if all checks pass.

    Returns True if verification passes, False otherwise.
    """
    client = get_client()
    lead = client.table("raw_leads").select("*").eq("id", lead_id).single().execute().data

    if not lead:
        log.error(f"Lead {lead_id} not found")
        return False

    failures = []
    updates: dict = {}

    # Phone number validation
    phone_1 = lead.get("phone_1")
    if phone_1:
        if not E164_PATTERN.match(phone_1):
            failures.append(f"phone_1 not in E.164 format: {phone_1}")
    # phone_2 and phone_3 are optional — validate format if present
    for field in ["phone_2", "phone_3"]:
        val = lead.get(field)
        if val and not E164_PATTERN.match(val):
            log.warning(f"Lead {lead_id}: {field} not in E.164 format — clearing")
            updates[field] = None

    # Owner mailing address validation
    mailing_address = lead.get("owner_mailing_address")
    if mailing_address:
        import usaddress
        try:
            parsed, _ = usaddress.tag(mailing_address)
            # Check if mailing address is out-of-state
            state_in_address = parsed.get("StateName", "").upper()
            if state_in_address and state_in_address != lead.get("state", "").upper():
                updates["owner_out_of_state"] = True
                log.info(f"Lead {lead_id}: owner mailing address is out-of-state ({state_in_address})")
        except Exception:
            log.warning(f"Lead {lead_id}: could not parse mailing address: {mailing_address}")

    # Financial data validation
    estimated_value = lead.get("estimated_value")
    if estimated_value is not None:
        if estimated_value <= 10_000:
            failures.append(f"estimated_value ${estimated_value} is too low — likely an error")

    equity_pct = lead.get("estimated_equity_pct")
    if equity_pct is not None:
        if not (0 <= equity_pct <= 100):
            failures.append(f"estimated_equity_pct {equity_pct} is out of range 0–100")
    elif not lead.get("equity_unknown"):
        updates["equity_unknown"] = True
        log.info(f"Lead {lead_id}: equity data missing — setting equity_unknown=True")

    if failures:
        existing_notes = lead.get("verification_notes") or ""
        gate2_notes = "; ".join(failures)
        updates.update({
            "verified_enriched": False,
            "verification_notes": f"{existing_notes} | Gate2: {gate2_notes}".strip(" | "),
        })
        update_row("raw_leads", lead_id, updates)
        log.warning(f"Lead {lead_id} failed Gate 2: {gate2_notes}")
        return False

    updates["verified_enriched"] = True
    update_row("raw_leads", lead_id, updates)
    log.info(f"Lead {lead_id} passed Gate 2 verification")
    return True


def run_all_unenriched() -> None:
    """Run Gate 2 on all enriched-but-unverified leads.

    Catches both verified_enriched=False and verified_enriched IS NULL so
    newly enriched leads are always verified before scoring and routing.
    """
    client = get_client()
    response = (
        client.table("raw_leads")
        .select("id")
        .eq("enriched", True)
        .or_("verified_enriched.is.null,verified_enriched.eq.false")
        .execute()
    )
    leads = response.data or []
    log.info(f"Running Gate 2 on {len(leads)} enriched records")

    passed = failed = 0
    for row in leads:
        if verify_enriched_record(row["id"]):
            passed += 1
        else:
            failed += 1

    log.info(f"Gate 2 complete — {passed} passed, {failed} failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Gate 2 verification on enriched leads")
    parser.parse_args()
    run_all_unenriched()
