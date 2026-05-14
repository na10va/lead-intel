"""
verification/daily_report.py — Sends the owner's daily verification digest.

Runs at 8:00 AM EST after the pipeline completes.
Always sends — even if the pipeline partially failed.

CLI:
    python verification/daily_report.py          # send today's real digest
    python verification/daily_report.py --test   # send test email with sample data
"""

import argparse
import os
from datetime import date

from db.client import get_client
from routing.notify import send_email
from utils.logger import get_logger

log = get_logger("verification.daily_report")

OWNER_EMAIL = os.getenv("OWNER_EMAIL")


def _gather_stats(run_date: date) -> dict:
    """Query Supabase for today's pipeline statistics."""
    client = get_client()
    today = run_date.isoformat()

    def count(filters: dict) -> int:
        q = client.table("raw_leads").select("id", count="exact")
        for col, val in filters.items():
            q = q.eq(col, val)
        q = q.gte("created_at", today)
        return q.execute().count or 0

    def count_tier_d(source_type: str) -> int:
        return (
            client.table("raw_leads")
            .select("id", count="exact")
            .eq("source_type", source_type)
            .gte("created_at", today)
            .execute()
            .count or 0
        )

    sources = client.table("sources").select("*").execute().data or []
    healthy = [s for s in sources if s.get("status") == "healthy"]
    degraded = [s for s in sources if s.get("status") != "healthy"]

    api_costs = (
        client.table("api_costs")
        .select("cost_usd")
        .gte("called_at", today)
        .execute()
        .data or []
    )
    total_cost = sum(row.get("cost_usd", 0) for row in api_costs)

    return {
        "run_date": run_date.strftime("%B %d, %Y"),
        "sources_checked": len(sources),
        "sources_healthy": len(healthy),
        "sources_degraded": len(degraded),
        "degraded_names": [s["source_name"] for s in degraded],
        "raw_scraped": count({}),
        "failed_gate1": count({"verified_raw": False}),
        "failed_gate2": count({"verified_enriched": False, "enriched": True}),
        "passed_both": count({"verified_raw": True, "verified_enriched": True}),
        "tier_a_routed": count({"tier": "A", "routed_to_va": True}),
        "tier_b_routed": count({"tier": "B", "routed_to_va": True}),
        "tier_c_stored": count({"tier": "C"}),
        "tier_d_divorce": count_tier_d("divorce"),
        "tier_d_eviction": count_tier_d("eviction"),
        "tier_d_bankruptcy": count_tier_d("bankruptcy"),
        "tier_d_fsbo": count_tier_d("fsbo"),
        "api_cost_usd": round(total_cost, 2),
    }


def _build_email_body(stats: dict) -> str:
    """Build the HTML email body for the daily digest."""
    degraded_list = (
        "<br>".join(f"• {n}" for n in stats["degraded_names"])
        if stats["degraded_names"] else "None"
    )

    return f"""
<h2>Lead Intel — Daily Digest ({stats['run_date']})</h2>

<h3>Source Health</h3>
<table border="1" cellpadding="6">
<tr><td>Sources checked</td><td>{stats['sources_checked']}</td></tr>
<tr><td>Sources healthy</td><td>{stats['sources_healthy']}</td></tr>
<tr><td>Sources degraded/blocked</td><td>{stats['sources_degraded']}<br>{degraded_list}</td></tr>
</table>

<h3>Pipeline Results</h3>
<table border="1" cellpadding="6">
<tr><td>Raw records scraped</td><td>{stats['raw_scraped']}</td></tr>
<tr><td>Failed Gate 1 (post-scrape)</td><td>{stats['failed_gate1']}</td></tr>
<tr><td>Failed Gate 2 (post-enrichment)</td><td>{stats['failed_gate2']}</td></tr>
<tr><td>Passed both gates</td><td><strong>{stats['passed_both']}</strong></td></tr>
</table>

<h3>Lead Routing</h3>
<table border="1" cellpadding="6">
<tr><td>Tier A leads routed</td><td>{stats['tier_a_routed']}</td></tr>
<tr><td>Tier B leads routed</td><td>{stats['tier_b_routed']}</td></tr>
<tr><td>Tier C leads stored</td><td>{stats['tier_c_stored']}</td></tr>
<tr><td>Tier D — Divorce</td><td>{stats['tier_d_divorce']}</td></tr>
<tr><td>Tier D — Eviction</td><td>{stats['tier_d_eviction']}</td></tr>
<tr><td>Tier D — Bankruptcy</td><td>{stats['tier_d_bankruptcy']}</td></tr>
<tr><td>Tier D — FSBO</td><td>{stats['tier_d_fsbo']}</td></tr>
</table>

<h3>Costs</h3>
<table border="1" cellpadding="6">
<tr><td>Estimated API cost today</td><td>${stats['api_cost_usd']:.2f}</td></tr>
</table>
"""


def send_daily_digest(run_date: date = None, test_mode: bool = False) -> None:
    """Gather stats and send the daily digest email to the owner."""
    run_date = run_date or date.today()

    if test_mode:
        stats = {
            "run_date": run_date.strftime("%B %d, %Y"),
            "sources_checked": 8, "sources_healthy": 7, "sources_degraded": 1,
            "degraded_names": ["Lake County Probate Court"],
            "raw_scraped": 42, "failed_gate1": 3, "failed_gate2": 1,
            "passed_both": 38, "tier_a_routed": 2, "tier_b_routed": 5,
            "tier_c_stored": 12, "tier_d_divorce": 4, "tier_d_eviction": 2,
            "tier_d_bankruptcy": 1, "tier_d_fsbo": 8, "api_cost_usd": 4.70,
        }
        log.info("Test mode — using sample stats")
    else:
        stats = _gather_stats(run_date)

    body = _build_email_body(stats)
    subject = f"Lead Intel Daily Digest — {stats['run_date']}"

    send_email(to=OWNER_EMAIL, subject=subject, html_body=body)
    log.info(f"Daily digest sent to {OWNER_EMAIL}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send daily verification digest")
    parser.add_argument("--test", action="store_true", help="Send with sample data")
    args = parser.parse_args()
    send_daily_digest(test_mode=args.test)
