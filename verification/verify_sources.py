"""
verification/verify_sources.py — Gate 3: Daily source health check.

Runs at 6:45 AM EST, 15 minutes before the main pipeline.
If more than 2 sources are down simultaneously, pauses the full pipeline.

CLI:
    python verification/verify_sources.py
"""

import argparse
import time

import requests

from db.client import get_client, update_row, insert_row
from routing.notify import send_sms
from utils.logger import get_logger

log = get_logger("verification.verify_sources")

REQUEST_TIMEOUT_SEC = 15
MAX_BLOCKED_BEFORE_PAUSE = 2


def check_source(source: dict) -> dict:
    """Run a health check on a single source.

    Sends a test request to the source URL and confirms the expected
    HTML element or data field is present in the response.

    Returns a status dict with keys: status, response_time_ms, notes.
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
                "notes": f"HTTP {response.status_code}",
            }

        if expected_element and expected_element not in response.text:
            return {
                "status": "degraded",
                "response_time_ms": elapsed_ms,
                "notes": f"Expected element '{expected_element}' not found in response",
            }

        return {"status": "healthy", "response_time_ms": elapsed_ms, "notes": None}

    except requests.exceptions.Timeout:
        return {"status": "blocked", "response_time_ms": None, "notes": "Request timed out"}
    except requests.exceptions.ConnectionError as e:
        return {"status": "blocked", "response_time_ms": None, "notes": str(e)[:200]}
    except Exception as e:
        return {"status": "degraded", "response_time_ms": None, "notes": str(e)[:200]}


def run_health_check() -> dict:
    """Run health check on all active sources in the sources table.

    Returns a summary dict for the daily digest.
    """
    client = get_client()
    sources = client.table("sources").select("*").eq("blocked", False).execute().data or []

    log.info(f"Running health check on {len(sources)} sources")
    degraded_sources = []

    for source in sources:
        result = check_source(source)
        status = result["status"]
        source_name = source.get("source_name", "")

        updates = {
            "status": status,
            "last_checked_at": "now()",
            "response_time_ms": result.get("response_time_ms"),
            "retry_count_today": source.get("retry_count_today", 0),
        }

        if status == "healthy":
            updates["last_healthy_at"] = "now()"
            log.info(f"[OK] {source_name} — {result['response_time_ms']}ms")
        else:
            updates["blocked"] = status == "blocked"
            updates["needs_manual_review"] = True
            degraded_sources.append(source_name)
            log.warning(f"[{status.upper()}] {source_name} — {result.get('notes')}")

            send_sms(
                f"[SOURCE ALERT] {source_name} is not responding. "
                f"Leads from this source paused until resolved."
            )

            insert_row("maintenance_log", {
                "event_type": "source_blocked",
                "source_name": source_name,
                "description": f"Source {status}: {result.get('notes')}",
                "resolved": False,
            })

        update_row("sources", source["id"], updates)

    if len(degraded_sources) > MAX_BLOCKED_BEFORE_PAUSE:
        msg = (
            f"[PIPELINE ALERT] {len(degraded_sources)} sources are down simultaneously: "
            f"{', '.join(degraded_sources)}. Pipeline paused — manual restart required."
        )
        send_sms(msg)
        log.error(msg)

        insert_row("maintenance_log", {
            "event_type": "pipeline_paused",
            "source_name": None,
            "description": msg,
            "resolved": False,
        })

    return {
        "total_checked": len(sources),
        "healthy": len(sources) - len(degraded_sources),
        "degraded_or_blocked": len(degraded_sources),
        "degraded_names": degraded_sources,
        "pipeline_paused": len(degraded_sources) > MAX_BLOCKED_BEFORE_PAUSE,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run source health check")
    parser.parse_args()
    summary = run_health_check()
    log.info(
        f"Health check complete — "
        f"{summary['healthy']}/{summary['total_checked']} healthy, "
        f"{summary['degraded_or_blocked']} degraded/blocked"
    )
