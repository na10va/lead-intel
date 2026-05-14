"""
enrichment/backfill_names.py — Backfill owner_first_name / owner_last_name
on all existing raw_leads rows that have owner_name but no split name.

Requires the migration to have been run first:
    ALTER TABLE raw_leads
      ADD COLUMN IF NOT EXISTS owner_first_name TEXT,
      ADD COLUMN IF NOT EXISTS owner_last_name  TEXT;

CLI:
    python enrichment/backfill_names.py
    python enrichment/backfill_names.py --dry-run
"""

import argparse
from dotenv import load_dotenv

load_dotenv()

from db.client import get_client, update_row
from utils.name_splitter import split_owner_name
from utils.logger import get_logger

log = get_logger("enrichment.backfill_names")

PAGE_SIZE    = 500
REQUEST_DELAY = 0.0   # no external API — pure DB writes, no throttle needed


def run_backfill(dry_run: bool = False) -> None:
    client = get_client()

    updated = skipped = failed = 0
    offset = 0

    log.info(f"Starting name backfill (dry_run={dry_run})")

    while True:
        rows = (
            client.table("raw_leads")
            .select("id,owner_name,owner_first_name,owner_last_name,source_type")
            .not_.is_("owner_name", "null")
            .neq("owner_name", "")
            .is_("owner_first_name", "null")   # not yet split
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
            .data or []
        )

        if not rows:
            break

        log.info(f"Processing batch of {len(rows)} (offset={offset})")

        for row in rows:
            name = row.get("owner_name", "")
            first, last = split_owner_name(name, row.get("source_type", ""))

            if not first and not last:
                skipped += 1
                continue

            if dry_run:
                log.info(f"[DRY RUN] {row['id'][:8]}: '{name}' → '{first}' / '{last}'")
                updated += 1
                continue

            try:
                update_row("raw_leads", row["id"], {
                    "owner_first_name": first or None,
                    "owner_last_name":  last or None,
                })
                updated += 1
            except Exception as e:
                log.error(f"Failed to update {row['id']}: {e}")
                failed += 1

        offset += PAGE_SIZE
        if len(rows) < PAGE_SIZE:
            break

    log.info(
        f"Backfill complete — updated={updated}  "
        f"skipped={skipped} (entities/unsplittable)  failed={failed}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill owner first/last name from owner_name")
    parser.add_argument("--dry-run", action="store_true", help="Print splits without writing to DB")
    args = parser.parse_args()
    run_backfill(dry_run=args.dry_run)
