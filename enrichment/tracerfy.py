"""
enrichment/tracerfy.py — Tracerfy skip tracing (Step 2 provider).

Two modes:
  Batch (async) — $0.02/hit — use for bulk backlog enrichment
  Sync          — $0.10/hit — use in the daily waterfall for individual leads

Batch flow:
  1. Submit JSON payload to POST /v1/api/trace/
  2. Get queue_id back
  3. Poll GET /v1/api/queue/:id until status = "completed"
  4. Parse flat results (mobile_1..5, landline_1..3) and write to DB

API docs: https://tracerfy.com/skip-tracing-api
API key:  TRACERFY_API_KEY in .env

CLI:
    python enrichment/tracerfy.py --sample 5          # sync dry run, no DB writes
    python enrichment/tracerfy.py --all               # async batch, all leads missing phones
    python enrichment/tracerfy.py --all --tier A,B    # Tier A and B only
    python enrichment/tracerfy.py --credits           # check account balance
"""

import argparse
import json
import os
import re
import time
from typing import Optional

import requests
from dotenv import load_dotenv

from db.client import get_client, insert_row, update_row
from utils.logger import get_logger

load_dotenv()
log = get_logger("enrichment.tracerfy")

BASE_URL          = "https://tracerfy.com/v1/api"
SYNC_ENDPOINT     = f"{BASE_URL}/trace/lookup/"
BATCH_ENDPOINT    = f"{BASE_URL}/trace/"
QUEUE_ENDPOINT    = f"{BASE_URL}/queue"
ANALYTICS_URL     = f"{BASE_URL}/analytics/"

COST_PER_HIT_BATCH = 0.02
COST_PER_HIT_SYNC  = 0.10
PAGE_SIZE          = 300    # leads fetched per batch — keeps each job under 5 min
POLL_INTERVAL_S    = 15     # seconds between batch status polls
POLL_TIMEOUT_S     = 1800   # 30 minutes max wait per batch
PROGRESS_EVERY     = 100    # print progress every N processed leads
SAFETY_BUFFER      = 25     # stop batch loop when credits_left <= this

TRACERFY_AVAILABLE = bool(os.getenv("TRACERFY_API_KEY"))


def _api_key() -> str:
    key = os.getenv("TRACERFY_API_KEY", "")
    if not key:
        raise RuntimeError("TRACERFY_API_KEY is not set in .env")
    return key


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


# ---------------------------------------------------------------------------
# Address parsing
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
        "street": m.group("street").strip().title(),
        "city":   m.group("city").strip().title(),
        "state":  m.group("state").upper(),
        "zip":    (m.group("zip") or "").strip() or None,
    }


def _parse_owner_name(owner_name: str) -> tuple[str, str]:
    """Return (first_name, last_name) from a raw owner_name string."""
    name = (owner_name or "").strip()
    if not name:
        return "", ""
    if "," in name:                          # "SMITH, JOHN WILLIAM"
        last, rest = name.split(",", 1)
        parts = rest.strip().split()
        return (parts[0].title() if parts else ""), last.strip().title()
    parts = name.split()
    if len(parts) >= 2:
        return parts[0].title(), parts[-1].title()
    return "", name.title()


# ---------------------------------------------------------------------------
# Account analytics
# ---------------------------------------------------------------------------

def get_credits() -> Optional[int]:
    """Return current credit balance, or None on error."""
    try:
        resp = requests.get(ANALYTICS_URL, headers=_headers(), timeout=15)
        resp.raise_for_status()
        return resp.json().get("balance")
    except Exception as e:
        log.error(f"Tracerfy analytics request failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Sync endpoint — single lead (used by waterfall.py per-lead enrichment)
# ---------------------------------------------------------------------------

def _parse_sync_result(data: dict, property_state: str = "OH") -> dict:
    """Parse the nested sync response into our standard enrichment dict."""
    out = {
        "phone_1": None, "phone_2": None, "phone_3": None,
        "phone_1_dnc": None, "phone_2_dnc": None,
        "owner_email": None, "owner_mailing_address": None,
        "owner_out_of_state": False,
        "api_owner_first_name": None, "api_owner_last_name": None,
        "litigator": False,
        "mobile_found": False,
    }

    persons = data.get("persons") or []
    if not persons:
        return out

    person = persons[0]
    out["litigator"] = bool(person.get("litigator"))

    # Phones — sorted by rank, mobile-first within same rank
    def _phone_rank(ph: dict) -> tuple:
        type_rank = 0 if (ph.get("type") or "").lower() == "mobile" else 1
        return (ph.get("rank", 99), type_rank)

    phones = sorted(person.get("phones") or [], key=_phone_rank)

    # Normalise to E.164 (+1XXXXXXXXXX) — Tracerfy returns 10-digit strings
    def _to_e164(num: str) -> Optional[str]:
        digits = re.sub(r"\D", "", num or "")
        if len(digits) == 10:
            return f"+1{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        return None

    if phones:
        out["phone_1"]      = _to_e164(phones[0].get("number", ""))
        out["phone_1_dnc"]  = bool(phones[0].get("dnc"))
        out["mobile_found"] = (phones[0].get("type") or "").lower() == "mobile"
    if len(phones) > 1:
        out["phone_2"]     = _to_e164(phones[1].get("number", ""))
        out["phone_2_dnc"] = bool(phones[1].get("dnc"))
    if len(phones) > 2:
        out["phone_3"] = _to_e164(phones[2].get("number", ""))

    emails = sorted(person.get("emails") or [], key=lambda e: e.get("rank", 99))
    if emails:
        out["owner_email"] = emails[0].get("email")

    mailing = person.get("mailing_address") or {}
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

    out["api_owner_first_name"] = (person.get("first_name") or "").strip() or None
    out["api_owner_last_name"]  = (person.get("last_name")  or "").strip() or None

    return out


_COUNTY_FALLBACK_CITY = {
    "cuyahoga": ("Cleveland", "OH"),
    "lake":     ("Painesville", "OH"),
    "mahoning": ("Youngstown", "OH"),
}


def run_tracerfy(lead: dict) -> dict:
    """Enrich a single lead via Tracerfy sync endpoint.

    Called by waterfall.py as Step 2. Costs $0.10/hit.
    Returns parsed enrichment dict (with mobile_found flag).
    """
    raw_addr = lead.get("geocoded_address") or lead.get("property_address") or ""
    addr = _parse_address(raw_addr)
    if not addr:
        # Probate addresses are often bare street addresses (no city/state).
        # Fall back to the county's primary city so Tracerfy can resolve the property.
        county = (lead.get("county") or "").lower()
        fallback = _COUNTY_FALLBACK_CITY.get(county)
        if fallback and raw_addr.strip():
            addr = {"street": raw_addr.strip(), "city": fallback[0], "state": fallback[1], "zip": None}
            log.debug(f"Address city fallback for lead {lead['id'][:8]}: {fallback[0]}, {fallback[1]}")
        else:
            log.warning(f"Cannot parse address for lead {lead['id'][:8]}: {raw_addr!r}")
            return {"mobile_found": False}

    first, last = _parse_owner_name(lead.get("owner_name") or "")
    payload: dict = {
        "address":     addr["street"],
        "city":        addr["city"],
        "state":       addr["state"],
        "find_owner":  True,
    }
    if addr["zip"]:
        payload["zip"] = addr["zip"]
    if first:
        payload["first_name"] = first
    if last:
        payload["last_name"] = last

    try:
        resp = requests.post(SYNC_ENDPOINT, headers=_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Tracerfy sync request failed for lead {lead['id'][:8]}: {e}")
        # Signal a provider-level error so the waterfall can fall back to Skip Sherpa
        # rather than treating this as a genuine "no match" result.
        return {"mobile_found": False, "provider_error": True}

    if not data.get("hit"):
        insert_row("api_costs", {
            "service": "tracerfy", "lead_id": lead["id"],
            "cost_usd": 0.0, "result": "failed",
        })
        return {"mobile_found": False}

    parsed = _parse_sync_result(data, lead.get("state") or "OH")
    _write_enrichment(lead["id"], parsed, cost=COST_PER_HIT_SYNC)
    return parsed


# ---------------------------------------------------------------------------
# Batch endpoint — async bulk enrichment (used for the backlog)
# ---------------------------------------------------------------------------

def _build_batch_row(lead: dict) -> Optional[dict]:
    """Build a single row dict for the batch JSON payload."""
    raw_addr = lead.get("geocoded_address") or lead.get("property_address") or ""
    addr = _parse_address(raw_addr)
    if not addr:
        return None
    first, last = _parse_owner_name(lead.get("owner_name") or "")
    row = {
        "id":           lead["id"],      # passed through — used to match results
        "address":      addr["street"],
        "city":         addr["city"],
        "state":        addr["state"],
        "zip":          addr["zip"] or "",
        "first_name":   first,
        "last_name":    last,
        "mail_address": "",
        "mail_city":    "",
        "mail_state":   "",
        "mail_zip":     "",
    }
    return row


def _submit_batch(rows: list[dict]) -> Optional[tuple[int, int]]:
    """Submit a batch to Tracerfy. Returns (queue_id, estimated_wait_seconds) or None on failure."""
    auth_headers = {"Authorization": f"Bearer {_api_key()}"}
    form_data = {
        "json_data":              json.dumps(rows),
        "address_column":         "address",
        "city_column":            "city",
        "state_column":           "state",
        "zip_column":             "zip",
        "first_name_column":      "first_name",
        "last_name_column":       "last_name",
        "mail_address_column":    "mail_address",
        "mail_city_column":       "mail_city",
        "mail_state_column":      "mail_state",
        "mail_zip_column":        "mail_zip",
        "trace_type":             "normal",
    }
    try:
        resp = requests.post(BATCH_ENDPOINT, headers=auth_headers, data=form_data, timeout=60)
        if not resp.ok:
            log.error(f"Tracerfy batch submit failed: {resp.status_code} — {resp.text[:500]}")
            return None
        data = resp.json()
        queue_id = data.get("queue_id")
        est_wait = int(data.get("estimated_wait_seconds") or POLL_INTERVAL_S)
        log.info(f"Batch submitted — queue_id={queue_id}  rows={data.get('rows_uploaded')}  "
                 f"est_wait={est_wait}s")
        return queue_id, est_wait
    except Exception as e:
        log.error(f"Tracerfy batch submit failed: {e}")
        return None


def _poll_batch(queue_id: int, initial_wait: int = 30) -> Optional[list[dict]]:
    """Poll until the batch job completes. Returns result rows or None on timeout/error.

    Sleeps for initial_wait seconds before the first poll — Tracerfy needs time to start
    processing and returns [] immediately if polled too soon, which looks like 0 results.
    """
    log.info(f"Waiting {initial_wait}s before first poll (est. processing time)...")
    time.sleep(initial_wait)

    deadline = time.time() + POLL_TIMEOUT_S
    url = f"{QUEUE_ENDPOINT}/{queue_id}"
    seen_pending = False   # True once we've confirmed the job is/was in queue

    while time.time() < deadline:
        try:
            resp = requests.get(url, headers=_headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                if len(data) > 0:
                    # Non-empty list — definitive completion with results
                    log.info(f"Batch {queue_id} complete — {len(data)} hits returned")
                    return data
                else:
                    # Empty list: complete with 0 hits if we already saw a pending status,
                    # otherwise it's too early — Tracerfy returns [] before processing starts
                    if seen_pending:
                        log.info(f"Batch {queue_id} complete — 0 hits (no matches found)")
                        return []
                    log.debug(f"Batch {queue_id} returned [] before pending state — waiting...")
                    time.sleep(POLL_INTERVAL_S)
                    continue

            # Status dict — job is queued or processing
            status = data.get("status", "pending")
            pending = data.get("pending", "?")
            seen_pending = True
            log.debug(f"Batch {queue_id} status={status}  pending={pending}")

            if status == "failed":
                log.error(f"Batch {queue_id} failed on Tracerfy side")
                return None

        except Exception as e:
            log.warning(f"Tracerfy poll error for queue {queue_id}: {e}")

        time.sleep(POLL_INTERVAL_S)

    log.error(f"Batch {queue_id} timed out after {POLL_TIMEOUT_S}s")
    return None


def _parse_batch_row(row: dict, property_state: str = "OH") -> dict:
    """Parse a flat batch result row into our standard enrichment dict."""
    out = {
        "phone_1": None, "phone_2": None, "phone_3": None,
        "phone_1_dnc": None, "phone_2_dnc": None,
        "owner_email": None, "owner_mailing_address": None,
        "owner_out_of_state": False,
        "api_owner_first_name": None, "api_owner_last_name": None,
        "mobile_found": False,
    }

    def _to_e164(num: str) -> Optional[str]:
        digits = re.sub(r"\D", "", num or "")
        if len(digits) == 10:
            return f"+1{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        return None

    # Prefer mobiles first, then landlines
    phones_in_order = []
    for field in ["mobile_1", "mobile_2", "mobile_3", "mobile_4", "mobile_5",
                  "landline_1", "landline_2", "landline_3"]:
        val = (row.get(field) or "").strip()
        if val:
            is_mobile = field.startswith("mobile")
            phones_in_order.append((val, is_mobile))

    # Also check primary_phone if not covered above
    primary = (row.get("primary_phone") or "").strip()
    primary_type = (row.get("primary_phone_type") or "").lower()
    if primary and not phones_in_order:
        phones_in_order.append((primary, primary_type == "mobile"))

    if phones_in_order:
        p1_num, p1_mobile = phones_in_order[0]
        out["phone_1"]     = _to_e164(p1_num)
        out["mobile_found"] = p1_mobile
    if len(phones_in_order) > 1:
        out["phone_2"] = _to_e164(phones_in_order[1][0])
    if len(phones_in_order) > 2:
        out["phone_3"] = _to_e164(phones_in_order[2][0])

    for i, key in enumerate(["email_1", "email_2"], start=1):
        val = (row.get(key) or "").strip()
        if val:
            out["owner_email"] = val
            break

    mail_parts = [
        row.get("mail_address") or "",
        row.get("mail_city")    or "",
        row.get("mail_state")   or "",
    ]
    mail_str = ", ".join(p for p in mail_parts if p)
    if mail_str:
        out["owner_mailing_address"] = mail_str
        mail_state = (row.get("mail_state") or "").upper()
        if mail_state and mail_state != property_state.upper():
            out["owner_out_of_state"] = True

    out["api_owner_first_name"] = (row.get("first_name") or "").strip() or None
    out["api_owner_last_name"]  = (row.get("last_name")  or "").strip() or None

    return out


# ---------------------------------------------------------------------------
# Write enrichment to DB
# ---------------------------------------------------------------------------

def _write_enrichment(lead_id: str, parsed: dict, cost: float = COST_PER_HIT_BATCH) -> None:
    updates = {
        "phone_1":               parsed["phone_1"],
        "phone_1_dnc":           parsed["phone_1_dnc"],
        "phone_2":               parsed["phone_2"],
        "phone_2_dnc":           parsed["phone_2_dnc"],
        "phone_3":               parsed["phone_3"],
        "litigator":             parsed.get("litigator", False),
        "owner_email":           parsed["owner_email"],
        "owner_mailing_address": parsed["owner_mailing_address"],
        "owner_out_of_state":    parsed["owner_out_of_state"],
        "api_owner_first_name":  parsed["api_owner_first_name"],
        "api_owner_last_name":   parsed["api_owner_last_name"],
        "enrichment_step":       2,
        "enriched":              True,
        "verified_enriched":     True,
    }
    # Keep False values (DNC, litigator) — only strip None
    updates = {k: v for k, v in updates.items() if v is not None}
    update_row("raw_leads", lead_id, updates)

    try:
        insert_row("api_costs", {
            "service":  "tracerfy",
            "lead_id":  lead_id,
            "cost_usd": cost if parsed.get("mobile_found") or parsed.get("phone_1") else 0.0,
            "result":   "success" if parsed.get("mobile_found") else
                        ("no_mobile" if parsed.get("phone_1") else "failed"),
        })
    except Exception as e:
        log.warning(f"Could not log api_costs for lead {lead_id[:8]}: {e}")


# ---------------------------------------------------------------------------
# Sample dry run — sync, no DB writes
# ---------------------------------------------------------------------------

def run_sample(n: int = 5, tiers: Optional[list[str]] = None) -> None:
    """Pull n leads, call the sync endpoint, print results. Nothing written to DB."""
    client = get_client()
    q = (
        client.table("raw_leads")
        .select("id, owner_name, property_address, geocoded_address, county, tier, state")
        .is_("phone_1", "null")
        .not_.is_("tier", "null")
        .limit(n)
    )
    if tiers:
        q = q.in_("tier", tiers)
    leads = q.execute().data or []

    if not leads:
        print("No leads available for sample run")
        return

    credits = get_credits()
    print(f"\nTracerfy account balance: {credits} credits")
    print(f"Sending {len(leads)} sync lookups (${COST_PER_HIT_SYNC:.2f}/hit, dry run)...\n")

    hits_mobile = hits_any = hits_email = 0
    SEP = "═" * 72
    print(SEP)

    for i, lead in enumerate(leads, 1):
        raw_addr = lead.get("geocoded_address") or lead.get("property_address") or ""
        addr = _parse_address(raw_addr)
        if not addr:
            print(f"[{i}] SKIP — cannot parse address: {raw_addr!r}")
            continue

        first, last = _parse_owner_name(lead.get("owner_name") or "")
        payload: dict = {"address": addr["street"], "city": addr["city"],
                         "state": addr["state"], "find_owner": True}
        if addr["zip"]: payload["zip"] = addr["zip"]
        if first:       payload["first_name"] = first
        if last:        payload["last_name"]  = last

        try:
            resp = requests.post(SYNC_ENDPOINT, headers=_headers(), json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[{i}] ERROR — {e}")
            continue

        if not data.get("hit"):
            print(f"[{i}] MISS  — {lead.get('owner_name', '')} | {raw_addr}")
            continue

        parsed = _parse_sync_result(data, lead.get("state") or "OH")
        if parsed["phone_1"]:   hits_any    += 1
        if parsed["mobile_found"]: hits_mobile += 1
        if parsed["owner_email"]:  hits_email  += 1

        person = (data.get("persons") or [{}])[0]
        litigator = "⚠ LITIGATOR" if person.get("litigator") else ""
        print(f"[{i}] HIT   {litigator}")
        print(f"     Owner : {person.get('full_name', '')} | County: {lead.get('county','')} | Tier: {lead.get('tier','')}")
        print(f"     Addr  : {raw_addr}")
        for ph in sorted(person.get("phones") or [], key=lambda p: p.get("rank", 99)):
            dnc = "DNC" if ph.get("dnc") else "   "
            print(f"     Phone : {ph.get('number','')}  {ph.get('type',''):8}  {dnc}  carrier={ph.get('carrier','')}")
        if parsed["owner_email"]:
            print(f"     Email : {parsed['owner_email']}")
        if parsed["owner_mailing_address"]:
            oos = " ← OUT-OF-STATE" if parsed["owner_out_of_state"] else ""
            print(f"     Mail  : {parsed['owner_mailing_address']}{oos}")
        print()

    total = len(leads)
    print(SEP)
    print(f"  Mobile: {hits_mobile}/{total}  |  Any phone: {hits_any}/{total}  |  Email: {hits_email}/{total}")
    print(f"  ✓ Dry run — nothing written to database")
    print(SEP + "\n")


# ---------------------------------------------------------------------------
# Full async batch run — $0.02/hit
# ---------------------------------------------------------------------------

def _post_batch_route_and_export() -> None:
    """Score newly enriched leads, route Tier A/B/C to VA sheet + Mojo CSV."""
    from scoring.score import run_batch_scoring
    from routing.va_router import route_backlog
    from routing.mojo_export import export_leads

    sep = "─" * 70
    print(f"\n{sep}")
    print("  Post-batch: scoring newly enriched leads...")
    run_batch_scoring(rescore=False)

    # Route all new Tier A/B/C leads with phones to VA sheet + Mojo
    client = get_client()
    q = (
        client.table("raw_leads")
        .select("*")
        .in_("tier", ["A", "B", "C"])
        .eq("routed_to_va", False)
        .not_.is_("phone_1", "null")
        .eq("verified_raw", True)
        .eq("enriched", True)
        .neq("litigator", True)
    )

    to_route: list[dict] = []
    offset = 0
    while True:
        page = q.range(offset, offset + 999).execute().data or []
        to_route.extend(page)
        if len(page) < 1000:
            break
        offset += 1000

    if to_route:
        print(f"  Routing {len(to_route)} leads (Tier A/B/C) to VA sheet...")
        routed, dnc_held = route_backlog(to_route)
        print(f"  ✓ VA sheet: {routed} routed  |  {dnc_held} held in DNC review")
    else:
        print("  No new leads to route")

    print("  Exporting Mojo CSV...")
    export_path = export_leads(new_only=True)
    print(f"  ✓ Mojo CSV ready: {export_path}")
    print(f"{sep}\n")


def run_batch(tiers: Optional[list[str]] = None, max_leads: int = 50_000,
              auto_route: bool = True) -> None:
    """Enrich all leads missing phones via Tracerfy batch endpoint ($0.02/hit).

    Submits leads in pages, waits for each batch to complete before fetching next page.
    Caps each batch at available credit balance so the API never rejects on insufficient credits.
    When auto_route=True (default), scores and routes new leads to the VA sheet + Mojo CSV
    automatically after the batch completes.
    """
    log.info(f"Tracerfy batch starting — tiers={tiers or 'all'}  max_leads={max_leads}")

    credits = get_credits()
    print(f"\nTracerfy account balance: {credits} credits")
    if credits is not None and credits <= SAFETY_BUFFER:
        print(f"  ⚠ Insufficient credits ({credits} ≤ safety buffer {SAFETY_BUFFER}). "
              f"Top up at tracerfy.com and retry.")
        return
    print(f"{'─'*70}")
    print(f"  Tracerfy batch — tiers={tiers or 'all'}  max_leads={max_leads}")
    print(f"  Cost: $0.02/hit (1 credit/hit at normal tier)")
    print(f"{'─'*70}\n")

    total_submitted = 0
    total_hits      = 0
    total_mobile    = 0
    total_skipped   = 0
    offset          = 0

    while total_submitted < max_leads:
        client = get_client()

        # Refresh credits each loop so we cap accurately after previous batches consumed some
        current_credits = get_credits()
        if current_credits is not None and current_credits <= SAFETY_BUFFER:
            print(f"  Credits exhausted ({current_credits} remaining) — stopping batch. "
                  f"Top up at tracerfy.com to continue.")
            break

        available = (current_credits - SAFETY_BUFFER) if current_credits is not None else PAGE_SIZE
        fetch_size = min(PAGE_SIZE, max_leads - total_submitted, available)

        q = (
            client.table("raw_leads")
            .select("id, owner_name, property_address, geocoded_address, county, tier, state")
            .is_("phone_1", "null")
            .not_.is_("tier", "null")
            .not_.is_("property_address", "null")
            .or_("verification_notes.is.null,verification_notes.not.like.*tracerfy*")
            .limit(fetch_size)
            .offset(offset)
        )
        if tiers:
            q = q.in_("tier", tiers)
        leads = q.execute().data or []

        if not leads:
            log.info("No more leads to enrich — done")
            break

        # Build batch rows, track which lead IDs had unparseable addresses
        rows, skip_ids = [], []
        lead_map = {}
        for lead in leads:
            row = _build_batch_row(lead)
            if row:
                rows.append(row)
                lead_map[lead["id"]] = lead
            else:
                skip_ids.append(lead["id"])
                total_skipped += 1

        # Mark skipped leads so they don't re-appear next run
        for lid in skip_ids:
            lead = next((l for l in leads if l["id"] == lid), {})
            update_row("raw_leads", lid, {
                "verification_notes": (
                    ((lead.get("verification_notes") or "") + " | tracerfy_skip").strip(" | ")
                )
            })

        if not rows:
            offset += len(leads)
            continue

        # Submit batch
        submit_result = _submit_batch(rows)
        if not submit_result:
            log.error("Batch submission failed — stopping")
            break
        queue_id, est_wait = submit_result

        total_submitted += len(rows)
        print(f"  Batch submitted: {len(rows)} leads → queue_id={queue_id}  "
              f"(total submitted: {total_submitted})")

        # Poll until complete — wait estimated time first so we don't read [] prematurely
        results = _poll_batch(queue_id, initial_wait=est_wait)
        if results is None:
            log.error(f"Batch {queue_id} did not complete — stopping")
            break

        # Build result lookup by ID (if Tracerfy echoes it back) and by address (fallback).
        # Tracerfy does not guarantee it echoes the "id" field — address matching is the
        # primary strategy.
        if results:
            log.debug(f"Sample result row keys: {list(results[0].keys())}")

        result_by_id: dict = {}
        result_by_addr: dict = {}
        for r in results:
            row_id = r.get("id")
            if row_id:
                result_by_id[str(row_id)] = r
            addr_key = (
                (r.get("address") or "").lower().strip(),
                (r.get("city")    or "").lower().strip(),
                (r.get("state")   or "").upper().strip(),
            )
            if any(addr_key):
                result_by_addr[addr_key] = r

        # Also build lookup from submitted row address → lead_id
        row_by_lead_id = {r["id"]: r for r in rows}

        batch_hits = batch_mobile = 0

        for lead_id, lead in lead_map.items():
            # Try ID match first, then fall back to address match
            result_row = result_by_id.get(lead_id)
            if not result_row:
                submitted = row_by_lead_id.get(lead_id, {})
                addr_key = (
                    (submitted.get("address") or "").lower().strip(),
                    (submitted.get("city")    or "").lower().strip(),
                    (submitted.get("state")   or "").upper().strip(),
                )
                result_row = result_by_addr.get(addr_key)

            if not result_row:
                # No hit for this lead
                update_row("raw_leads", lead_id, {
                    "enriched": True,
                    "verification_notes": (
                        ((lead.get("verification_notes") or "") + " | tracerfy_no_hit").strip(" | ")
                    ),
                })
                insert_row("api_costs", {
                    "service": "tracerfy", "lead_id": lead_id,
                    "cost_usd": 0.0, "result": "failed",
                })
                continue

            parsed = _parse_batch_row(result_row, lead.get("state") or "OH")
            _write_enrichment(lead_id, parsed, cost=COST_PER_HIT_BATCH)

            batch_hits += 1
            total_hits += 1
            if parsed["mobile_found"]:
                batch_mobile += 1
                total_mobile += 1

        print(f"  Batch {queue_id} done — hits={batch_hits}/{len(rows)}  "
              f"mobile={batch_mobile}  running_total={total_hits} hits  "
              f"cost≈${total_hits * COST_PER_HIT_BATCH:.2f}")

        # If we got fewer results than we submitted, we've likely exhausted available leads
        if len(results) < len(rows) * 0.1:
            log.info("Very low hit rate on last batch — may be at end of enrichable leads")

        offset += len(leads)

    print(f"\n{'─'*70}")
    print(f"  COMPLETE — submitted={total_submitted}  hits={total_hits}  "
          f"mobile={total_mobile}  skipped={total_skipped}")
    print(f"  Total cost ≈ ${total_hits * COST_PER_HIT_BATCH:.2f}")
    print(f"{'─'*70}\n")
    log.info(f"Tracerfy batch complete — submitted={total_submitted} hits={total_hits} "
             f"mobile={total_mobile} cost≈${total_hits * COST_PER_HIT_BATCH:.2f}")

    if auto_route and total_hits > 0:
        _post_batch_route_and_export()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tracerfy skip tracing")
    parser.add_argument("--sample",  type=int, metavar="N",
                        help="Sync dry run for N leads — prints results, no DB writes")
    parser.add_argument("--all",     action="store_true",
                        help="Run async batch on all leads missing phones")
    parser.add_argument("--tier",    metavar="TIERS",
                        help="Comma-separated tiers: A,B or A,B,C")
    parser.add_argument("--limit",   type=int, default=50_000,
                        help="Max leads to submit this run (default 50,000)")
    parser.add_argument("--credits", action="store_true",
                        help="Check account credit balance and exit")
    parser.add_argument("--no-auto-route", action="store_true",
                        help="Skip scoring/routing/Mojo export after batch (enrichment only)")
    args = parser.parse_args()

    if args.credits:
        bal = get_credits()
        print(f"Tracerfy balance: {bal} credits  (≈ ${bal * 0.02:.2f} at $0.02/hit)")
    elif args.sample:
        tiers = [t.strip().upper() for t in args.tier.split(",")] if args.tier else None
        run_sample(n=args.sample, tiers=tiers)
    elif args.all:
        tiers = [t.strip().upper() for t in args.tier.split(",")] if args.tier else None
        run_batch(tiers=tiers, max_leads=args.limit, auto_route=not args.no_auto_route)
    else:
        parser.print_help()
