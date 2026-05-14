"""
enrichment/google_maps.py — Step 1: Google Maps address verification + Street View.

For every lead where verified_enriched=false and property_address is not null:
  1. Google Maps Geocoding API  → validate, standardize, write lat/lng back
  2. Google Street View Static  → build thumbnail URL (400x300), write to street_view_url
  3. Google Maps hyperlink      → build maps_url for easy one-click review
  4. Set verified_enriched=true only if geocoding returns status OK

Pricing (pay-per-request after free $200/month credit):
  Geocoding:          $0.005 / request (~40,000 free requests/month)
  Street View Static: URL is free to generate; charged only when rendered as an image.

Requires: GOOGLE_MAPS_API_KEY in .env

CLI:
    # Dry run — geocode 10 leads, print results, write nothing to Supabase
    python enrichment/google_maps.py --sample 10

    # Full batch — process all eligible leads
    python enrichment/google_maps.py --all
"""

import argparse
import os
import time
import urllib.parse

import requests
from dotenv import load_dotenv

from db.client import get_client, update_row
from utils.logger import get_logger

load_dotenv()
log = get_logger("enrichment.google_maps")

GEOCODE_URL   = "https://maps.googleapis.com/maps/api/geocode/json"
STREET_VIEW_BASE = "https://maps.googleapis.com/maps/api/streetview"
MAPS_LINK_BASE   = "https://www.google.com/maps/search/?api=1&query="

# 50ms between requests — well within Google's 50 QPS limit and free of charge as spacing
REQUEST_DELAY_S = 0.05


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not key:
        raise EnvironmentError("GOOGLE_MAPS_API_KEY is not set in .env")
    return key


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def geocode_address(address: str) -> dict | None:
    """Call Google Maps Geocoding API.

    Returns dict with standardized_address, lat, lng on success.
    Returns None on network error or non-OK status (ZERO_RESULTS, INVALID_REQUEST, etc.).
    """
    params = {"address": address, "key": _api_key()}
    try:
        resp = requests.get(GEOCODE_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Geocoding request failed for '{address}': {e}")
        return None

    status = data.get("status")
    if status != "OK":
        log.warning(f"Geocoding status '{status}' for '{address}'")
        return None

    result = data["results"][0]
    loc    = result["geometry"]["location"]
    return {
        "standardized_address": result["formatted_address"],
        "lat": loc["lat"],
        "lng": loc["lng"],
    }


def build_street_view_url(lat: float, lng: float) -> str:
    """Return a Google Street View Static API thumbnail URL (400×300).

    The URL includes the API key but is only billed when a browser renders the image.
    Uses return_error_codes=true so a 404 is returned for locations with no imagery
    rather than the generic grey placeholder.
    """
    params = {
        "size": "400x300",
        "location": f"{lat},{lng}",
        "key": _api_key(),
        "return_error_codes": "true",
    }
    return f"{STREET_VIEW_BASE}?{urllib.parse.urlencode(params)}"


def build_maps_url(address: str) -> str:
    """Return a Google Maps search hyperlink for one-click property review."""
    return MAPS_LINK_BASE + urllib.parse.quote_plus(address)


# ---------------------------------------------------------------------------
# Per-lead enrichment
# ---------------------------------------------------------------------------

def enrich_lead(lead: dict) -> dict:
    """Geocode one lead and return the DB update dict.

    Returns updates dict with all geocoding fields set.
    verified_enriched is True only if geocoding succeeded.
    Does not write to the database — caller decides whether to persist.
    """
    address = (lead.get("property_address") or "").strip()
    state   = (lead.get("state") or "OH").strip()

    if not address:
        return {
            "verified_enriched": False,
            "verification_notes": "property_address is null — skipped geocoding",
        }

    # Append state code if not already present — narrows geocoding to Ohio
    search_address = address if state in address else f"{address}, {state}"

    geo = geocode_address(search_address)
    if not geo:
        notes = (lead.get("verification_notes") or "")
        suffix = f"Geocoding failed for '{address}'"
        return {
            "verified_enriched": False,
            "verification_notes": f"{notes} | {suffix}".strip(" | "),
        }

    log.info(
        f"Geocoded '{address}' → '{geo['standardized_address']}' "
        f"({geo['lat']:.5f}, {geo['lng']:.5f})"
    )
    return {
        "geocoded_address": geo["standardized_address"],
        "geocoded_lat":     geo["lat"],
        "geocoded_lng":     geo["lng"],
        "street_view_url":  build_street_view_url(geo["lat"], geo["lng"]),
        "maps_url":         build_maps_url(geo["standardized_address"]),
        "verified_enriched": True,
    }


# ---------------------------------------------------------------------------
# Sample run (--sample N) — dry run, no DB writes
# ---------------------------------------------------------------------------

def run_sample(n: int = 10) -> None:
    """Geocode n leads and print a results table. Writes nothing to Supabase."""
    client = get_client()
    leads = (
        client.table("raw_leads")
        .select("id, property_address, county, state, owner_name, source_type, verification_notes")
        .eq("verified_enriched", False)
        .not_.is_("property_address", "null")
        .or_("verification_notes.is.null,verification_notes.not.like.*geocoding_attempted=true*")
        .limit(n)
        .execute()
        .data or []
    )

    if not leads:
        log.info("No eligible leads found for sample run")
        return

    log.info(f"Sample run — geocoding {len(leads)} leads (dry run, no DB writes)")

    rows = []
    for lead in leads:
        updates = enrich_lead(lead)
        rows.append({
            "id":         lead["id"],
            "owner":      (lead.get("owner_name") or "")[:35],
            "county":     lead.get("county", ""),
            "source":     lead.get("source_type", ""),
            "input":      lead.get("property_address", ""),
            "geocoded":   updates.get("geocoded_address", "FAILED"),
            "ok":         updates.get("verified_enriched", False),
            "maps_url":   updates.get("maps_url", ""),
            "sv_url":     (updates.get("street_view_url") or "")[:80],
        })
        time.sleep(REQUEST_DELAY_S)

    ok    = [r for r in rows if r["ok"]]
    fails = [r for r in rows if not r["ok"]]

    print(f"\n{'═'*90}")
    print(f"  GOOGLE MAPS SAMPLE RUN — {len(rows)} leads")
    print(f"  Succeeded: {len(ok)}   Failed: {len(fails)}")
    print(f"{'═'*90}")
    for r in rows:
        marker = "✓" if r["ok"] else "✗"
        print(f"\n  [{marker}] {r['id']}")
        print(f"       Owner:    {r['owner']}")
        print(f"       County:   {r['county']}  Source: {r['source']}")
        print(f"       Input:    {r['input']}")
        print(f"       Geocoded: {r['geocoded']}")
        if r["maps_url"]:
            print(f"       Maps:     {r['maps_url']}")
        if r["sv_url"]:
            print(f"       StrView:  {r['sv_url']}...")
    print(f"\n{'═'*90}")
    print("  Dry run complete — no writes to Supabase.")
    print("  Run with --all to process the full database.")
    print(f"{'═'*90}\n")


# ---------------------------------------------------------------------------
# Full batch run (--all)
# ---------------------------------------------------------------------------

def run_batch() -> None:
    """Geocode all eligible leads in Supabase.

    Eligible: verified_enriched=false AND property_address IS NOT NULL.
    Processes in pages of 500. A fresh Supabase client is created per page
    to prevent httpx HTTP/2 connection errors on long-running batches.
    """
    page_size = 500
    total = 0
    succeeded = 0

    while True:
        # Fresh client each page — avoids httpx RemoteProtocolError on
        # long-running batches where the HTTP/2 connection goes stale.
        client = get_client()

        # Always query from offset=0: as each page is processed and marked
        # verified_enriched=True, those rows drop out of the filter and the
        # next page naturally surfaces without advancing the offset.
        leads = (
            client.table("raw_leads")
            .select("id, property_address, county, state, owner_name, verification_notes")
            .eq("verified_enriched", False)
            .not_.is_("property_address", "null")
            .or_("verification_notes.is.null,verification_notes.not.like.*geocoding_attempted=true*")
            .limit(page_size)
            .execute()
            .data or []
        )
        if not leads:
            break

        log.info(f"Batch geocoding — page={len(leads)} leads (total so far: {total})")

        page_succeeded = 0
        for lead in leads:
            updates = enrich_lead(lead)
            if not updates.get("verified_enriched"):
                # Mark geocoding as attempted so this record is never re-queued.
                # Keeps verified_enriched=False (correct semantically) but adds a
                # note that prevents it from surfacing in the batch filter again.
                updates["verification_notes"] = (
                    (lead.get("verification_notes") or "") +
                    " | geocoding_attempted=true"
                ).strip(" | ")
            try:
                update_row("raw_leads", lead["id"], updates)
                if updates.get("verified_enriched"):
                    succeeded += 1
                    page_succeeded += 1
            except Exception as e:
                log.error(f"DB write failed for lead {lead['id']}: {e}")
            total += 1
            time.sleep(REQUEST_DELAY_S)

        # If every lead in the page failed geocoding, nothing will drop out of
        # the filter next iteration — break to avoid an infinite loop.
        if page_succeeded == 0:
            log.warning(f"Full page of {len(leads)} leads could not be geocoded — stopping batch")
            break

    log.info(f"Batch complete — {succeeded}/{total} leads geocoded successfully")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Google Maps address verification + Street View")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sample", type=int, metavar="N",
                       help="Dry-run geocoding on N leads — prints results, no DB writes")
    group.add_argument("--all", action="store_true",
                       help="Geocode all eligible leads in Supabase")
    args = parser.parse_args()

    if args.sample:
        run_sample(args.sample)
    elif args.all:
        run_batch()
