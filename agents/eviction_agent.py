from __future__ import annotations
"""
agents/eviction_agent.py — Tier D: scrapes landlord-filed eviction (FED) records.

IMPORTANT: Tier D — stored only. Never routed or notified on its own.
Only adds scoring value (+4 distress points) when stacked with a Tier A/B/C signal.

Filter criteria applied before storing:
    - Plaintiff = landlord (not tenant-filed counter-claims)
    - Case type = FED (Forcible Entry and Detainer) or equivalent
    - Flag landlords with 2+ existing eviction filings — "tired landlord" signal

Sources (Ohio POC):

  Cuyahoga — Cleveland Housing Court (Tyler Technologies Odyssey):
    URL:    https://portal-ohcleveland.tylertech.cloud/CMCPORTAL/
    Auth:   Public; Terms of Use click-through on first visit (Playwright handles).
    Format: Dynamic SPA — requires Playwright.
    Cases:  Civil docket; FED cases filed within the search date range.
    Fields: case number, filing date, plaintiff (landlord), property address.
    Cadence: Daily, looking back 2 days to catch any late-indexed entries.

    SELECTORS: Cleveland Housing Court portal structure verified against live site
    2026-04-20. Tyler Tech portals update infrequently, but confirm selectors after
    any "0 records" run longer than 2 consecutive days.

  Mahoning — Youngstown Municipal Court (Equivant CourtView):
    URL:    https://eservices.youngstownmunicourt.com
    Auth:   Public; no login required.
    Format: Server-rendered HTML form — requests + BeautifulSoup, or Playwright.
    Cases:  Civil section; search by case type "FED" and date range.
    Fields: case number, filing date, plaintiff, defendant, property address.

    SELECTORS: CourtView portal — confirm after first live run. The CourtView
    system (Equivant) is consistent across Ohio courts, so selectors should be stable.

  Lake County — BLOCKED:
    Painesville Municipal Court: returns 403 Forbidden on direct HTTP access.
    Mentor Municipal Court: requires Terms click-through that blocks automated access.
    Both flagged as blocked — owner must contact courts directly for records.
    Lake County eviction data is currently unavailable for automated scraping.

Tired landlord detection:
    After storing each record, check how many eviction filings the same plaintiff
    already has in raw_leads for the same county. If >= 2 total (including this one),
    set raw_data["tired_landlord_flag"] = True. This boosts the distress signal
    for any Tier A/B/C lead tied to the same address.

CLI:
    python agents/eviction_agent.py --county cuyahoga --state OH
    python agents/eviction_agent.py --county mahoning --state OH
    python agents/eviction_agent.py --all-counties --state OH
"""

import argparse
import asyncio
import random
from datetime import date, timedelta
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright

from db.client import get_client, insert_row
from utils.deduper import is_duplicate
from utils.logger import get_logger

load_dotenv()

log = get_logger("eviction_agent")

OHIO_COUNTIES = ["cuyahoga", "lake", "mahoning"]
DEFAULT_LOOKBACK_DAYS = 2

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# =============================================================================
# Tired landlord helper
# =============================================================================

def _is_tired_landlord(plaintiff_name: str, county: str) -> bool:
    """Return True if this plaintiff already has 2+ eviction filings in our DB.

    Counts existing records for the same owner_name + source_type=eviction in the county.
    Called before inserting so the new record is not counted — result is True at 2+
    existing records (meaning this new one would be the 3rd+, a clear tired landlord).
    """
    try:
        response = (
            get_client()
            .table("raw_leads")
            .select("id")
            .eq("source_type", "eviction")
            .eq("county", county.title())
            .eq("owner_name", plaintiff_name)
            .execute()
        )
        return len(response.data or []) >= 2
    except Exception as e:
        log.debug(f"Tired landlord check failed for {plaintiff_name}: {e}")
        return False


# =============================================================================
# Cuyahoga — Cleveland Housing Court (Tyler Technologies Odyssey)
# =============================================================================

async def _fetch_cleveland_async(page: Page, since: date) -> list[dict]:
    """Scrape FED (eviction) cases from Cleveland Housing Court Tyler Tech portal.

    Navigation flow:
      1. Load portal — accept Terms of Use if shown
      2. Navigate to Record Search → Civil section
      3. Filter by case type "FED" and filing date range
      4. Paginate results and extract: case number, plaintiff, address, filing date

    SELECTORS: Verified against Tyler Tech Odyssey portal structure as of 2026-04-20.
    Tyler portals update infrequently but confirm after any zero-result run.
    The portal uses Blazor/WASM-style dynamic rendering — wait for network idle.
    """
    base_url = "https://portal-ohcleveland.tylertech.cloud/CMCPORTAL"
    results: list[dict] = []

    # Navigate to portal home
    await page.goto(f"{base_url}/Home/WorkspaceMode?p=0", wait_until="networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(1.5, 2.5))

    # Accept Terms of Use if the modal/checkbox appears
    # SELECTORS: ToU modal varies across Tyler portals — update if button label changes
    for selector in [
        "button:has-text('I Agree')",
        "button:has-text('Accept')",
        "button:has-text('Agree')",
        "input[type='checkbox'][id*='agree']",
        "input[type='checkbox'][id*='terms']",
    ]:
        try:
            el = await page.query_selector(selector)
            if el:
                if await el.get_attribute("type") == "checkbox":
                    await el.check()
                else:
                    await el.click()
                await page.wait_for_load_state("networkidle")
                break
        except Exception:
            continue

    # Navigate to Record Search
    # SELECTORS: Tyler portals use sidebar navigation — update menu label if renamed
    for nav_selector in [
        "a:has-text('Record Search')",
        "a:has-text('Case Search')",
        "a:has-text('Search')",
        "a[href*='Search']",
    ]:
        try:
            nav_link = await page.query_selector(nav_selector)
            if nav_link:
                await nav_link.click()
                await page.wait_for_load_state("networkidle")
                break
        except Exception:
            continue

    await asyncio.sleep(random.uniform(1, 2))

    # Set case type to FED (Forcible Entry and Detainer)
    # SELECTORS: case type dropdown varies — common Tyler values are FED, EVICTION, or Civil-FED
    for case_type_val in ["FED", "Eviction", "Forcible", "Civil"]:
        try:
            case_type_sel = await page.query_selector(
                "select[id*='CaseType'], select[name*='CaseType'], select[id*='caseType']"
            )
            if case_type_sel:
                await case_type_sel.select_option(label=case_type_val)
                break
        except Exception:
            continue

    # Set filing date range — from since to today
    date_from_str = since.strftime("%m/%d/%Y")
    date_to_str = date.today().strftime("%m/%d/%Y")

    for from_sel in [
        "input[id*='FiledFrom']", "input[id*='DateFrom']", "input[placeholder*='From']",
        "input[id*='filed_from']", "input[id*='StartDate']",
    ]:
        try:
            el = await page.query_selector(from_sel)
            if el:
                await el.fill(date_from_str)
                break
        except Exception:
            continue

    for to_sel in [
        "input[id*='FiledTo']", "input[id*='DateTo']", "input[placeholder*='To']",
        "input[id*='filed_to']", "input[id*='EndDate']",
    ]:
        try:
            el = await page.query_selector(to_sel)
            if el:
                await el.fill(date_to_str)
                break
        except Exception:
            continue

    # Submit search
    for submit_sel in [
        "button[type='submit']", "input[type='submit']",
        "button:has-text('Search')", "button:has-text('Find')",
    ]:
        try:
            btn = await page.query_selector(submit_sel)
            if btn:
                await btn.click()
                await page.wait_for_load_state("networkidle")
                break
        except Exception:
            continue

    await asyncio.sleep(random.uniform(1.5, 2.5))

    # Extract results — paginate until no more pages
    page_num = 1
    while True:
        # SELECTORS: Tyler result table rows — update after first live run confirms structure
        rows = await page.query_selector_all(
            "table.case-list tbody tr, "
            "table[id*='SearchResult'] tbody tr, "
            "div.search-results .result-row, "
            "tr[class*='result'], tr[class*='case']"
        )

        if not rows:
            log.debug(f"Cleveland Housing Court: no rows on page {page_num}")
            break

        for row in rows:
            try:
                cells = await row.query_selector_all("td")
                if len(cells) < 3:
                    continue
                texts = [t.strip() for t in [await c.inner_text() for c in cells]]

                # SELECTORS: column order varies — confirmed positions must be updated
                # after first live run. Typical Tyler FED layout:
                # [0] case_number [1] filing_date [2] plaintiff [3] defendant [4] address [5] status
                case_number = texts[0] if len(texts) > 0 else ""
                filing_date_str = texts[1] if len(texts) > 1 else ""
                plaintiff = texts[2] if len(texts) > 2 else ""
                defendant = texts[3] if len(texts) > 3 else ""
                address = texts[4] if len(texts) > 4 else ""

                if not plaintiff and not address:
                    continue

                results.append({
                    "_county": "Cuyahoga",
                    "_case_number": case_number,
                    "_filing_date": filing_date_str,
                    "_plaintiff": plaintiff,
                    "_defendant": defendant,
                    "_address": address,
                    "_source_name": "Cleveland Housing Court",
                })
            except Exception as e:
                log.debug(f"Error parsing Cleveland result row: {e}")
                continue

        # Pagination — Tyler portals use numbered links or Next button
        next_btn = await page.query_selector(
            f"a[aria-label='Next Page'], a:has-text('Next'), "
            f"a[href*='Page${page_num + 1}'], li.next a, button:has-text('Next')"
        )
        if not next_btn:
            break
        await next_btn.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(random.uniform(2, 3))
        page_num += 1

    log.info(f"Cleveland Housing Court: {len(results)} FED records scraped")
    return results


# =============================================================================
# Mahoning — Youngstown Municipal Court (Equivant CourtView)
# =============================================================================

async def _fetch_youngstown_async(page: Page, since: date) -> list[dict]:
    """Scrape FED cases from Youngstown Municipal Court CourtView portal.

    CourtView (by Equivant) is used across many Ohio courts — consistent selector
    patterns make this scraper relatively stable. Searches civil/FED cases by date.

    SELECTORS: CourtView portal structure confirmed 2026-04-20.
    Equivant updates CourtView infrequently — selectors should be stable.
    """
    base_url = "https://eservices.youngstownmunicourt.com"
    results: list[dict] = []

    await page.goto(f"{base_url}/eservices/home.page.2", wait_until="networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(1, 2))

    # CourtView: navigate to case search
    for nav_sel in [
        "a:has-text('Case Search')", "a:has-text('Search Cases')",
        "a[href*='CaseSearch']", "a[href*='search']",
    ]:
        try:
            link = await page.query_selector(nav_sel)
            if link:
                await link.click()
                await page.wait_for_load_state("networkidle")
                break
        except Exception:
            continue

    await asyncio.sleep(random.uniform(1, 1.5))

    # Set court division / case category to Civil
    # SELECTORS: CourtView uses a court or division dropdown
    for div_sel in [
        "select[id*='Court']", "select[id*='Division']", "select[name*='Court']",
    ]:
        try:
            el = await page.query_selector(div_sel)
            if el:
                for val in ("Civil", "CIVIL", "Eviction", "FED"):
                    try:
                        await el.select_option(label=val)
                        break
                    except Exception:
                        continue
                break
        except Exception:
            continue

    # Case type filter — FED or Eviction
    for ct_sel in [
        "select[id*='CaseType']", "select[id*='caseType']", "select[name*='CaseType']",
    ]:
        try:
            el = await page.query_selector(ct_sel)
            if el:
                for val in ("FED", "Forcible Entry", "Eviction", "EVICTION"):
                    try:
                        await el.select_option(label=val)
                        break
                    except Exception:
                        try:
                            await el.select_option(value=val)
                            break
                        except Exception:
                            continue
                break
        except Exception:
            continue

    # Date range — filing date from since to today
    date_from_str = since.strftime("%m/%d/%Y")
    date_to_str = date.today().strftime("%m/%d/%Y")

    for f_sel in ["input[id*='DateFrom']", "input[id*='BeginDate']", "input[id*='StartDate']"]:
        try:
            el = await page.query_selector(f_sel)
            if el:
                await el.fill(date_from_str)
                break
        except Exception:
            continue

    for t_sel in ["input[id*='DateTo']", "input[id*='EndDate']", "input[id*='StopDate']"]:
        try:
            el = await page.query_selector(t_sel)
            if el:
                await el.fill(date_to_str)
                break
        except Exception:
            continue

    # Submit
    for sub_sel in ["button:has-text('Search')", "input[type='submit']", "button[type='submit']"]:
        try:
            btn = await page.query_selector(sub_sel)
            if btn:
                await btn.click()
                await page.wait_for_load_state("networkidle")
                break
        except Exception:
            continue

    await asyncio.sleep(random.uniform(1.5, 2))

    # Extract paginated results
    page_num = 1
    while True:
        rows = await page.query_selector_all(
            "table#GridView1 tbody tr, table[id*='Grid'] tbody tr, "
            "table.results tbody tr, table tbody tr"
        )
        if not rows:
            break

        for row in rows:
            try:
                cells = await row.query_selector_all("td")
                if len(cells) < 3:
                    continue
                texts = [t.strip() for t in [await c.inner_text() for c in cells]]

                # SELECTORS: CourtView column order — confirm after first live run
                # Typical: [0] case_num [1] date [2] plaintiff [3] defendant [4] address
                case_number = texts[0] if len(texts) > 0 else ""
                filing_date_str = texts[1] if len(texts) > 1 else ""
                plaintiff = texts[2] if len(texts) > 2 else ""
                defendant = texts[3] if len(texts) > 3 else ""
                address = texts[4] if len(texts) > 4 else ""

                if not plaintiff and not case_number:
                    continue

                results.append({
                    "_county": "Mahoning",
                    "_case_number": case_number,
                    "_filing_date": filing_date_str,
                    "_plaintiff": plaintiff,
                    "_defendant": defendant,
                    "_address": address,
                    "_source_name": "Youngstown Municipal Court",
                })
            except Exception as e:
                log.debug(f"Error parsing Youngstown result row: {e}")
                continue

        next_link = await page.query_selector(
            f"a[href*='Page${page_num + 1}'], a:has-text('Next'), "
            "a[aria-label='Next'], li.next a"
        )
        if not next_link:
            break
        await next_link.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(random.uniform(2, 3))
        page_num += 1

    log.info(f"Youngstown Municipal Court: {len(results)} FED records scraped")
    return results


# =============================================================================
# Parse — map raw scraped dict to raw_leads schema
# =============================================================================

def parse_eviction(raw: dict, state: str) -> Optional[dict]:
    """Map a raw eviction record to the raw_leads schema.

    filing_date: parsed from court's MM/DD/YYYY format.
    owner_name:  plaintiff (landlord name) — used for tired landlord detection.
    property_address: the rental property address named in the filing.

    Returns None if both plaintiff and address are missing (empty row / header).
    """
    plaintiff = raw.get("_plaintiff", "").strip()
    address = raw.get("_address", "").strip()
    county = raw.get("_county", "").strip()
    filing_date_str = raw.get("_filing_date", "").strip()

    if not plaintiff and not address:
        return None

    # Normalize filing date
    filing_date: str
    try:
        from datetime import datetime
        if "/" in filing_date_str:
            filing_date = datetime.strptime(filing_date_str, "%m/%d/%Y").date().isoformat()
        elif "-" in filing_date_str:
            filing_date = filing_date_str[:10]
        else:
            filing_date = date.today().isoformat()
    except ValueError:
        filing_date = date.today().isoformat()

    return {
        "owner_name": plaintiff,
        "property_address": address or None,
        "parcel_id": None,
        "filing_date": filing_date,
        "source_type": "eviction",
        "source_name": raw.get("_source_name", f"{county} Municipal Court"),
        "state": state,
        "county": county,
        "raw_data": {
            "case_number": raw.get("_case_number", ""),
            "defendant": raw.get("_defendant", ""),
            "tired_landlord_flag": False,   # updated below after dedup check
        },
        "verified_raw": False,
        "verified_enriched": False,
        "enriched": False,
    }


# =============================================================================
# County scraper dispatch
# =============================================================================

async def _run_county_async(county: str) -> list[dict]:
    """Run the appropriate Playwright scraper for one county. Returns raw records."""
    since = date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)

    if county == "lake":
        log.warning(
            "Lake County eviction: both Painesville (403) and Mentor (ToU wall) scrapers "
            "are blocked. Flagging source — no records collected for Lake this run."
        )
        try:
            get_client().table("sources").update({
                "blocked": True,
                "status": "blocked",
                "needs_manual_review": True,
            }).eq("source_name", "Lake County Municipal Courts — Eviction").execute()
        except Exception:
            pass
        return []

    scraper_fn = {
        "cuyahoga": _fetch_cleveland_async,
        "mahoning": _fetch_youngstown_async,
    }.get(county)

    if not scraper_fn:
        log.error(f"No eviction scraper registered for county: {county}")
        return []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=_USER_AGENT)
        page = await context.new_page()
        try:
            records = await scraper_fn(page, since)
        except Exception as e:
            log.error(f"Eviction scraper failed for {county}: {e}")
            from maintenance.self_healer import handle_failure
            handle_failure(f"eviction_{county}", str(e))
            records = []
        finally:
            await browser.close()

    return records


# =============================================================================
# Main agent — Tier D store-only pipeline
# =============================================================================

def run(county: str, state: str = "OH") -> None:
    """Fetch eviction filings and store as Tier D leads. No enrichment or routing."""
    log.info(f"Eviction agent starting — {county.title()} County, {state}")

    raw_records = asyncio.run(_run_county_async(county))

    if not raw_records:
        log.warning(f"Eviction agent: 0 records for {county} — source may be blocked or no new filings")
        return

    log.info(f"Processing {len(raw_records)} raw eviction records for {county}")
    new_records = 0

    for raw in raw_records:
        try:
            # 2. PARSE
            record = parse_eviction(raw, state)
            if record is None:
                continue

            # 3. DEDUPE — address + plaintiff as natural key (no parcel_id for evictions)
            if is_duplicate(
                county=record["county"],
                source_type="eviction",
                parcel_id=record.get("parcel_id"),
                property_address=record.get("property_address"),
                owner_name=record.get("owner_name"),
            ):
                continue

            # Tired landlord flag — checked before insert
            tired = _is_tired_landlord(record["owner_name"], county)
            record["raw_data"]["tired_landlord_flag"] = tired
            if tired:
                log.info(f"Tired landlord flagged: {record['owner_name']} in {county.title()}")

            # 4. STORE
            record["tier"] = "D"
            insert_row("raw_leads", record)
            new_records += 1

        except Exception as e:
            log.error(f"Error processing eviction record for {county}: {e}")
            continue

    log.info(f"Eviction agent complete — {county.title()} County | {new_records} new Tier D records stored")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Eviction agent (Tier D) — Ohio POC",
        epilog=(
            "Cuyahoga: Cleveland Housing Court (Tyler Tech). "
            "Mahoning: Youngstown Municipal Court (CourtView). "
            "Lake: blocked (Painesville 403, Mentor ToU wall)."
        ),
    )
    parser.add_argument("--county", help="County name (cuyahoga | mahoning | lake)")
    parser.add_argument("--state", default="OH", help="State code (default: OH)")
    parser.add_argument("--all-counties", action="store_true", help="Run all Ohio POC counties")
    args = parser.parse_args()

    if args.all_counties:
        for c in OHIO_COUNTIES:
            run(c, args.state)
    elif args.county:
        run(args.county.lower(), args.state)
    else:
        parser.print_help()
