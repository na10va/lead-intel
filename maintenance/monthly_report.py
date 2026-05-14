"""
maintenance/monthly_report.py — Emails the owner a full system health summary.

Runs on the 1st of every month at 8:00 AM EST.
Covers: pipeline uptime, source health, lead counts, costs, Skip Matrix queue.

CLI:
    python maintenance/monthly_report.py          # send real report
    python maintenance/monthly_report.py --test   # send with sample data
"""

import argparse
import os
from datetime import date

from db.client import get_client
from enrichment.skip_matrix_flag import get_pending_count
from maintenance.dependency_check import get_flagged_packages
from routing.notify import send_email
from utils.logger import get_logger

log = get_logger("maintenance.monthly_report")

OWNER_EMAIL = os.getenv("OWNER_EMAIL")


def _gather_monthly_stats() -> dict:
    """Query Supabase for last month's pipeline statistics."""
    client = get_client()
    today = date.today()
    month_start = today.replace(day=1).isoformat()

    def count(filters: dict) -> int:
        q = client.table("raw_leads").select("id", count="exact")
        for col, val in filters.items():
            q = q.eq(col, val)
        q = q.gte("created_at", month_start)
        return q.execute().count or 0

    # Source health stats from maintenance_log
    self_healed = (
        client.table("maintenance_log")
        .select("id", count="exact")
        .eq("event_type", "self_heal")
        .eq("resolved", True)
        .gte("created_at", month_start)
        .execute()
        .count or 0
    )
    needs_attention = (
        client.table("maintenance_log")
        .select("id", count="exact")
        .eq("event_type", "self_heal")
        .eq("resolved", False)
        .gte("created_at", month_start)
        .execute()
        .count or 0
    )

    # API costs
    cost_rows = (
        client.table("api_costs")
        .select("cost_usd")
        .gte("called_at", month_start)
        .execute()
        .data or []
    )
    total_cost = sum(r.get("cost_usd", 0) for r in cost_rows)
    verified_count = count({"verified_raw": True, "verified_enriched": True})
    cost_per_lead = (total_cost / verified_count) if verified_count > 0 else 0

    return {
        "month": today.strftime("%B %Y"),
        "self_healed_sources": self_healed,
        "sources_needing_attention": needs_attention,
        "total_leads": count({}),
        "tier_a_leads": count({"tier": "A"}),
        "tier_a_routed": count({"tier": "A", "routed_to_va": True}),
        "skip_matrix_pending": get_pending_count(),
        "total_api_spend": round(total_cost, 2),
        "cost_per_verified_lead": round(cost_per_lead, 2),
        "flagged_packages": get_flagged_packages(),
    }


def _build_email(stats: dict) -> str:
    pkg_list = (
        "<br>".join(f"• {p}" for p in stats["flagged_packages"])
        if stats["flagged_packages"] else "None"
    )
    return f"""
<h2>Lead Intel — Monthly Health Report ({stats['month']})</h2>

<h3>Pipeline Health</h3>
<table border="1" cellpadding="6">
<tr><td>Sources self-healed</td><td>{stats['self_healed_sources']}</td></tr>
<tr><td>Sources needing attention</td><td>{stats['sources_needing_attention']}</td></tr>
</table>

<h3>Lead Volume</h3>
<table border="1" cellpadding="6">
<tr><td>Total leads ingested</td><td>{stats['total_leads']}</td></tr>
<tr><td>Tier A leads</td><td>{stats['tier_a_leads']} ({stats['tier_a_routed']} routed to VA)</td></tr>
<tr><td>Skip Matrix queue</td><td>{stats['skip_matrix_pending']} leads awaiting manual enrichment</td></tr>
</table>

<h3>Costs</h3>
<table border="1" cellpadding="6">
<tr><td>Total API spend</td><td>${stats['total_api_spend']:.2f}</td></tr>
<tr><td>Cost per verified lead</td><td>${stats['cost_per_verified_lead']:.2f}</td></tr>
</table>

<h3>Dependencies</h3>
<p>Outdated or flagged packages:<br>{pkg_list}</p>
<p style="color:#999;font-size:12px;">Never auto-update. Review and approve manually.</p>
"""


def send_monthly_report(test_mode: bool = False) -> None:
    """Gather stats and send the monthly health report."""
    if test_mode:
        stats = {
            "month": date.today().strftime("%B %Y"),
            "self_healed_sources": 2,
            "sources_needing_attention": 1,
            "total_leads": 384,
            "tier_a_leads": 28,
            "tier_a_routed": 26,
            "skip_matrix_pending": 4,
            "total_api_spend": 87.40,
            "cost_per_verified_lead": 0.23,
            "flagged_packages": ["playwright==1.49.0 (security advisory)", "requests==2.32.3 (outdated)"],
        }
        log.info("Test mode — using sample stats")
    else:
        stats = _gather_monthly_stats()

    body = _build_email(stats)
    subject = f"Lead Intel Monthly Report — {stats['month']}"
    send_email(to=OWNER_EMAIL, subject=subject, html_body=body)
    log.info(f"Monthly report sent to {OWNER_EMAIL}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send monthly health report")
    parser.add_argument("--test", action="store_true", help="Send with sample data")
    args = parser.parse_args()
    send_monthly_report(test_mode=args.test)
