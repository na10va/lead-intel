"""
enrichment/batch_leads.py — BatchLeads skip tracing (Step 2 alternative).

Used when Skip Sherpa credits are exhausted or as the primary Step 2 provider.
Requests phones (mobile preferred) + email for each lead.

All calls logged to api_costs table for cost watchdog.

API key: BATCH_LEADS_API_KEY in .env
Docs: https://developer.batchdata.com

CLI:
    python enrichment/batch_leads.py --sample 5           # dry run, no DB writes
    python enrichment/batch_leads.py --all                # enrich all leads missing phones
    python enrichment/batch_leads.py --all --tier A,B     # Tier A and B only
    python enrichment/batch_leads.py --all --limit 500    # cap at 500 calls this run
"""

import argparse
import os
import re
import time
from typing import Optional

import requests
from dotenv import load_dotenv

from db.client import get_client, insert_row, update_row
from utils.logger import get_logger

load_dotenv()
log = get_logger("enrichment.batch_leads")

BATCH_LEADS_BASE     = "https://api.batchdata.com"
SKIP_TRACE_ENDPOINT  = f"{BATCH_LEADS_BASE}/api/v1/property/skip-trace"
BATCH_SIZE           = 100        # BatchData allows up to 100 per request
COST_PER_HIT_USD     = 0.07       # ~$0.07 per successful match; verify in your plan
REQUEST_DELAY_S      = 1.0        # polite delay between batches
PAGE_SIZE            = 500        # how many leads to fetch from DB at once
SAFETY_BUFFER        = 25         # stop when credits_left <= this


def _api_key() -> str:
    key = os.getenv("BATCH_LEADS_API_KEY", "")
    if not key:
        raise RuntimeError("BATCH_LEADS_API_KEY is not set in .env")
    return key


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


BATCH_LEADS_AVAILABLE = bool(os.getenv("BATCH_LEADS_API_KEY"))


# ---------------------------------------------------------------------------
# Address + owner parsing (reuses Skip Sherpa's address format)
# ---------------------------------------------------------------------------

_ADDR_RE = re.compile(
    r"^(?P<street>.+?),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s*(?P<zip>\d{5})?",
    re.IGNORECASE,
)
_ADDR_INLINE_RE = re.compile(
    r"^(?P<street>\d+\s+\S+(?:\s+\S+){0,5}?)\s+(?P<city>[A-Za-z\s]+?)\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})?$",
    re.IGNORECASE,
)


def _parse_address(raw: str) -> Optional[dict]:
    if not raw:
        return None
    clean = raw.strip().removesuffix(", USA").removesuffix(",USA").strip()
    m = _ADDR_RE.match(clean) or _ADDR_INLINE_RE.match(clean)
    if not m:
        return None
    return {
        "street":  m.group("street").strip().title(),
        "city":    m.group("city").strip().title(),
        "state":   m.group("state").upper(),
        "zip":     (m.group("zip") or "").strip() or None,
    }


_BUSINESS_KW = re.compile(
    r"\b(LLC|L\.L\.C|INC|CORP|TRUST|LTD|LP|LLP|HOLDINGS|PROPERTIES|REALTY|GROUP|ESTATE)\b",
    re.IGNORECASE,
)


def _build_request_item(lead: dict) -> Optional[dict]:
    """Build a single skip-trace request item for the BatchData API."""
    raw_addr = lead.get("geocoded_address") or lead.get("property_address") or ""
    addr = _parse_address(raw_addr)
    if not addr:
        log.warning(f"Cannot parse address for lead {lead['id'][:8]}: {raw_addr!r}")
        return None

    item: dict = {
        "propertyAddress": {
            "street": addr["street"],
            "city":   addr["city"],
            "state":  addr["state"],
        }
    }
    if addr["zip"]:
        item["propertyAddress"]["zip"] = addr["zip"]

    # Owner name — split into first/last for better match rate
    name = (lead.get("owner_name") or "").strip()
    if name and not _BUSINESS_KW.search(name):
        if "," in name:
            last, rest = name.split(",", 1)
            parts = rest.strip().split()
            item["owner"] = {
                "firstName": parts[0].title() if parts else "",
                "lastName":  last.strip().title(),
            }
        else:
            parts = name.split()
            if len(parts) >= 2:
                item["owner"] = {
                    "firstName": parts[0].title(),
                    "lastName":  parts[-1].title(),
                }

    # Pass our lead ID as a reference so we can match results back
    item["referenceId"] = lead["id"]
    return item


# ---------------------------------------------------------------------------
# Phone ranking
# ---------------------------------------------------------------------------

_PHONE_RANK = {"mobile": 0, "wireless": 0, "voip": 1, "other": 2, "landline": 3}


def _phone_sort_key(ph: dict) -> int:
    return _PHONE_RANK.get((ph.get("type") or ph.get("phoneType") or "").lower(), 4)


# ---------------------------------------------------------------------------
# Parse BatchData response
#
# BatchData response structure:
#   data.results[n]
#     .referenceId          — echoed back from our request
#     .identity.phones[n]
#         .phone            — E.164 format e.g. "+12165551234"
#         .type             — "mobile" | "landline" | "voip"
#         .dnc              — bool
#         .lastSeen         — "YYYY-MM-DD"
#     .identity.emails[n]
#         .email
#     .mailingAddress.street / .city / .state / .zip
#     .owner.firstName / .lastName
# ---------------------------------------------------------------------------

def _is_dnc(ph: dict) -> bool:
    return bool(ph.get("dnc") or ph.get("isDnc"))


def _parse_result(result: dict, property_state: str = "OH") -> dict:
    out: dict = {
        "phone_1":               None,
        "phone_2":               None,
        "phone_3":               None,
        "phone_1_dnc":           None,
        "phone_2_dnc":           None,
        "owner_email":           None,
        "owner_mailing_address": None,
        "owner_out_of_state":    False,
        "api_owner_first_name":  None,
        "api_owner_last_name":   None,
        "mobile_found":          False,
    }

    identity = result.get("identity") or {}
    raw_phones = sorted(identity.get("phones") or [], key=_phone_sort_key)

    e164s = [p.get("phone") for p in raw_phones if p.get("phone")]
    if e164s:
        out["phone_1"]     = e164s[0]
        out["phone_1_dnc"] = _is_dnc(raw_phones[0])
        out["mobile_found"] = _phone_sort_key(raw_phones[0]) == 0
    if len(e164s) > 1:
        out["phone_2"]     = e164s[1]
        out["phone_2_dnc"] = _is_dnc(raw_phones[1])
    if len(e164s) > 2:
        out["phone_3"] = e164s[2]

    emails = identity.get("emails") or []
    if emails:
        out["owner_email"] = emails[0].get("email")

    mailing = result.get("mailingAddress") or {}
    if mailing:
        parts = [
            mailing.get("street") or "",
            mailing.get("city")   or "",
            mailing.get("state")  or "",
            mailing.get("zip")    or "",
        ]
        addr_str = ", ".join(p for p in parts if p)
        if addr_str:
            out["owner_mailing_address"] = addr_str
            mail_state = (mailing.get("state") or "").upper()
            if mail_state and mail_state != property_state.upper():
                out["owner_out_of_state"] = True

    owner = result.get("owner") or {}
    if owner.get("firstName"):
        out["api_owner_first_name"] = owner["firstName"]
    if owner.get("lastName"):
        out["api_owner_last_name"] = owner["lastName"]

    return out


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

def call_batch_leads(items: list[dict]) -> list[dict]:
    """POST up to BATCH_SIZE items to BatchData skip-trace endpoint.

    Returns list of result dicts keyed by referenceId, or empty list on failure.
    """
    try:
        resp = requests.post(
            SKIP_TRACE_ENDPOINT,
            headers=_headers(),
            json={"requests": items},
            timeout=60,
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            log.warning(f"BatchLeads rate limited — waiting {retry_after}s")
            time.sleep(retry_after)
            return []
        resp.raise_for_status()
        return (resp.json().get("data") or {}).get("results") or []
    except Exception as e:
        log.error(f"BatchLeads API request failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Write enrichment to DB
# ---------------------------------------------------------------------------

def _write_enrichment(lead_id: str, parsed: dict) -> None:
    updates = {
        "phone_1":               parsed["phone_1"],
        "phone_2":               parsed["phone_2"],
        "phone_3":               parsed["phone_3"],
        "phone_1_dnc":           parsed["phone_1_dnc"],
        "phone_2_dnc":           parsed["phone_2_dnc"],
        "owner_email":           parsed["owner_email"],
        "owner_mailing_address": parsed["owner_mailing_address"],
        "owner_out_of_state":    parsed["owner_out_of_state"],
        "api_owner_first_name":  parsed["api_owner_first_name"],
        "api_owner_last_name":   parsed["api_owner_last_name"],
        "enrichment_step":       2,
        "enriched":              True,
        "verified_enriched":     True,
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    update_row("raw_leads", lead_id, updates)

    result_label = "success" if parsed["mobile_found"] else "no_mobile"
    insert_row("api_costs", {
        "service":  "batch_leads",
        "lead_id":  lead_id,
        "cost_usd": COST_PER_HIT_USD if parsed["mobile_found"] else 0.0,
        "result":   result_label,
    })


# ---------------------------------------------------------------------------
# Single-lead entry point (used by waterfall.py)
# ---------------------------------------------------------------------------

def run_batch_leads(lead: dict) -> dict:
    """Enrich a single lead via BatchLeads. Returns parsed result dict.

    Called by waterfall.py as Step 2 alternative.
    """
    item = _build_request_item(lead)
    if not item:
        return {"mobile_found": False}

    results = call_batch_leads([item])
    if not results:
        return {"mobile_found": False}

    parsed = _parse_result(results[0], lead.get("state") or "OH")
    _write_enrichment(lead["id"], parsed)
    return parsed


# ---------------------------------------------------------------------------
# Sample run — dry run, no DB writes
# ---------------------------------------------------------------------------

def run_sample(n: int = 5, tiers: Optional[list[str]] = None) -> None:
    client = get_client()
    q = (
        client.table("raw_leads")
        .select("id, owner_name, property_address, geocoded_address, county, tier, state")
        .is_("phone_1", "null")
        .not_.is_("tier", "null")
        .not_.is_("property_address", "null")
        .limit(n)
    )
    if tiers:
        q = q.in_("tier", tiers)
    leads = q.execute().data or []

    if not leads:
        print("No leads available for sample run")
        return

    items, skipped = [], []
    for lead in leads:
        item = _build_request_item(lead)
        if item:
            items.append((lead, item))
        else:
            skipped.append(lead)

    if skipped:
        print(f"\n⚠  {len(skipped)} lead(s) skipped — address unparseable")

    if not items:
        print("No valid lookups could be built")
        return

    print(f"\nSending {len(items)} lookup(s) to BatchLeads...")
    results = call_batch_leads([i for _, i in items])

    if not results:
        print("✗  API returned no results — check BATCH_LEADS_API_KEY and endpoint")
        return

    hits_mobile = hits_any = hits_email = 0
    SEP = "═" * 72
    print(f"\n{SEP}\n  BATCH LEADS SAMPLE — {len(results)} result(s)\n{SEP}")

    for idx, (res, (lead, _)) in enumerate(zip(results, items), 1):
        parsed    = _parse_result(res, lead.get("state") or "OH")
        ref_id    = res.get("referenceId", "")
        phones_raw = sorted((res.get("identity") or {}).get("phones") or [], key=_phone_sort_key)

        if phones_raw:   hits_any += 1
        if parsed["mobile_found"]: hits_mobile += 1
        if parsed["owner_email"]:  hits_email  += 1

        print(f"\n  [{idx}]  Lead  : {lead['id'][:8]}  refId={ref_id[:8]}")
        print(f"        Owner : {(lead.get('owner_name') or '').strip() or '(none)'}")
        print(f"        County: {lead.get('county', '')}  Tier: {lead.get('tier', '')}")

        if phones_raw:
            for ph in phones_raw:
                num   = ph.get("phone", "")
                ptype = (ph.get("type") or "unknown").ljust(8)
                dnc   = "DNC" if _is_dnc(ph) else "   "
                seen  = ph.get("lastSeen") or ""
                print(f"        Phone : {num}  {ptype}  {dnc}  last={seen}")
        else:
            print("        Phone : — none")

        print(f"        Email : {parsed['owner_email'] or '— none'}")
        print(f"        Mail  : {parsed['owner_mailing_address'] or '— none'}")

    total = len(results)
    print(f"\n{SEP}")
    print(f"  Mobile: {hits_mobile}/{total}  Any phone: {hits_any}/{total}  Email: {hits_email}/{total}")
    print(f"  Estimated cost if written: ${hits_any * COST_PER_HIT_USD:.2f}")
    print(f"{SEP}")
    print("\n  ✓ Dry run — nothing written to database\n")


# ---------------------------------------------------------------------------
# Full batch run
# ---------------------------------------------------------------------------

def run_batch(tiers: Optional[list[str]] = None, max_calls: int = 10_000) -> None:
    """Enrich all leads missing phone_1 via BatchLeads.

    Args:
        tiers:     List of tier letters to process, e.g. ["A", "B"]. None = all.
        max_calls: Hard cap on API calls this session (default 10,000).
    """
    PROGRESS_EVERY = 100

    log.info(f"BatchLeads batch starting — tiers={tiers or 'all'}  max_calls={max_calls}")
    enriched   = 0
    no_mobile  = 0
    skipped    = 0
    total_proc = 0
    api_calls  = 0

    def _progress(final: bool = False) -> None:
        tag = "FINAL" if final else f"{total_proc:>6}"
        print(
            f"  [{tag}]  api_calls={api_calls}  mobile={enriched}  "
            f"no_mobile={no_mobile}  skipped={skipped}  "
            f"cost≈${api_calls * COST_PER_HIT_USD:.2f}",
            flush=True,
        )

    print(f"\n{'─'*70}")
    print(f"  BatchLeads batch — tiers={tiers or 'all'}  max_calls={max_calls}")
    print(f"  Est. cost @ ${COST_PER_HIT_USD}/hit: ${max_calls * COST_PER_HIT_USD:.2f} max")
    print(f"{'─'*70}")

    while api_calls < max_calls - SAFETY_BUFFER:
        client     = get_client()
        fetch_size = min(PAGE_SIZE, max_calls - api_calls - SAFETY_BUFFER)

        q = (
            client.table("raw_leads")
            .select("id, owner_name, property_address, geocoded_address, county, tier, state, "
                    "verification_notes")
            .is_("phone_1", "null")
            .not_.is_("tier", "null")
            .not_.is_("property_address", "null")
            .or_("verification_notes.is.null,verification_notes.not.like.*batch_leads*")
            .limit(fetch_size)
        )
        if tiers:
            q = q.in_("tier", tiers)
        leads = q.execute().data or []

        if not leads:
            log.info("No more leads to enrich — done")
            break

        # Build request items
        valid_pairs = []
        for lead in leads:
            item = _build_request_item(lead)
            if item:
                valid_pairs.append((lead, item))
            else:
                skipped += 1
                total_proc += 1
                update_row("raw_leads", lead["id"], {
                    "verification_notes": (
                        ((lead.get("verification_notes") or "") + " | batch_leads_skip").strip(" | ")
                    )
                })

        if not valid_pairs:
            break

        # Send in chunks of BATCH_SIZE
        all_results: list[dict] = []
        for i in range(0, len(valid_pairs), BATCH_SIZE):
            chunk_pairs = valid_pairs[i:i + BATCH_SIZE]
            chunk_items = [item for _, item in chunk_pairs]
            results     = call_batch_leads(chunk_items)
            all_results.extend(results)
            api_calls  += len(chunk_items)
            time.sleep(REQUEST_DELAY_S)

        # Match results back to leads via referenceId
        result_by_ref = {r.get("referenceId"): r for r in all_results}

        for lead, _item in valid_pairs:
            lead_id = lead["id"]
            res     = result_by_ref.get(lead_id)

            if not res:
                # No result returned for this lead
                update_row("raw_leads", lead_id, {
                    "verification_notes": (
                        ((lead.get("verification_notes") or "") + " | batch_leads_no_result").strip(" | ")
                    ),
                    "enriched": True,
                })
                skipped += 1
                total_proc += 1
                continue

            parsed = _parse_result(res, lead.get("state") or "OH")
            _write_enrichment(lead_id, parsed)
            total_proc += 1

            if parsed["mobile_found"]:
                enriched += 1
            else:
                no_mobile += 1

            if total_proc % PROGRESS_EVERY == 0:
                _progress()

    _progress(final=True)
    print(f"{'─'*70}\n")
    log.info(
        f"BatchLeads batch complete — enriched={enriched}  "
        f"no_mobile={no_mobile}  skipped={skipped}  cost≈${api_calls * COST_PER_HIT_USD:.2f}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BatchLeads skip tracing")
    parser.add_argument("--sample", type=int, metavar="N",
                        help="Dry run: call API for N leads, print results, no DB writes")
    parser.add_argument("--all",    action="store_true",
                        help="Run full batch on all leads missing phones")
    parser.add_argument("--tier",   metavar="TIERS",
                        help="Comma-separated tier filter: A,B or A,B,C")
    parser.add_argument("--limit",  type=int, default=10_000,
                        help="Max API calls this run (default 10,000)")
    args = parser.parse_args()

    tier_list = [t.strip().upper() for t in args.tier.split(",")] if args.tier else None

    if args.sample:
        run_sample(n=args.sample, tiers=tier_list)
    elif args.all:
        run_batch(tiers=tier_list, max_calls=args.limit)
    else:
        parser.print_help()
