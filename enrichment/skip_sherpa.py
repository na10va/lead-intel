"""
enrichment/skip_sherpa.py — Step 2: Skip Sherpa API skip tracing.

Only triggered when Step 1 (public sources) did not return a verified mobile.
Requests up to 3 phone numbers (mobile preferred) + email address.

Rate limit: max 500 calls/day to stay within budget.
All calls are logged to api_costs table for the cost watchdog.

API key: SKIP_SHERPA_API_KEY in .env

CLI:
    python enrichment/skip_sherpa.py --sample 5          # dry run, 5 leads
    python enrichment/skip_sherpa.py --all               # full batch (respects 500/day cap)
    python enrichment/skip_sherpa.py --all --tier A      # Tier A only
"""

import argparse
import os
import re
import time
from datetime import date
from typing import Optional

import requests
from dotenv import load_dotenv

from db.client import get_client, insert_row, update_row
from utils.logger import get_logger

load_dotenv()
log = get_logger("enrichment.skip_sherpa")

SKIP_SHERPA_BASE    = "https://skipsherpa.com"
PROPERTIES_ENDPOINT = f"{SKIP_SHERPA_BASE}/api/properties"
BATCH_SIZE          = 25
DAILY_CALL_LIMIT    = 500
COST_PER_CALL_USD   = 0.10
REQUEST_DELAY_S     = 1.5   # between API sub-batches — avoids per-minute rate limit
PAGE_SIZE           = 200

SKIP_SHERPA_AVAILABLE = bool(os.getenv("SKIP_SHERPA_API_KEY"))


# ---------------------------------------------------------------------------
# Auth & headers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.getenv("SKIP_SHERPA_API_KEY", "")
    if not key:
        raise RuntimeError("SKIP_SHERPA_API_KEY is not set in .env")
    return key


def _headers() -> dict:
    return {
        "API-Key":      _api_key(),
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------

# Handles geocoded Google format: "10904 Shale Ave, Cleveland, OH 44104, USA"
# and raw county format:           "10904 SHALE AVE CLEVELAND OH 44104"
_ADDR_RE = re.compile(
    r"^(?P<street>.+?),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s*(?P<zip>\d{5})?",
    re.IGNORECASE,
)
_ADDR_INLINE_RE = re.compile(
    r"^(?P<street>\d+\s+\S+(?:\s+\S+){0,5}?)\s+(?P<city>[A-Za-z\s]+?)\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})?$",
    re.IGNORECASE,
)


def _parse_address(raw: str) -> Optional[dict]:
    """Return {street, city, state, zipcode} or None if unparseable."""
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
        "zipcode": (m.group("zip") or "").strip() or None,
    }


# ---------------------------------------------------------------------------
# Owner name parsing
# ---------------------------------------------------------------------------

_BUSINESS_KEYWORDS = re.compile(
    r"\b(LLC|L\.L\.C|INC|CORP|TRUST|LTD|LP|LLP|HOLDINGS|PROPERTIES|REALTY|GROUP|ESTATE)\b",
    re.IGNORECASE,
)


def _parse_owner_name(owner_name: str) -> dict:
    """Return the owner entity sub-dict for a Skip Sherpa PropertyLookup."""
    name = (owner_name or "").strip()
    if not name:
        return {}

    if _BUSINESS_KEYWORDS.search(name):
        return {"business_name_lookup": {"business_name": name}}

    # Ohio auditor format: "LAST, FIRST MIDDLE"
    if "," in name:
        last, rest = name.split(",", 1)
        parts = rest.strip().split()
        first  = parts[0].title() if parts else ""
        middle = parts[1].title() if len(parts) > 1 else ""
        return {"person_name_lookup": {
            "first_name":  first,
            "middle_name": middle,
            "last_name":   last.strip().title(),
        }}

    parts = name.split()
    if len(parts) >= 2:
        return {"person_name_lookup": {
            "first_name": parts[0].title(),
            "last_name":  parts[-1].title(),
        }}

    return {"person_name_lookup": {"last_name": name.title()}}


# ---------------------------------------------------------------------------
# Build lookup payload
# ---------------------------------------------------------------------------

def _build_property_lookup(lead: dict) -> Optional[dict]:
    """Build a single PropertyLookup dict for the Skip Sherpa request body."""
    raw_addr = lead.get("geocoded_address") or lead.get("property_address") or ""
    addr = _parse_address(raw_addr)
    if not addr:
        log.warning(f"Cannot parse address for lead {lead['id'][:8]}: {raw_addr!r}")
        return None

    lookup: dict = {
        "reference_id": lead["id"],
        "property_address_lookup": {
            "street": addr["street"],
            "city":   addr["city"],
            "state":  addr["state"],
        },
    }
    if addr["zipcode"]:
        lookup["property_address_lookup"]["zipcode"] = addr["zipcode"]

    owner_entity = _parse_owner_name(lead.get("owner_name") or "")
    if owner_entity:
        lookup.update(owner_entity)

    return lookup


# ---------------------------------------------------------------------------
# Phone prioritization
# Actual API response uses "type" field (not "phone_type")
# ---------------------------------------------------------------------------

_PHONE_RANK = {"mobile": 0, "voip": 1, "other": 2, "landline": 3}


def _phone_sort_key(ph: dict) -> int:
    return _PHONE_RANK.get((ph.get("type") or "").lower(), 4)


# ---------------------------------------------------------------------------
# Parse API response
#
# Actual response structure (confirmed from live API):
#   result["property"]["owners"][0]["person"]["phone_numbers"][n]
#     .e164_format   — phone number
#     .type          — "mobile" | "landline" | "voip" | "other"
#     .last_seen     — "YYYY-MM-DD"
#     .dnc_statuses[0].is_dnc — bool
#   result["property"]["owners"][0]["person"]["emails"][n].email_address
#   result["property"]["tax_mailing_address"]["us_address"]
#     .street / .city / .state / .zipcode
# ---------------------------------------------------------------------------

def _is_dnc(ph: dict) -> bool:
    """Return True if any DNC status entry marks this number as registered."""
    return any(s.get("is_dnc") for s in (ph.get("dnc_statuses") or []))


def _parse_result(result: dict, property_state: str = "OH") -> dict:
    """Extract phones, DNC flags, email, mailing address, and owner name from a live PropertyResult."""
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

    prop    = result.get("property") or {}
    owners  = prop.get("owners") or []
    person  = (owners[0].get("person") or {}) if owners else {}

    # Phone numbers — sorted mobile-first, using e164_format field
    raw_phones = sorted(person.get("phone_numbers") or [], key=_phone_sort_key)
    e164s = [p["e164_format"] for p in raw_phones if p.get("e164_format")]
    if e164s:
        out["phone_1"]     = e164s[0]
        out["phone_1_dnc"] = _is_dnc(raw_phones[0])
        out["mobile_found"] = _phone_sort_key(raw_phones[0]) == 0
    if len(e164s) > 1:
        out["phone_2"]     = e164s[1]
        out["phone_2_dnc"] = _is_dnc(raw_phones[1])
    if len(e164s) > 2:
        out["phone_3"] = e164s[2]

    # Email
    emails = person.get("emails") or []
    if emails:
        out["owner_email"] = emails[0].get("email_address")

    # Tax mailing address
    mailing_us = (prop.get("tax_mailing_address") or {}).get("us_address") or {}
    if mailing_us:
        parts = [
            mailing_us.get("street") or "",
            mailing_us.get("city")   or "",
            mailing_us.get("state")  or "",
            mailing_us.get("zipcode") or "",
        ]
        addr_str = ", ".join(p for p in parts if p)
        if addr_str:
            out["owner_mailing_address"] = addr_str
            mail_state = (mailing_us.get("state") or "").upper()
            if mail_state and mail_state != property_state.upper():
                out["owner_out_of_state"] = True

    # Owner name from Skip Sherpa (splits into first/last for the new schema columns)
    pn = person.get("person_name") or {}
    first = (pn.get("first_name") or "").strip()
    last  = (pn.get("last_name")  or "").strip()
    if first:
        out["api_owner_first_name"] = first
    if last:
        out["api_owner_last_name"] = last

    return out


# ---------------------------------------------------------------------------
# Daily call counter
# ---------------------------------------------------------------------------

def _calls_today(client) -> int:
    resp = (
        client.table("api_costs")
        .select("id", count="exact")
        .eq("service", "skip_sherpa")
        .gte("called_at", date.today().isoformat())
        .execute()
    )
    return resp.count or 0


# ---------------------------------------------------------------------------
# Batch API call
#
# The API does NOT echo reference_id back in the result objects.
# Results are returned in the same positional order as the input lookups.
# We zip(lookups, results) to match them.
# ---------------------------------------------------------------------------

def call_skip_sherpa_batch(lookups: list[dict]) -> list[dict] | None:
    """
    Send up to BATCH_SIZE PropertyLookup dicts to Skip Sherpa.
    Returns property_results list in the same order as lookups.
    Returns None on provider-level errors (HTTP 4xx/5xx, network failure) so
    callers can distinguish a genuine no-match from an API failure.
    Returns [] only when the API responded 200 but found no results.
    """
    try:
        resp = requests.put(
            PROPERTIES_ENDPOINT,
            headers=_headers(),
            json={"property_lookups": lookups},
            timeout=30,
        )
        if resp.status_code == 429:
            retry_after = max(int(resp.headers.get("Retry-After", 900)), 900)
            log.warning(f"Skip Sherpa rate limited — waiting {retry_after}s before retry")
            time.sleep(retry_after)
            return None
        resp.raise_for_status()
        return resp.json().get("property_results") or []
    except Exception as e:
        log.error(f"Skip Sherpa batch request failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Write enrichment back to DB
# ---------------------------------------------------------------------------

def _write_enrichment(lead_id: str, parsed: dict) -> None:
    """Write Skip Sherpa results to raw_leads and log the cost."""
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
        "verified_enriched":     True,
    }
    # None means "no data returned" — don't overwrite existing values with NULL.
    # False is a valid DNC value and must NOT be stripped.
    updates = {k: v for k, v in updates.items() if v is not None}
    update_row("raw_leads", lead_id, updates)

    result_label = "success" if parsed["mobile_found"] else "no_mobile"
    insert_row("api_costs", {
        "service":  "skip_sherpa",
        "lead_id":  lead_id,
        "cost_usd": COST_PER_CALL_USD,
        "result":   result_label,
    })


# ---------------------------------------------------------------------------
# Single-lead enrichment (used by waterfall.py Step 2 fallback)
# ---------------------------------------------------------------------------

def run_single(lead: dict) -> dict:
    """Enrich one lead via Skip Sherpa. Returns parsed result dict; does NOT write to DB.

    Sets provider_error=True when the API itself failed (HTTP error, network timeout)
    so the waterfall can fall back to Tracerfy. A genuine no-match returns
    mobile_found=False without provider_error.
    """
    lk = _build_property_lookup(lead)
    if not lk:
        return {"mobile_found": False}
    results = call_skip_sherpa_batch([lk])
    if results is None:
        # Provider-level error — signal the waterfall to try Tracerfy
        return {"mobile_found": False, "provider_error": True}
    if not results:
        return {"mobile_found": False}
    result = results[0]
    if result.get("status_code") != 200:
        return {"mobile_found": False}
    return _parse_result(result, lead.get("state") or "OH")


# ---------------------------------------------------------------------------
# Sample run — verbose dry run, nothing written to DB
# ---------------------------------------------------------------------------

def run_sample(n: int = 5, tier_filter: Optional[str] = None) -> None:
    """Pull n leads, call Skip Sherpa, print full results. No DB writes."""
    client = get_client()
    q = (
        client.table("raw_leads")
        .select("id, owner_name, property_address, geocoded_address, county, tier")
        .is_("phone_1", "null")
        .not_.is_("tier", "null")
        .limit(n)
    )
    if tier_filter:
        q = q.eq("tier", tier_filter.upper())
    leads = q.execute().data or []

    if not leads:
        print("No leads available for sample run")
        return

    valid_leads = []
    skipped_leads = []

    for lead in leads:
        lk = _build_property_lookup(lead)
        if lk:
            valid_leads.append((lead, lk))
        else:
            skipped_leads.append(lead)

    if skipped_leads:
        print(f"\n⚠  {len(skipped_leads)} lead(s) skipped — address unparseable:")
        for sl in skipped_leads:
            raw = sl.get("geocoded_address") or sl.get("property_address") or "(none)"
            print(f"   {sl['id'][:8]}  {(sl.get('owner_name') or '')[:40]}  addr={raw!r}")

    if not valid_leads:
        print("No valid lookups could be built — check address data")
        return

    lookups = [lk for _, lk in valid_leads]

    print(f"\nSending {len(lookups)} lookup(s) to Skip Sherpa...")
    print("\nPayload submitted:")
    for lead, lk in valid_leads:
        addr = lk["property_address_lookup"]
        name_block = lk.get("person_name_lookup") or lk.get("business_name_lookup") or {}
        print(
            f"  [{lead['id'][:8]}]  "
            f"name={name_block}  "
            f"addr={addr.get('street')}, {addr.get('city')}, {addr.get('state')} {addr.get('zipcode') or ''}"
        )

    results = call_skip_sherpa_batch(lookups)

    if not results:
        print("\n✗  API returned no results (check key, network, or API status)")
        return

    # ── Per-result display ─────────────────────────────────────────────────
    SEP = "═" * 72

    hits_mobile    = 0
    hits_any_phone = 0
    hits_email     = 0
    total          = len(results)

    print(f"\n{SEP}")
    print(f"  SKIP SHERPA SAMPLE — {total} result(s) returned")
    print(SEP)

    for idx, (res, (lead, lk)) in enumerate(zip(results, valid_leads), 1):
        prop   = res.get("property") or {}
        owners = prop.get("owners") or []
        person = (owners[0].get("person") or {}) if owners else {}

        raw_phones  = sorted(person.get("phone_numbers") or [], key=_phone_sort_key)
        raw_emails  = person.get("emails") or []
        mailing_us  = (prop.get("tax_mailing_address") or {}).get("us_address") or {}
        parsed      = _parse_result(res, "OH")
        status_code = res.get("status_code", "?")

        addr_in = lead.get("geocoded_address") or lead.get("property_address") or "(none)"

        # API-returned owner name (may differ from what's in our DB)
        pn = person.get("person_name") or {}
        api_owner = " ".join(filter(None, [
            pn.get("first_name"), pn.get("middle_name"), pn.get("last_name")
        ])).strip() or "(not returned)"

        if raw_phones:
            hits_any_phone += 1
        if parsed["mobile_found"]:
            hits_mobile += 1
        if raw_emails:
            hits_email += 1

        print(f"\n  [{idx}]  Lead ID  : {lead['id'][:8]}  (status={status_code})")
        print(f"        Our owner: {(lead.get('owner_name') or '').strip() or '(none)'}")
        print(f"        API owner: {api_owner}")
        print(f"        County   : {lead.get('county', '')}  |  Tier: {lead.get('tier', '')}")
        print(f"        Addr in  : {addr_in}")

        if raw_phones:
            print(f"        Phones   : {len(raw_phones)} returned")
            for ph in raw_phones:
                num      = ph.get("e164_format") or ""
                local    = ph.get("local_format") or ""
                ptype    = (ph.get("type") or "unknown").ljust(8)
                seen     = ph.get("last_seen") or ""
                dnc_list = ph.get("dnc_statuses") or []
                dnc      = "DNC" if any(d.get("is_dnc") for d in dnc_list) else "   "
                carrier  = (ph.get("carrier") or "")[:40]
                print(f"                   {local}  {num}  {ptype}  {dnc}  last={seen}  {carrier}")
        else:
            print(f"        Phones   : — none returned")

        if raw_emails:
            print(f"        Emails   :")
            for em in raw_emails:
                print(f"                   {em.get('email_address', '')}")
        else:
            print(f"        Emails   : — none returned")

        if mailing_us:
            mail_str = ", ".join(v for v in [
                mailing_us.get("street"), mailing_us.get("city"),
                mailing_us.get("state"),  mailing_us.get("zipcode"),
            ] if v)
            oos = "  ← OUT-OF-STATE" if parsed["owner_out_of_state"] else ""
            print(f"        Mail to  : {mail_str}{oos}")
        else:
            print(f"        Mail to  : — not returned")

        # What we'd write to DB
        p1_dnc = "DNC" if parsed["phone_1_dnc"] else ("clean" if parsed["phone_1_dnc"] is False else "—")
        p2_dnc = "DNC" if parsed["phone_2_dnc"] else ("clean" if parsed["phone_2_dnc"] is False else "—")
        print(f"        → phone_1 : {parsed['phone_1'] or '(none)'}  [{p1_dnc}]")
        print(f"        → phone_2 : {parsed['phone_2'] or '(none)'}  [{p2_dnc}]")
        print(f"        → phone_3 : {parsed['phone_3'] or '(none)'}")
        print(f"        → email   : {parsed['owner_email'] or '(none)'}")
        first = parsed["api_owner_first_name"] or ""
        last  = parsed["api_owner_last_name"] or ""
        print(f"        → owner   : {(first + ' ' + last).strip() or '(none)'}")

    print(f"\n{SEP}")
    print(f"  HIT RATE SUMMARY  (n={total})")
    print(f"  Mobile found   : {hits_mobile}/{total}  ({100*hits_mobile//total if total else 0}%)")
    print(f"  Any phone      : {hits_any_phone}/{total}  ({100*hits_any_phone//total if total else 0}%)")
    print(f"  Email found    : {hits_email}/{total}  ({100*hits_email//total if total else 0}%)")
    print(f"{SEP}")
    print(f"\n  ✓ Dry run complete — nothing written to database\n")


# ---------------------------------------------------------------------------
# Full batch run
# ---------------------------------------------------------------------------

def run_batch(tier_filter: Optional[str] = None, max_calls: int = 950) -> None:
    """
    Enrich all leads missing phone_1 with Skip Sherpa.

    max_calls: session cap — counts only API calls made in this run.
               Stops automatically when remaining < SAFETY_BUFFER (50).
    Always queries from offset 0 — each enriched lead sets phone_1 (non-null)
    and drops out of the filter naturally.
    """
    SAFETY_BUFFER  = 50
    PROGRESS_EVERY = 50

    log.info(f"Skip Sherpa batch starting (tier_filter={tier_filter or 'all'}  max_calls={max_calls})")
    enriched       = 0
    no_mobile      = 0
    skipped        = 0
    dnc_both       = 0
    total_proc     = 0
    api_calls_made = 0   # counts only real API calls this session

    def _print_progress(final: bool = False) -> None:
        tag = "FINAL" if final else f"{total_proc:>5}"
        remaining = max_calls - api_calls_made
        print(
            f"  [{tag}]  processed={total_proc}  api_calls={api_calls_made}  "
            f"mobile={enriched}  no_mobile={no_mobile}  "
            f"skipped={skipped}  both_dnc={dnc_both}  "
            f"credits_left≈{remaining}",
            flush=True,
        )

    print(f"\n{'─'*70}")
    print(f"  Skip Sherpa batch — tier={tier_filter or 'all'}  max_calls={max_calls}  safety_stop={SAFETY_BUFFER}")
    print(f"{'─'*70}")

    while True:
        remaining_credits = max_calls - api_calls_made
        if remaining_credits <= SAFETY_BUFFER:
            print(f"\n  ⚠  Safety threshold: {remaining_credits} credits left — stopping to preserve buffer")
            log.info(f"Safety threshold hit: {remaining_credits} credits remaining, stopping")
            break

        client     = get_client()
        fetch_size = min(PAGE_SIZE, remaining_credits - SAFETY_BUFFER)

        q = (
            client.table("raw_leads")
            .select("id, owner_name, property_address, geocoded_address, county, tier, state, "
                    "verification_notes")
            .is_("phone_1", "null")
            .not_.is_("tier", "null")
            .not_.is_("property_address", "null")
            .neq("property_address", "")
            # Exclude leads already attempted and marked by a prior run.
            .or_("verification_notes.is.null,verification_notes.not.like.*skip_sherpa*")
            .limit(fetch_size)
        )
        if tier_filter:
            q = q.eq("tier", tier_filter.upper())
        leads = q.execute().data or []

        if not leads:
            log.info("No more leads to enrich — done")
            break

        log.info(f"Processing {len(leads)} leads (api_calls_made={api_calls_made}  credits_left≈{max_calls - api_calls_made})")

        valid_pairs = []
        for lead in leads:
            lk = _build_property_lookup(lead)
            if lk:
                valid_pairs.append((lead, lk))
            else:
                skipped += 1
                total_proc += 1
                update_row("raw_leads", lead["id"], {
                    "verification_notes": (
                        ((lead.get("verification_notes") or "") + " | skip_sherpa_skip").strip(" | ")
                    )
                })

        if not valid_pairs:
            log.info("No valid lookups in this batch — stopping")
            break

        lookups = [lk for _, lk in valid_pairs]

        # Send in sub-batches of BATCH_SIZE; results are positionally aligned with lookups
        all_results: list[dict] = []
        for i in range(0, len(lookups), BATCH_SIZE):
            chunk   = lookups[i:i + BATCH_SIZE]
            results = call_skip_sherpa_batch(chunk)
            all_results.extend(results or [])
            api_calls_made += len(chunk)   # count every lookup sent, hit or miss
            time.sleep(REQUEST_DELAY_S)

        # Positional match: result[i] corresponds to valid_pairs[i]
        for (lead, _lk), res in zip(valid_pairs, all_results):
            lead_id = lead["id"]
            county  = lead.get("county") or ""

            if res.get("status_code") != 200:
                log.info(f"No result for {lead_id[:8]} (status={res.get('status_code')}) — marking")
                update_row("raw_leads", lead_id, {
                    "verification_notes": (
                        ((lead.get("verification_notes") or "") + " | skip_sherpa_no_result").strip(" | ")
                    )
                })
                skipped += 1
                total_proc += 1
                insert_row("api_costs", {
                    "service":  "skip_sherpa",
                    "lead_id":  lead_id,
                    "cost_usd": COST_PER_CALL_USD,
                    "result":   "failed",
                })
                if total_proc % PROGRESS_EVERY == 0:
                    _print_progress()
                continue

            parsed    = _parse_result(res, lead.get("state") or "OH")
            phone_str = parsed["phone_1"] or "none"
            email_str = parsed["owner_email"] or "none"

            _write_enrichment(lead_id, parsed)
            total_proc += 1

            # Track leads where every phone we found is on the DNC list
            p1_dnc = parsed["phone_1_dnc"]
            p2_dnc = parsed["phone_2_dnc"]
            if parsed["phone_1"] and p1_dnc and (p2_dnc is True or not parsed["phone_2"]):
                dnc_both += 1

            if parsed["mobile_found"]:
                enriched += 1
                log.info(
                    f"Enriched {lead_id[:8]} county={county}  "
                    f"phone={phone_str}  email={email_str}"
                )
            else:
                no_mobile += 1
                log.info(
                    f"No mobile {lead_id[:8]} county={county}  "
                    f"best_phone={phone_str}  email={email_str}"
                )

            if total_proc % PROGRESS_EVERY == 0:
                _print_progress()

    _print_progress(final=True)
    print(f"{'─'*70}\n")
    log.info(
        f"Skip Sherpa batch complete — enriched={enriched}  "
        f"no_mobile={no_mobile}  skipped={skipped}  both_dnc={dnc_both}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Skip Sherpa phone enrichment (Step 2)")
    parser.add_argument("--sample", type=int, metavar="N",
                        help="Dry-run: call API for N leads, print full results, no DB writes")
    parser.add_argument("--all",  action="store_true",
                        help="Run full batch up to 500 calls/day")
    parser.add_argument("--tier", metavar="TIER",
                        help="Filter to a specific tier: A, B, or C")
    args = parser.parse_args()

    if args.sample:
        run_sample(n=args.sample, tier_filter=args.tier)
    elif args.all:
        run_batch(tier_filter=args.tier)
    else:
        parser.print_help()
