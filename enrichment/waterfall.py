"""
enrichment/waterfall.py — Master enrichment controller.

Runs Step 1 → 2 → 3 in strict order. Escalates to the next step only
when the previous step fails to return a verified mobile phone number.
Never skip steps. Never run Step 2 or 3 if Step 1 already returned a valid mobile.

Step 1: Free Ohio public sources (county auditor, USPS, Ohio SOS)
Step 2: Skip Sherpa API (pay-per-call — only if Step 1 found no mobile)
Step 3: Skip Matrix flag (manual only — only for Tier A where Steps 1+2 failed)
"""

from db.client import get_client, update_row
from utils.logger import get_logger

log = get_logger("enrichment.waterfall")


def enrich_lead(lead_id: str) -> bool:
    """Run the full enrichment waterfall for a single lead.

    Args:
        lead_id: UUID of the raw_leads row to enrich.

    Returns True if enrichment produced a verified mobile number.
    Returns False if all steps failed (lead is flagged for Skip Matrix if Tier A).
    """
    from enrichment.public_sources import run_public_sources
    from enrichment.skip_matrix_flag import flag_for_skip_matrix

    client = get_client()
    lead = client.table("raw_leads").select("*").eq("id", lead_id).single().execute().data

    if not lead:
        log.error(f"Lead {lead_id} not found in raw_leads")
        return False

    if lead.get("enriched"):
        log.debug(f"Lead {lead_id} already enriched — skipping")
        return bool(lead.get("phone_1"))

    # Step 1 — Free Ohio public sources
    log.info(f"Enrichment Step 1 (public sources) for lead {lead_id}")
    step1_result = run_public_sources(lead)

    if step1_result.get("mobile_found"):
        _apply_enrichment(lead_id, step1_result, step=1)
        log.info(f"Lead {lead_id} enriched at Step 1 — mobile found")
        return True

    _apply_enrichment(lead_id, step1_result, step=1, partial=True)

    # Step 2 — Skip tracing (Tracerfy preferred at $0.02/hit; Skip Sherpa as fallback)
    from enrichment.skip_sherpa import SKIP_SHERPA_AVAILABLE, run_single as run_skip_sherpa
    from enrichment.tracerfy import TRACERFY_AVAILABLE, run_tracerfy

    step2_result = None

    from maintenance.cost_watchdog import is_tracerfy_paused
    tracerfy_ok = TRACERFY_AVAILABLE and not is_tracerfy_paused()

    if tracerfy_ok:
        log.info(f"Enrichment Step 2 (Tracerfy) for lead {lead_id}")
        step2_result = run_tracerfy(lead)
        # If Tracerfy hit a provider-level error (402/500/network), fall through to
        # Skip Sherpa rather than treating it as a genuine no-match.
        if step2_result.get("provider_error") and SKIP_SHERPA_AVAILABLE:
            log.warning(f"Tracerfy provider error — falling back to Skip Sherpa for lead {lead_id}")
            step2_result = run_skip_sherpa(lead)
    elif TRACERFY_AVAILABLE and is_tracerfy_paused():
        log.warning(f"Tracerfy paused (monthly cap hit) — falling back to Skip Sherpa for lead {lead_id}")
        if SKIP_SHERPA_AVAILABLE:
            step2_result = run_skip_sherpa(lead)
    elif SKIP_SHERPA_AVAILABLE:
        log.info(f"Enrichment Step 2 (Skip Sherpa) for lead {lead_id}")
        step2_result = run_skip_sherpa(lead)
    else:
        log.warning(f"No Step 2 provider configured — skipping skip trace for lead {lead_id}")

    if step2_result and step2_result.get("mobile_found"):
        _apply_enrichment(lead_id, step2_result, step=2)
        log.info(f"Lead {lead_id} enriched at Step 2 — mobile found")
        return True

    # Step 3 — Skip Matrix flag (Tier A only, manual)
    lead_tier = lead.get("tier")
    if lead_tier == "A":
        log.info(f"Lead {lead_id} is Tier A with no mobile — flagging for Skip Matrix")
        flag_for_skip_matrix(lead_id)

    update_row("raw_leads", lead_id, {
        "enriched":            True,
        "enriched_at":         "now()",
        "no_mobile_exhausted": True,
        "verification_notes":  (
            (lead.get("verification_notes") or "") +
            " | Enrichment: no mobile found after Steps 1+2"
        ).strip(" | "),
    })

    # Write to no_mobile_queue Google Sheet tab
    try:
        from routing.va_router import route_no_mobile_lead
        # Re-fetch lead to include any enrichment data written by Step 1
        refreshed = client.table("raw_leads").select("*").eq("id", lead_id).single().execute().data
        route_no_mobile_lead(refreshed or lead)
    except Exception as e:
        log.warning(f"Could not write lead {lead_id} to no_mobile_queue sheet: {e}")

    log.warning(f"Lead {lead_id} — no mobile found after all enrichment steps")
    return False


def _apply_enrichment(lead_id: str, result: dict, step: int, partial: bool = False) -> None:
    """Write enrichment results to raw_leads and trigger Gate 2 verification."""
    from verification.verify_enrichment import verify_enriched_record

    updates = {
        "enriched": True,
        "enrichment_step": step,
        "enriched_at": "now()",
    }

    for field in ["phone_1", "phone_2", "phone_3", "owner_email",
                  "owner_mailing_address", "owner_out_of_state",
                  "estimated_value", "estimated_equity_pct",
                  "last_sale_date", "last_sale_price"]:
        if result.get(field) is not None:
            updates[field] = result[field]

    if result.get("equity_unknown"):
        updates["equity_unknown"] = True

    update_row("raw_leads", lead_id, updates)

    if not partial:
        verify_enriched_record(lead_id)
