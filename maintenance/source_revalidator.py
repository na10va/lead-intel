"""
maintenance/source_revalidator.py — Proactive source re-validation every 30 days.

Runs for every source — even healthy ones. Detects silent degradation before
it affects lead quality. County websites don't announce structural changes.

CLI:
    python maintenance/source_revalidator.py
"""

import time
from datetime import date, timedelta

import requests

from db.client import get_client, insert_row, update_row
from routing.notify import send_sms
from utils.logger import get_logger

log = get_logger("maintenance.source_revalidator")

REVALIDATION_INTERVAL_DAYS = 30
REQUEST_TIMEOUT_SEC = 15


def _is_due_for_revalidation(source: dict) -> bool:
    """Return True if the source hasn't been revalidated in 30 days."""
    last = source.get("last_revalidated_at")
    if not last:
        return True
    try:
        last_date = date.fromisoformat(last[:10])
        return (date.today() - last_date).days >= REVALIDATION_INTERVAL_DAYS
    except (ValueError, TypeError):
        return True


def revalidate_source(source: dict) -> dict:
    """Re-validate a single source URL and confirm data fields are accessible.

    Checks:
        1. URL is reachable (HTTP 200)
        2. Expected HTML element is still present
        3. No new login wall or CAPTCHA detected

    Returns a result dict with status and notes.
    """
    url = source.get("url", "")
    expected_element = source.get("expected_element")
    source_name = source.get("source_name", "")

    start = time.time()
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SEC)
        elapsed_ms = int((time.time() - start) * 1000)

        if response.status_code != 200:
            return {
                "status": "degraded",
                "response_time_ms": elapsed_ms,
                "structure_changed": False,
                "notes": f"HTTP {response.status_code}",
            }

        # Check for login walls and CAPTCHAs
        auth_indicators = ["login", "sign in", "captcha", "please verify", "access denied"]
        page_lower = response.text.lower()
        if any(indicator in page_lower for indicator in auth_indicators):
            return {
                "status": "auth_required",
                "response_time_ms": elapsed_ms,
                "structure_changed": True,
                "notes": "Possible login wall or CAPTCHA detected",
            }

        structure_changed = False
        if expected_element and expected_element not in response.text:
            structure_changed = True
            log.warning(f"{source_name}: expected element '{expected_element}' not found — structure may have changed")

        return {
            "status": "healthy",
            "response_time_ms": elapsed_ms,
            "structure_changed": structure_changed,
            "notes": "Structure changed — self-healer review recommended" if structure_changed else None,
        }

    except Exception as e:
        return {
            "status": "blocked",
            "response_time_ms": None,
            "structure_changed": False,
            "notes": str(e)[:200],
        }


def run_revalidation() -> dict:
    """Re-validate all sources that are due (every 30 days).

    Returns a summary dict for the monthly health report.
    """
    client = get_client()
    sources = client.table("sources").select("*").execute().data or []
    due = [s for s in sources if _is_due_for_revalidation(s)]

    log.info(f"Source revalidation: {len(due)} of {len(sources)} sources due")
    changed = auth_required = 0

    for source in due:
        result = revalidate_source(source)
        source_name = source.get("source_name", "")

        insert_row("source_validation_log", {
            "source_id": source["id"],
            "source_name": source_name,
            "status": result["status"],
            "structure_changed": result.get("structure_changed", False),
            "response_time_ms": result.get("response_time_ms"),
            "notes": result.get("notes"),
        })

        update_row("sources", source["id"], {
            "last_revalidated_at": "now()",
            "structure_changed": result.get("structure_changed", False),
        })

        if result["status"] == "auth_required":
            auth_required += 1
            send_sms(
                f"[SOURCE ALERT] {source_name} now requires authentication. "
                f"Manual review required."
            )
            log.error(f"{source_name}: auth required — owner alerted")

        elif result.get("structure_changed"):
            changed += 1
            log.warning(f"{source_name}: structure changed — flagged for self-healer review")

        else:
            log.info(f"[OK] {source_name} revalidated — {result.get('response_time_ms')}ms")

    return {
        "sources_checked": len(due),
        "structure_changed": changed,
        "auth_required": auth_required,
    }


if __name__ == "__main__":
    summary = run_revalidation()
    print(
        f"Revalidation complete — "
        f"{summary['sources_checked']} checked, "
        f"{summary['structure_changed']} changed, "
        f"{summary['auth_required']} auth required"
    )
