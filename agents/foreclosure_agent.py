from __future__ import annotations
"""
agents/foreclosure_agent.py — Scrapes foreclosure filings from Cuyahoga County Common Pleas Court.

Source:  Cuyahoga County Clerk of Courts — Foreclosure Search (cpdocket.cp.cuyahogacounty.gov)
Fields:  Case Defendant (owner/borrower), Parcel Address, City, Zip, Case Number, Parcel ID,
         Case Status, Filing Date
Cadence: Every 4 hours between 7 AM–7 PM EST — new filings trigger immediate SMS to owner.
Notes:   TOS acceptance required on every fresh Playwright session.
         SSL cert issue on cpdocket — Playwright launched with ignore_https_errors=True.
         Case number (CV-YY-XXXXXX) used as parcel_id for dedup uniqueness constraint.
         Actual Cuyahoga parcel number preserved in raw_data for enrichment.

To add a new county: implement a _fetch_<county>() async function and register it in
COUNTY_SCRAPERS. The 9-step pipeline is shared.

CLI:
    python agents/foreclosure_agent.py --county cuyahoga --state OH
    python agents/foreclosure_agent.py --county cuyahoga --state OH --days 14
"""

import argparse
import asyncio
import re
from datetime import date, timedelta

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from db.client import get_client, insert_row, update_row
from enrichment.waterfall import enrich_lead
from routing.notify import send_sms
from routing.va_router import route_lead


from scoring.score import score_lead
from utils.deduper import is_duplicate
from utils.logger import get_logger
from verification.verify_leads import verify_raw_record

load_dotenv()

log = get_logger("foreclosure_agent")

DEFAULT_LOOKBACK_DAYS = 1  # 4-hour cadence; 1-day window catches all filings since last run

# =============================================================================
# Cuyahoga County Common Pleas Court — Foreclosure Docket
#
# Confirmed live and public 2026-04-18. No login required; TOS gate only.
# SSL certificate issue on cpdocket.cp.cuyahogacounty.gov — use ignore_https_errors.
# Dedicated FORECLOSURE SEARCH mode (radio value "forcl") with date-range filter.
# Results load at ForeclosureSearchResults.aspx after ASP.NET postback.
# ~80–100 new foreclosure filings per week in Cuyahoga County.
# =============================================================================

_CPDOCKET_URL = "https://cpdocket.cp.cuyahogacounty.gov/"


async def _fetch_cuyahoga(since: date) -> list[dict]:
    """Scrape foreclosure filings from the Cuyahoga County Common Pleas docket.

    Accepts TOS, selects FORECLOSURE SEARCH, fills date range via JavaScript
    (MaskedEditExtender blocks direct .fill()), submits, and parses the result
    gridview. Returns a list of raw row dicts.
    """
    results: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # ignore_https_errors required — cpdocket.cp.cuyahogacounty.gov has SSL cert issues
        ctx = await browser.new_context(ignore_https_errors=True)
        page = await ctx.new_page()

        try:
            await page.goto(_CPDOCKET_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            # Accept Terms of Service — shown on every new session
            tos_btn = page.locator("#SheetContentPlaceHolder_btnYes")
            if await tos_btn.count() > 0:
                await tos_btn.click()
                await page.wait_for_load_state("networkidle", timeout=15000)

            # Select FORECLOSURE SEARCH radio — triggers ASP.NET postback to reveal form
            await page.click("#SheetContentPlaceHolder_rbCivilForeclosure")
            await page.wait_for_timeout(3000)
            await page.wait_for_load_state("networkidle", timeout=10000)

            # Fill date range via JS — direct .fill() fails with MaskedEditExtender
            from_str = since.strftime("%m/%d/%Y")
            to_str = date.today().strftime("%m/%d/%Y")
            await page.evaluate(f"""
                document.getElementById('SheetContentPlaceHolder_foreclosureSearch_txtFromDate').value = '{from_str}';
                document.getElementById('SheetContentPlaceHolder_foreclosureSearch_txtToDate').value = '{to_str}';
            """)

            # Submit search — navigates to ForeclosureSearchResults.aspx on success
            await page.click(
                "input[name='ctl00$SheetContentPlaceHolder$foreclosureSearch$btnSubmit']"
            )
            await page.wait_for_load_state("networkidle", timeout=20000)
            await page.wait_for_timeout(2000)

            if "ForeclosureSearchResults.aspx" not in page.url:
                log.warning(f"Unexpected URL after search: {page.url} — no results or form error")
                return results

            html = await page.content()
            results = _parse_gridview(html)
            log.debug(f"Cuyahoga: parsed {len(results)} rows from gridview (since {since})")

        except Exception as e:
            log.error(f"Cuyahoga fetch failed: {e}")
        finally:
            await browser.close()

    log.info(f"Cuyahoga: fetched {len(results)} foreclosure records since {since}")
    return results


def _parse_gridview(html: str) -> list[dict]:
    """Extract rows from the ForeclosureSearchResults ASP.NET GridView.

    Column order: Case Defendant | Parcel Address | City | Zip |
                  Case Number | Parcel | Status | Filed
    """
    rows: list[dict] = []

    # Gridview control ID confirmed 2026-04-18
    gv_match = re.search(
        r'id="SheetContentPlaceHolder_ctl00_gvForeclosureResults"', html
    )
    if not gv_match:
        log.warning("Foreclosure gridview not found — page structure may have changed")
        return rows

    table_start = gv_match.start()
    table_end = html.find("</table>", table_start)
    table_html = html[table_start : table_end + 8]

    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL | re.IGNORECASE):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.DOTALL | re.IGNORECASE)
        if len(cells) < 8:
            continue

        cleaned = [
            re.sub(r"<[^>]+>", "", c).strip().replace("&nbsp;", "").strip()
            for c in cells
        ]

        # Skip header row (th cells with column labels)
        if cleaned[4].upper() == "CASE NUMBER":
            continue

        defendant, address, city, zipcode, case_num, parcel, status, filed = cleaned[:8]

        # Skip malformed rows (no valid case number)
        if not re.match(r"CV-\d{2}-\d+", case_num):
            continue

        rows.append({
            "defendant": defendant,
            "address": address,
            "city": city,
            "zip": zipcode,
            "case_number": case_num,
            "parcel_number": parcel,  # actual Cuyahoga parcel number — used by enrichment
            "status": status,
            "filed": filed,
        })

    return rows


def parse_foreclosure(raw: dict, county: str, state: str) -> dict:
    """Map a raw gridview row to the raw_leads table schema."""
    # Build full property address from address components
    parts = [raw.get("address", ""), raw.get("city", ""), state, raw.get("zip", "")]
    property_address = ", ".join(p for p in parts if p) or None

    # Use case number as parcel_id to satisfy (parcel_id, county, source_type) uniqueness.
    # A parcel can have multiple foreclosure cases; case number is the true idempotency key.
    case_number = raw.get("case_number", "").strip() or None

    return {
        "owner_name": raw.get("defendant", "").strip(),
        "property_address": property_address,
        "parcel_id": case_number,
        "filing_date": raw.get("filed"),
        "raw_data": raw,
        "source_type": "foreclosure",
        "source_name": "Cuyahoga County Common Pleas Court (Foreclosure Docket)",
        "state": state,
        "county": county.title(),
        "verified_raw": False,
        "verified_enriched": False,
        "enriched": False,
    }


# County scraper registry — add new counties here as scrapers are implemented
COUNTY_SCRAPERS = {
    "cuyahoga": _fetch_cuyahoga,
}


# =============================================================================
# Main agent — full 9-step pipeline
# =============================================================================

async def _run_async(county: str, state: str, lookback_days: int) -> None:
    """Async inner loop — runs the full 9-step pipeline for one county."""
    scraper = COUNTY_SCRAPERS.get(county.lower())
    if not scraper:
        log.error(f"No scraper registered for county: {county}")
        return

    since = date.today() - timedelta(days=lookback_days)
    log.info(
        f"Foreclosure agent starting — {county.title()} County, {state} (since {since})"
    )

    # 1. FETCH
    try:
        raw_records = await scraper(since)
    except Exception as e:
        log.error(f"FETCH failed for {county}: {e}")
        from maintenance.self_healer import handle_failure
        handle_failure(f"foreclosure_{county}", str(e))
        return

    if not raw_records:
        log.warning(
            f"No foreclosure records returned for {county} — "
            "check source URL or expand --days window"
        )
        from maintenance.self_healer import handle_failure
        handle_failure(f"foreclosure_{county}", "Zero records returned")
        return

    log.info(f"Processing {len(raw_records)} raw records for {county}")
    new_records = 0
    tier_a_count = 0
    tier_b_count = 0

    for raw in raw_records:
        try:
            case_num = raw.get("case_number", "")

            # 2. PARSE
            record = parse_foreclosure(raw, county, state)
            if not record.get("property_address"):
                log.debug(f"Skipping record with no address: case={case_num}")
                continue

            # 3. DEDUPE — case number is parcel_id; maps to (parcel_id, county, source_type) constraint
            if is_duplicate(
                county=county.title(),
                source_type="foreclosure",
                parcel_id=record.get("parcel_id"),
                property_address=record.get("property_address"),
                owner_name=record.get("owner_name"),
            ):
                log.debug(f"Duplicate case={case_num} — skipping")
                continue

            # 4. STORE
            stored = insert_row("raw_leads", record)
            lead_id = stored.get("id")
            if not lead_id:
                log.error(f"Insert returned no ID for case={case_num}")
                continue
            new_records += 1

            # 5. VERIFY (Gate 1 — post-scrape record validation)
            passed_gate1 = verify_raw_record(lead_id)
            if not passed_gate1:
                log.debug(f"Lead {lead_id} failed Gate 1 — skipping enrichment")
                continue

            # 6. ENRICH (waterfall: county auditor → Skip Sherpa → Skip Matrix flag)
            enrich_lead(lead_id)

            # 7. VERIFY (Gate 2 — post-enrichment field validation)
            refreshed = (
                get_client()
                .table("raw_leads")
                .select("*")
                .eq("id", lead_id)
                .single()
                .execute()
                .data
            )
            if not refreshed or not refreshed.get("verified_enriched"):
                log.debug(f"Lead {lead_id} failed Gate 2 — skipping scoring")
                continue

            # 8. SCORE
            result = score_lead(refreshed)
            update_row("raw_leads", lead_id, {
                **result,
                "scored_at": "now()",
            })
            log.info(
                f"Scored: {record.get('property_address')} | "
                f"distress={result['distress_score']} deal={result['deal_score']} "
                f"total={result['score']} tier={result['tier']}"
            )

            # 9. ROUTE
            if result["tier"] == "A":
                tier_a_count += 1
                route_lead(lead_id, result["tier"])
            elif result["tier"] == "B":
                tier_b_count += 1
                route_lead(lead_id, result["tier"])

        except Exception as e:
            log.error(f"Error processing case={raw.get('case_number', '?')}: {e}")
            continue

    if tier_a_count > 0:
        send_sms(
            f"[Lead Intel] {new_records} new leads added — "
            f"{tier_a_count} Tier A, {tier_b_count} Tier B. Check your sheet."
        )

    log.info(
        f"Foreclosure agent complete — {county.title()} County | "
        f"{new_records} new records stored"
    )


def run(
    county: str,
    state: str = "OH",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> None:
    """Run the foreclosure agent for one county (synchronous entry point)."""
    asyncio.run(_run_async(county, state, lookback_days))


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Foreclosure agent — Ohio POC")
    parser.add_argument(
        "--county",
        default="cuyahoga",
        help="County name (default: cuyahoga)",
    )
    parser.add_argument(
        "--state",
        default="OH",
        help="State code (default: OH)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"Lookback days (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    args = parser.parse_args()
    run(args.county.lower(), args.state, args.days)
