"""
maintenance/cost_watchdog.py — Pauses pipeline if daily API spend hits $50.

Runs after every enrichment batch. Checks total spend in api_costs for today.

Thresholds:
    $50/day  → pause enrichment pipeline + SMS owner
    $200/month → email warning (no pause)
    $75/month (Tracerfy only) → pause Tracerfy enrichment + SMS owner

CLI:
    python maintenance/cost_watchdog.py --check
"""

import argparse
import os
from datetime import date

from db.client import get_client, insert_row
from routing.notify import send_email, send_sms
from utils.logger import get_logger

log = get_logger("maintenance.cost_watchdog")

DAILY_LIMIT_USD = 50.0
MONTHLY_LIMIT_USD = 200.0
TRACERFY_MONTHLY_LIMIT_USD = 75.0

OWNER_EMAIL = os.getenv("OWNER_EMAIL")
OWNER_PHONE = os.getenv("OWNER_PHONE")

# Global pipeline pause flag — checked by waterfall.py before calling Skip Sherpa
_PIPELINE_PAUSED = False
_TRACERFY_PAUSED = False


def is_pipeline_paused() -> bool:
    """Return True if the cost watchdog has paused the enrichment pipeline."""
    return _PIPELINE_PAUSED


def is_tracerfy_paused() -> bool:
    """Return True if Tracerfy monthly spend has hit the $75 cap."""
    return _TRACERFY_PAUSED


def check_spend() -> dict:
    """Sum today's and this month's API spend and enforce thresholds.

    Returns a dict with daily_spend, monthly_spend, action_taken.
    """
    global _PIPELINE_PAUSED
    client = get_client()
    today = date.today()
    month_start = today.replace(day=1).isoformat()

    # Daily spend
    daily_rows = (
        client.table("api_costs")
        .select("cost_usd")
        .gte("called_at", today.isoformat())
        .execute()
        .data or []
    )
    daily_spend = sum(r.get("cost_usd", 0) for r in daily_rows)

    # Monthly spend
    monthly_rows = (
        client.table("api_costs")
        .select("cost_usd")
        .gte("called_at", month_start)
        .execute()
        .data or []
    )
    monthly_spend = sum(r.get("cost_usd", 0) for r in monthly_rows)

    # Tracerfy monthly spend
    tracerfy_rows = (
        client.table("api_costs")
        .select("cost_usd")
        .eq("service", "tracerfy")
        .gte("called_at", month_start)
        .execute()
        .data or []
    )
    tracerfy_monthly_spend = sum(r.get("cost_usd", 0) for r in tracerfy_rows)

    if tracerfy_monthly_spend >= TRACERFY_MONTHLY_LIMIT_USD and not _TRACERFY_PAUSED:
        _TRACERFY_PAUSED = True
        send_sms(
            f"[COST ALERT] Tracerfy monthly spend hit ${tracerfy_monthly_spend:.2f} "
            f"(limit ${TRACERFY_MONTHLY_LIMIT_USD:.0f}/month). "
            f"Tracerfy enrichment paused for the rest of the month."
        )
        insert_row("maintenance_log", {
            "event_type": "cost_alert",
            "source_name": "tracerfy",
            "description": (
                f"Tracerfy monthly spend ${tracerfy_monthly_spend:.2f} exceeded "
                f"${TRACERFY_MONTHLY_LIMIT_USD} limit. Tracerfy paused."
            ),
            "resolved": False,
        })
        log.error(f"COST ALERT: Tracerfy monthly spend ${tracerfy_monthly_spend:.2f} — Tracerfy paused")

    action_taken = "none"
    threshold_hit = None

    if daily_spend >= DAILY_LIMIT_USD:
        _PIPELINE_PAUSED = True
        action_taken = "pipeline_paused"
        threshold_hit = "daily_50"

        send_sms(
            f"[COST ALERT] Daily API spend hit ${daily_spend:.2f}. "
            f"Pipeline paused. Review api_costs table and restart manually with: "
            f"python scheduler/run_daily.py --resume"
        )

        insert_row("maintenance_log", {
            "event_type": "cost_alert",
            "source_name": None,
            "description": f"Daily spend ${daily_spend:.2f} exceeded ${DAILY_LIMIT_USD} limit. Pipeline paused.",
            "resolved": False,
        })

        log.error(f"COST ALERT: Daily spend ${daily_spend:.2f} — pipeline paused")

    elif monthly_spend >= MONTHLY_LIMIT_USD:
        action_taken = "alert_sent"
        threshold_hit = "monthly_200"

        send_email(
            to=OWNER_EMAIL,
            subject=f"[COST WARNING] Monthly API spend hit ${monthly_spend:.2f}",
            html_body=(
                f"<p>Monthly API spend has reached <strong>${monthly_spend:.2f}</strong> "
                f"against a ${MONTHLY_LIMIT_USD} budget.</p>"
                f"<p>Pipeline is still running. Review <code>api_costs</code> table "
                f"to identify the highest-volume service.</p>"
            ),
        )

        log.warning(f"Monthly spend ${monthly_spend:.2f} exceeds ${MONTHLY_LIMIT_USD} — email sent")

    else:
        log.debug(f"Spend OK — daily: ${daily_spend:.2f}, monthly: ${monthly_spend:.2f}")

    insert_row("cost_watchdog_log", {
        "daily_spend_usd": daily_spend,
        "monthly_spend_usd": monthly_spend,
        "threshold_hit": threshold_hit,
        "action_taken": action_taken,
    })

    return {
        "daily_spend": daily_spend,
        "monthly_spend": monthly_spend,
        "action_taken": action_taken,
        "pipeline_paused": _PIPELINE_PAUSED,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check API spend against thresholds")
    parser.add_argument("--check", action="store_true", help="Run watchdog check")
    args = parser.parse_args()

    if args.check:
        result = check_spend()
        print(
            f"Daily spend: ${result['daily_spend']:.2f} | "
            f"Monthly spend: ${result['monthly_spend']:.2f} | "
            f"Action: {result['action_taken']}"
        )
