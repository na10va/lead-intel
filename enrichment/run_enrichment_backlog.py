"""
enrichment/run_enrichment_backlog.py — One-shot backlog enrichment runner.

Enriches all verified, unenriched Tier A/B/C leads. Skips Tier D (per spec:
Tier D leads are stored only and never enriched unless stacked with a primary signal).

Usage:
    python enrichment/run_enrichment_backlog.py [--dry-run] [--concurrency N]
"""

import argparse
import concurrent.futures
from db.client import get_client
from enrichment.waterfall import enrich_lead
from utils.logger import get_logger

log = get_logger("enrichment.backlog")


def fetch_backlog(client, tiers: list[str] | None = None) -> list[str]:
    """Return IDs of all unenriched verified leads for the given tiers."""
    tiers = tiers or ["A", "B", "C"]
    ids = []
    offset = 0
    while True:
        batch = (
            client.table("raw_leads")
            .select("id")
            .eq("enriched", False)
            .eq("verified_raw", True)
            .in_("tier", tiers)
            .range(offset, offset + 999)
            .execute()
            .data or []
        )
        if not batch:
            break
        ids.extend(r["id"] for r in batch)
        offset += 1000
        if len(batch) < 1000:
            break
    return ids


def run(dry_run: bool = False, concurrency: int = 4, tiers: list[str] | None = None) -> None:
    client = get_client()
    ids = fetch_backlog(client, tiers=tiers)
    log.info(f"Enrichment backlog: {len(ids)} Tier A/B/C leads to enrich")

    if dry_run:
        log.info("Dry run — no enrichment calls will be made")
        for lead_id in ids[:10]:
            log.info(f"  Would enrich: {lead_id}")
        return

    success = 0
    failed = 0

    def _enrich_one(lead_id: str) -> tuple[str, bool]:
        try:
            result = enrich_lead(lead_id)
            return lead_id, result
        except Exception as e:
            log.error(f"Enrichment error for {lead_id}: {e}")
            return lead_id, False

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(_enrich_one, lid): lid for lid in ids}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            lead_id, ok = future.result()
            if ok:
                success += 1
            else:
                failed += 1
            if i % 25 == 0 or i == len(ids):
                log.info(f"Progress: {i}/{len(ids)} — success={success}, no_mobile={failed}")

    log.info(
        f"Enrichment backlog complete — "
        f"{success} mobile found, {failed} no mobile, {len(ids)} total"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enrich all unenriched Tier A/B/C leads")
    parser.add_argument("--dry-run", action="store_true", help="List leads without enriching")
    parser.add_argument("--concurrency", type=int, default=4, help="Parallel enrichment threads (default 4)")
    parser.add_argument("--tier", metavar="TIER", help="Only enrich leads of this tier: A, B, C, or D")
    args = parser.parse_args()
    tiers = [args.tier.upper()] if args.tier else None
    run(dry_run=args.dry_run, concurrency=args.concurrency, tiers=tiers)
