"""
routing/mojo_router.py — Syncs Tier A/B/C leads to Mojo via Zapier webhook.

Flow:
    1. Query all routed leads not yet pushed to Mojo (mojo_synced IS NULL or FALSE)
    2. POST each lead to ZAPIER_MOJO_PUSH_URL — Zapier adds it to the correct Mojo list
    3. Mark lead as mojo_synced=True in Supabase

Disposition writeback (Mojo → Supabase):
    Zapier catches a Mojo disposition event and POSTs to ZAPIER_MOJO_DISPOSITION_URL,
    which hits our /disposition endpoint (or a second Zap that calls our webhook).
    pull_dispositions() is called by the scheduler to process any pending writebacks
    stored in Supabase by the inbound Zap.

Zapier setup:
    Push Zap:        Trigger = Webhooks (Catch Hook) → Action = Mojo (Add Contact to List)
    Disposition Zap: Trigger = Mojo (New Disposition) → Action = Webhooks (POST to our system)

ENV vars:
    ZAPIER_MOJO_PUSH_URL         — webhook URL from the push Zap
    ZAPIER_MOJO_DISPOSITION_URL  — webhook URL from the disposition Zap (optional)

CLI:
    python routing/mojo_router.py --sync
    python routing/mojo_router.py --sync --tier A B
    python routing/mojo_router.py --dispositions
"""

import argparse
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

from db.client import get_client, update_row
from utils.logger import get_logger

log = get_logger("routing.mojo_router")

ZAPIER_PUSH_URL = os.getenv("ZAPIER_MOJO_PUSH_URL", "")
PAGE = 500
RATE_LIMIT_DELAY = 0.25  # seconds between Zapier POSTs — stay well under free tier limits


def _build_payload(lead: dict) -> dict:
    """Build the JSON payload Zapier will receive and forward to Mojo."""
    def _clean_phone(raw: str | None) -> str:
        import re
        return re.sub(r"\D", "", raw or "")[-10:]  # digits only, last 10

    phones = [
        _clean_phone(lead.get("phone_1")),
        _clean_phone(lead.get("phone_2")),
        _clean_phone(lead.get("phone_3")),
    ]

    return {
        "lead_id":    str(lead["id"]),
        "first_name": lead.get("owner_first_name") or "",
        "last_name":  lead.get("owner_last_name") or lead.get("owner_name") or "",
        "phone_1":    phones[0],
        "phone_2":    phones[1],
        "phone_3":    phones[2],
        "address":    lead.get("property_address") or "",
        "state":      (lead.get("state") or "")[:2].upper(),
        "tier":       lead.get("tier") or "",
        "score":      str(lead.get("score") or ""),
        "source":     lead.get("source_type") or "",
        "county":     lead.get("county") or "",
        "filing_date": str(lead.get("filing_date") or ""),
        "notes": (
            f"Tier {lead.get('tier')} | Score {lead.get('score')} | "
            f"{lead.get('source_type')} | {lead.get('county')} County | "
            f"Filed {lead.get('filing_date')}"
        ),
    }


def sync_new_leads(tier_filter: list[str] | None = None) -> None:
    """POST all unsynced routed leads to Zapier → Mojo."""
    if not ZAPIER_PUSH_URL:
        log.warning("ZAPIER_MOJO_PUSH_URL not set — skipping Mojo sync")
        return

    client = get_client()
    tiers = tier_filter or ["A", "B", "C"]

    # Paginate — routed leads not yet pushed to Mojo
    all_leads: list[dict] = []
    offset = 0
    while True:
        page = (
            client.table("raw_leads")
            .select(
                "id,owner_name,owner_first_name,owner_last_name,"
                "property_address,state,county,"
                "phone_1,phone_2,phone_3,"
                "score,tier,source_type,filing_date"
            )
            .in_("tier", tiers)
            .eq("routed_to_va", True)
            .or_("mojo_synced.is.null,mojo_synced.eq.false")
            .order("tier")
            .order("score", desc=True)
            .range(offset, offset + PAGE - 1)
            .execute()
            .data or []
        )
        all_leads.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE

    log.info(f"Mojo sync: {len(all_leads)} leads to push (tiers={tiers})")
    pushed = no_phone = failed = 0

    for lead in all_leads:
        phones = [lead.get("phone_1"), lead.get("phone_2"), lead.get("phone_3")]
        has_phone = any(phones)

        if not has_phone:
            # Mark synced so it doesn't retry — VA finds phone manually
            update_row("raw_leads", lead["id"], {"mojo_synced": True})
            no_phone += 1
            continue

        payload = _build_payload(lead)
        try:
            resp = requests.post(ZAPIER_PUSH_URL, json=payload, timeout=15)
            resp.raise_for_status()
            update_row("raw_leads", lead["id"], {"mojo_synced": True})
            pushed += 1
            time.sleep(RATE_LIMIT_DELAY)
        except Exception as e:
            log.error(f"Failed to push lead {lead['id'][:8]}: {e}")
            failed += 1

    log.info(f"Mojo sync complete — pushed={pushed}  no_phone={no_phone}  failed={failed}")


def pull_dispositions() -> None:
    """Write Mojo disposition data back to raw_leads.

    Zapier catches Mojo disposition events and writes them to a staging table
    or directly updates raw_leads via a POST to a Supabase edge function.
    This function processes any leads where disposition is pending a writeback.

    For now, dispositions written directly by Zapier via Supabase REST API
    need no additional processing here — this is a hook for future logic
    (e.g. re-queuing bad numbers for re-enrichment).
    """
    client = get_client()

    # Find leads marked with bad dispositions that should be re-enriched
    bad_dispositions = ["Wrong Number", "Disconnected", "Not In Service"]
    rows = (
        client.table("raw_leads")
        .select("id,disposition,phone_1")
        .in_("disposition", bad_dispositions)
        .not_.is_("phone_1", "null")
        .or_("reenrich_flagged.is.null,reenrich_flagged.eq.false")
        .execute()
        .data or []
    )

    if not rows:
        log.info("No bad-disposition leads pending re-enrichment")
        return

    log.info(f"{len(rows)} leads with bad dispositions — flagging for re-enrichment")
    for row in rows:
        try:
            update_row("raw_leads", row["id"], {
                "phone_1": None,  # clear the bad number
                "reenrich_flagged": True,
                "enriched": False,  # re-queue for enrichment waterfall
            })
        except Exception as e:
            log.error(f"Failed to flag lead {row['id'][:8]} for re-enrichment: {e}")

    log.info(f"Flagged {len(rows)} leads for re-enrichment")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mojo Power Dialer sync via Zapier")
    parser.add_argument("--sync", action="store_true", help="Push new leads to Mojo via Zapier")
    parser.add_argument("--dispositions", action="store_true", help="Process disposition writebacks")
    parser.add_argument("--tier", choices=["A", "B", "C"], nargs="+", help="Limit sync to specific tiers")
    args = parser.parse_args()

    if args.sync:
        sync_new_leads(tier_filter=args.tier)
    if args.dispositions:
        pull_dispositions()
    if not args.sync and not args.dispositions:
        parser.print_help()
