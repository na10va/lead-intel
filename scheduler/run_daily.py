"""
scheduler/run_daily.py — Master scheduler. Runs all agents and pipeline steps daily.

Schedule (all times EST):
    6:45 AM  — Source health check
    7:00 AM  — All 8 agents in parallel
    7:15 AM  — Gate 1 verification
    7:20 AM  — Enrichment (Tier A/B/C only)
    7:35 AM  — Gate 2 verification
    7:40 AM  — Scoring
    7:45 AM  — Routing
    7:50 AM  — Zillow Deal Finder
    8:00 AM  — Daily digest email
    Every 4h (7AM–7PM) — Foreclosure real-time check

If any step fails, log the error and continue to the next step.
The daily digest always sends — even if the pipeline partially failed.

CLI:
    python scheduler/run_daily.py              # start scheduled mode
    python scheduler/run_daily.py --run-now    # run full pipeline once immediately
    python scheduler/run_daily.py --resume     # resume after cost watchdog pause
"""

import argparse
import concurrent.futures

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from utils.logger import get_logger

log = get_logger("scheduler")

OHIO_COUNTIES = ["cuyahoga", "lake", "mahoning"]
STATE = "OH"


def step_source_health_check() -> bool:
    """6:45 AM — Run source health check. Returns False if pipeline should be aborted."""
    from verification.verify_sources import run_health_check
    log.info("STEP: Source health check")
    try:
        result = run_health_check()
        if result.get("pipeline_paused"):
            log.error("Pipeline paused — too many sources down. Aborting today's run.")
            return False
        return True
    except Exception as e:
        log.error(f"Source health check failed: {e}")
        return True  # non-fatal — continue pipeline


def step_run_agents() -> None:
    """7:00 AM — Run all 8 agents in parallel."""
    from agents import (
        probate_agent, code_violation_agent, tax_lien_agent,
        divorce_agent, eviction_agent, bankruptcy_agent, fsbo_agent,
    )

    log.info("STEP: Running all agents in parallel")

    tasks = []
    for county in OHIO_COUNTIES:
        tasks.extend([
            (probate_agent.run, (county, STATE)),
            (code_violation_agent.run, (county, STATE)),
            (tax_lien_agent.run, (county, STATE)),
            (divorce_agent.run, (county, STATE)),
            (eviction_agent.run, (county, STATE)),
            (fsbo_agent.run, (county, STATE)),
        ])
    tasks.append((bankruptcy_agent.run, ("ohio_northern",)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fn, *args): fn.__module__ for fn, args in tasks}
        for future in concurrent.futures.as_completed(futures):
            module = futures[future]
            try:
                future.result()
            except Exception as e:
                log.error(f"Agent {module} failed: {e}")


def step_gate1_verification() -> None:
    """7:15 AM — Gate 1 post-scrape verification."""
    from verification.verify_leads import run_all_unverified
    log.info("STEP: Gate 1 verification")
    try:
        run_all_unverified()
    except Exception as e:
        log.error(f"Gate 1 verification failed: {e}")


def step_county_auditor() -> None:
    """7:18 AM — Enrich all leads missing estimated_value from county auditor GIS (free)."""
    from enrichment.county_auditor import run_batch
    log.info("STEP: County auditor enrichment")
    try:
        run_batch()
    except Exception as e:
        log.error(f"County auditor enrichment failed: {e}")


def step_enrichment() -> None:
    """7:20 AM — Enrichment waterfall on all Gate 1 passing leads."""
    from db.client import get_client
    from enrichment.waterfall import enrich_lead
    from maintenance.cost_watchdog import check_spend, is_pipeline_paused

    log.info("STEP: Enrichment")

    watchdog = check_spend()
    if watchdog.get("pipeline_paused"):
        log.error("Enrichment skipped — cost watchdog has paused the pipeline")
        return

    client = get_client()
    leads = (
        client.table("raw_leads")
        .select("id")
        .eq("verified_raw", True)
        .eq("enriched", False)
        .in_("source_type", ["probate", "code_violation", "foreclosure", "tax_lien"])
        .execute()
        .data or []
    )

    log.info(f"Enriching {len(leads)} Gate 1 passing leads")
    for row in leads:
        if is_pipeline_paused():
            log.error("Enrichment halted mid-run — cost watchdog triggered")
            break
        try:
            enrich_lead(row["id"])
            check_spend()
        except Exception as e:
            log.error(f"Enrichment failed for lead {row['id']}: {e}")


def step_gate2_verification() -> None:
    """7:35 AM — Gate 2 post-enrichment verification."""
    from verification.verify_enrichment import run_all_unenriched
    log.info("STEP: Gate 2 verification")
    try:
        run_all_unenriched()
    except Exception as e:
        log.error(f"Gate 2 verification failed: {e}")


def step_scoring() -> None:
    """7:40 AM — Score all Gate 2 passing leads."""
    from scoring.score import run_batch_scoring
    log.info("STEP: Scoring")
    try:
        run_batch_scoring()
    except Exception as e:
        log.error(f"Scoring failed: {e}")


def step_routing() -> None:
    """7:45 AM — Route Tier A and B leads."""
    from db.client import get_client
    from routing.va_router import route_lead

    log.info("STEP: Routing")
    client = get_client()
    leads = (
        client.table("raw_leads")
        .select("id, tier")
        .in_("tier", ["A", "B"])
        .eq("routed_to_va", False)
        .eq("verified_enriched", True)
        .execute()
        .data or []
    )

    log.info(f"Routing {len(leads)} Tier A/B leads")
    for row in leads:
        try:
            route_lead(row["id"], row["tier"])
        except Exception as e:
            log.error(f"Routing failed for lead {row['id']}: {e}")


def step_zillow() -> None:
    """7:50 AM — Zillow Deal Finder."""
    from zillow.zillow_scraper import run
    log.info("STEP: Zillow Deal Finder")
    try:
        run()
    except Exception as e:
        log.error(f"Zillow Deal Finder failed: {e}")


def step_daily_digest() -> None:
    """8:00 AM — Send daily digest (always runs)."""
    from verification.daily_report import send_daily_digest
    log.info("STEP: Daily digest")
    try:
        send_daily_digest()
    except Exception as e:
        log.error(f"Daily digest failed: {e}")


def step_mojo_sync() -> None:
    """7:47 AM — Sync routed leads to Mojo Power Dialer and pull dispositions."""
    from routing.mojo_router import sync_new_leads, pull_dispositions
    log.info("STEP: Mojo sync")
    try:
        sync_new_leads()
        pull_dispositions()
    except Exception as e:
        log.error(f"Mojo sync failed: {e}")


def step_tier_d_stacker() -> None:
    """7:48 AM — Re-score primary leads that have gained new Tier D stacking signals."""
    from scoring.tier_d_stacker import run_all
    log.info("STEP: Tier D stacker")
    try:
        run_all()
    except Exception as e:
        log.error(f"Tier D stacker failed: {e}")


def step_lake_county_email() -> None:
    """First Monday of each month, 7:00 AM — Request delinquent list from Karen."""
    from scheduler.lake_county_email import send_lake_county_email
    log.info("STEP: Lake County monthly email to Karen")
    try:
        send_lake_county_email()
    except Exception as e:
        log.error(f"Lake County monthly email failed: {e}")


def step_foreclosure_check() -> None:
    """Every 4 hours (7AM–7PM) — Foreclosure real-time check."""
    from agents.foreclosure_agent import run as foreclosure_run
    log.info("STEP: Foreclosure real-time check")
    for county in OHIO_COUNTIES:
        try:
            foreclosure_run(county, STATE)
        except Exception as e:
            log.error(f"Foreclosure check failed for {county}: {e}")


def run_full_pipeline() -> None:
    """Run the complete daily pipeline once, in order."""
    log.info("=" * 60)
    log.info("Starting full pipeline run")
    log.info("=" * 60)

    if not step_source_health_check():
        step_daily_digest()
        return

    step_run_agents()
    step_gate1_verification()
    step_county_auditor()
    step_enrichment()
    step_gate2_verification()
    step_scoring()
    step_routing()
    step_mojo_sync()
    step_tier_d_stacker()
    step_zillow()
    step_daily_digest()

    log.info("Pipeline run complete")


def start_webhook_server() -> None:
    """Start the disposition webhook server in a background thread."""
    try:
        from routing.webhook_server import start
        start(block=False)
    except Exception as e:
        log.error(f"Webhook server failed to start: {e}")


def start_scheduler() -> None:
    """Start the APScheduler blocking scheduler with all jobs."""
    start_webhook_server()
    scheduler = BlockingScheduler(timezone="America/New_York")

    scheduler.add_job(run_full_pipeline, CronTrigger(hour=6, minute=45))
    # Fire at 7AM, 11AM, 3PM, 7PM EST — respects the 7AM–7PM window from CLAUDE.md.
    scheduler.add_job(
        step_foreclosure_check,
        CronTrigger(hour="7,11,15,19", timezone="America/New_York"),
        id="foreclosure_realtime",
        replace_existing=True,
    )
    # First Monday of each month at 7:00 AM EST.
    # start_date=2026-05-04: April data already ingested manually; emails begin in May.
    scheduler.add_job(
        step_lake_county_email,
        CronTrigger(
            day_of_week="mon",
            day="1-7",
            hour=7,
            minute=0,
            start_date="2026-05-04 07:00:00",
            timezone="America/New_York",
        ),
        id="lake_county_monthly_email",
    )

    log.info("Scheduler started — pipeline runs daily at 6:45 AM EST")
    log.info("Foreclosure check runs every 4 hours between 7 AM–7 PM EST")
    log.info("Lake County monthly email runs on the first Monday of each month at 7:00 AM EST (starting May 2026)")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Scheduler stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lead Intel master scheduler")
    parser.add_argument("--run-now", action="store_true", help="Run full pipeline once immediately")
    parser.add_argument("--resume", action="store_true", help="Resume after cost watchdog pause")
    args = parser.parse_args()

    if args.run_now or args.resume:
        if args.resume:
            log.info("Resuming pipeline after cost watchdog pause")
        run_full_pipeline()
    else:
        start_scheduler()
