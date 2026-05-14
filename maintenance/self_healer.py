"""
maintenance/self_healer.py — Detects and attempts to auto-repair broken scrapers.

Trigger: Any scraper that returns 0 records for 2 consecutive runs,
         or throws an unhandled exception during FETCH or PARSE.

Auto-repair sequence:
    1. Log failure with full error details and last known working selector
    2. Re-fetch source URL and compare current HTML to last known structure
    3. Attempt to identify new selector automatically
    4. If fix found: apply, re-run agent, log self-repair in maintenance_log
    5. If no fix in 15 min: SMS owner, flag source as needs_manual_review=True

CLI:
    python maintenance/self_healer.py --source probate_cuyahoga
"""

import argparse
import time

from db.client import get_client, insert_row, update_row
from routing.notify import send_sms
from utils.logger import get_logger

log = get_logger("maintenance.self_healer")

REPAIR_TIMEOUT_SEC = 15 * 60  # 15 minutes


def check_consecutive_failures(source_name: str, threshold: int = 2) -> bool:
    """Return True if a source has had `threshold` consecutive zero-record runs."""
    client = get_client()
    response = (
        client.table("maintenance_log")
        .select("*")
        .eq("source_name", source_name)
        .eq("event_type", "self_heal")
        .order("created_at", desc=True)
        .limit(threshold)
        .execute()
    )
    logs = response.data or []
    if len(logs) < threshold:
        return False
    return all(not row.get("resolved") for row in logs[:threshold])


def attempt_repair(source_name: str) -> bool:
    """Attempt to auto-repair a broken scraper.

    TODO: Implement HTML structure diffing and selector auto-detection.
    Fetch the source URL fresh, compare to last known structure stored in sources table.
    If a new selector can be identified, update the agent file and re-run.

    Returns True if repair succeeded, False if manual review is needed.
    """
    log.info(f"Attempting auto-repair for source: {source_name}")
    start = time.time()

    try:
        # TODO: Implement Playwright fetch + HTML diff + selector detection
        raise NotImplementedError("Auto-repair not yet implemented")

    except NotImplementedError:
        log.warning(f"Auto-repair not yet implemented for {source_name}")
        return False

    except Exception as e:
        elapsed = time.time() - start
        if elapsed >= REPAIR_TIMEOUT_SEC:
            log.error(f"Auto-repair timed out for {source_name} after {REPAIR_TIMEOUT_SEC}s")
        else:
            log.error(f"Auto-repair failed for {source_name}: {e}")
        return False


def handle_failure(source_name: str, error: str) -> None:
    """Handle a scraper failure — attempt repair, escalate if needed."""
    log.warning(f"Scraper failure detected: {source_name} — {error}")

    insert_row("maintenance_log", {
        "event_type": "self_heal",
        "source_name": source_name,
        "description": f"Scraper failure: {error}",
        "resolved": False,
    })

    if not check_consecutive_failures(source_name):
        log.info(f"First failure for {source_name} — monitoring, not escalating yet")
        return

    repaired = attempt_repair(source_name)

    if repaired:
        insert_row("maintenance_log", {
            "event_type": "self_heal",
            "source_name": source_name,
            "description": "Auto-repair succeeded — scraper restored",
            "resolved": True,
        })
        log.info(f"Auto-repair succeeded for {source_name}")
        return

    # Escalate to owner
    client = get_client()
    sources = client.table("sources").select("id").eq("source_name", source_name).execute().data
    if sources:
        update_row("sources", sources[0]["id"], {"needs_manual_review": True, "blocked": True})

    send_sms(
        f"[SCRAPER ALERT] {source_name} has failed and could not be auto-repaired. "
        f"Manual review required."
    )

    insert_row("maintenance_log", {
        "event_type": "self_heal",
        "source_name": source_name,
        "description": f"Auto-repair failed — owner alerted, source flagged for manual review",
        "resolved": False,
    })

    log.error(f"Auto-repair failed for {source_name} — owner alerted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run self-healer for a specific source")
    parser.add_argument("--source", required=True, help="Source name (e.g. probate_cuyahoga)")
    args = parser.parse_args()
    handle_failure(args.source, "Manual test run")
