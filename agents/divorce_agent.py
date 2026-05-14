from __future__ import annotations
"""
agents/divorce_agent.py — Tier D: scrapes divorce filing metadata from county courts.

IMPORTANT: Tier D — stored only. Never routed or notified on its own.
Only adds scoring value (+4 distress points) when stacked with a Tier A/B/C signal.

What "metadata only" means:
    Ohio courts deliberately restrict domestic relations records online. Full documents
    (petition, financial disclosures) are never available without in-person request.
    This agent collects case-level metadata only: case number, filing date, and party
    names. No addresses, no property info. Cross-referencing party names against county
    auditor records (enrichment/public_sources.py) is the only way to confirm that a
    divorcing party owns real estate in our target counties.

Sources (Ohio POC):

  Cuyahoga — BLOCKED (stub):
    cpdocket.cp.cuyahogacounty.gov explicitly excludes domestic relations cases from
    online access. Domestic violence and divorce/dissolution files are available only
    in-person at 1200 Ontario Street, Cleveland, OH.
    Status: flagged as blocked. Owner must contact Cuyahoga Clerk of Courts directly.

  Lake County — ShowCaseWeb (Equivant, Vue.js SPA):
    URL:    https://phoenix.lakecountyohio.gov/eservices/home.page.2
    Also:   https://courtrecords.lakecountyclerk.org
    Auth:   Public. No login required.
    Format: Vue.js SPA (ShowCaseWeb v4.2.21 by Equivant). Requires Playwright.
    Fields available: case number, filing date, case status, party names (petitioner +
                      respondent). Document images NOT available for domestic relations.
    Filter: Division = "Domestic Relations" or "DR"

    SELECTORS: Vue.js SPA — selectors interact with rendered DOM, not page source.
    Confirmed: public case search accessible as of 2026-04-20.
    Update selectors if the portal shows 0 results for 2+ consecutive days.

  Mahoning County — ecourts portal (Equivant, similar ShowCaseWeb):
    URL:    https://ecourts.mahoningcountyoh.gov/eservices/home.page.2
    Auth:   Public. No login required.
    Format: Same Equivant platform as Lake — selectors should be consistent.
    Filter: Division = "Domestic Relations" or "DR"

    SELECTORS: Confirmed publicly accessible as of 2026-04-20. JS-gated portal —
    Playwright required. Mahoning has no Cloudflare block on this specific portal
    (unlike the auditor site). Verify after first live run.

Property cross-reference (enrichment responsibility):
    Party names from divorce filings are matched against county auditor property records
    in enrichment/public_sources.py. If a match is found, the property address and
    parcel ID are populated — upgrading this Tier D record to a stacked signal candidate.
    Records where no property match is found stay as Tier D with no address.

CLI:
    python agents/divorce_agent.py --county cuyahoga --state OH
    python agents/divorce_agent.py --county lake --state OH
    python agents/divorce_agent.py --county mahoning --state OH
    python agents/divorce_agent.py --all-counties --state OH
"""

import argparse
import asyncio
import random
from datetime import date, timedelta
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright

from db.client import insert_row
from utils.deduper import is_duplicate
from utils.logger import get_logger

load_dotenv()

log = get_logger("divorce_agent")

OHIO_COUNTIES = ["cuyahoga", "lake", "mahoning"]
DEFAULT_LOOKBACK_DAYS = 3

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# =============================================================================
# Lake County — ShowCaseWeb (Equivant Vue.js SPA)
# =============================================================================

async def _fetch_lake_async(page: Page, since: date) -> list[dict]:
    """Scrape domestic relations case metadata from Lake County ShowCaseWeb portal.

    Selects the Domestic Relations division from the court search interface,
    applies a filing date filter, and extracts available case metadata.
    Document images are not available for DR cases — metadata only.

    SELECTORS: ShowCaseWeb (Equivant) Vue.js SPA. DOM is rendered client-side.
    Confirmed publicly accessible 2026-04-20. Update selectors after first live run
    confirms exact control IDs and column layout.
    """
    url = "https://phoenix.lakecountyohio.gov/eservices/home.page.2"
    results: list[dict] = []

    await page.goto(url, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(2, 3))

    # ShowCaseWeb: navigate to case search tab
    # SELECTORS: tab label may be "Case Search", "Search", or "Public Access"
    for tab_sel in [
        "a:has-text('Case Search')", "a:has-text('Search')",
        "button:has-text('Case Search')", "li:has-text('Case Search') a",
        "a[href*='search']",
    ]:
        try:
            tab = await page.query_selector(tab_sel)
            if tab:
                await tab.click()
                await page.wait_for_load_state("networkidle")
                break
        except Exception:
            continue

    await asyncio.sleep(random.uniform(1, 2))

    # Select Division = "Domestic Relations" or "DR"
    # SELECTORS: ShowCaseWeb typically uses a Court or Division dropdown
    for div_sel in [
        "select[id*='Division']", "select[id*='Court']", "select[name*='Division']",
        "select[id*='division']",
    ]:
        try:
            el = await page.query_selector(div_sel)
            if el:
                for val in ("Domestic Relations", "DR", "Domestic", "DOMESTIC"):
                    try:
                        await el.select_option(label=val)
                        await asyncio.sleep(0.5)
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

    # Set filing date range
    date_from_str = since.strftime("%m/%d/%Y")
    date_to_str = date.today().strftime("%m/%d/%Y")

    for f_sel in [
        "input[id*='DateFrom']", "input[id*='BeginDate']", "input[placeholder*='From']",
        "input[id*='filed_from']", "input[id*='StartDate']",
    ]:
        try:
            el = await page.query_selector(f_sel)
            if el:
                await el.fill(date_from_str)
                break
        except Exception:
            continue

    for t_sel in [
        "input[id*='DateTo']", "input[id*='EndDate']", "input[placeholder*='To']",
        "input[id*='filed_to']",
    ]:
        try:
            el = await page.query_selector(t_sel)
            if el:
                await el.fill(date_to_str)
                break
        except Exception:
            continue

    # Submit search
    for sub_sel in [
        "button:has-text('Search')", "button[type='submit']", "input[type='submit']",
    ]:
        try:
            btn = await page.query_selector(sub_sel)
            if btn:
                await btn.click()
                await page.wait_for_load_state("networkidle")
                break
        except Exception:
            continue

    await asyncio.sleep(random.uniform(2, 3))

    # Extract paginated results
    page_num = 1
    while True:
        # SELECTORS: ShowCaseWeb renders results as a table or card list in Vue
        rows = await page.query_selector_all(
            "table.case-results tbody tr, "
            "table[id*='Result'] tbody tr, "
            "div.case-row, tr.case-row, "
            "tbody tr[class*='case']"
        )

        if not rows:
            log.debug(f"Lake County divorce: no rows on page {page_num}")
            break

        for row in rows:
            try:
                cells = await row.query_selector_all("td")
                if len(cells) < 2:
                    continue
                texts = [t.strip() for t in [await c.inner_text() for c in cells]]

                # SELECTORS: column order — update after live run confirms ShowCaseWeb layout
                # Typical ShowCaseWeb DR layout:
                # [0] case_number [1] filing_date [2] party_1 (petitioner) [3] party_2 (respondent) [4] status
                case_number = texts[0] if len(texts) > 0 else ""
                filing_date_str = texts[1] if len(texts) > 1 else ""
                petitioner = texts[2] if len(texts) > 2 else ""
                respondent = texts[3] if len(texts) > 3 else ""
                status = texts[4] if len(texts) > 4 else ""

                if not case_number and not petitioner:
                    continue

                results.append({
                    "_county": "Lake",
                    "_case_number": case_number,
                    "_filing_date": filing_date_str,
                    "_petitioner": petitioner,
                    "_respondent": respondent,
                    "_status": status,
                    "_source_name": "Lake County Common Pleas — Domestic Relations",
                })
            except Exception as e:
                log.debug(f"Error parsing Lake County divorce row: {e}")
                continue

        # ShowCaseWeb pagination — Vue router or link-based
        next_btn = await page.query_selector(
            f"a[aria-label='Next Page'], a:has-text('Next '), "
            f"button:has-text('Next'), li.next a, a[href*='page={page_num + 1}']"
        )
        if not next_btn:
            break
        await next_btn.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(random.uniform(2, 3))
        page_num += 1

    log.info(f"Lake County divorce: {len(results)} DR case records scraped")
    return results


# =============================================================================
# Mahoning County — ecourts portal (same Equivant platform as Lake)
# =============================================================================

async def _fetch_mahoning_async(page: Page, since: date) -> list[dict]:
    """Scrape domestic relations case metadata from Mahoning County ecourts portal.

    Uses the same Equivant ShowCaseWeb platform as Lake County — selectors are
    expected to be consistent. No Cloudflare block on this portal (unlike the
    Mahoning auditor/treasurer sites). Confirmed publicly accessible 2026-04-20.

    SELECTORS: Mirror of Lake County _fetch_lake_async. If Lake selectors are updated
    after live testing, apply the same changes here.
    """
    url = "https://ecourts.mahoningcountyoh.gov/eservices/home.page.2"
    results: list[dict] = []

    await page.goto(url, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(2, 3))

    # Case search tab
    for tab_sel in [
        "a:has-text('Case Search')", "a:has-text('Search')",
        "button:has-text('Case Search')", "a[href*='search']",
    ]:
        try:
            tab = await page.query_selector(tab_sel)
            if tab:
                await tab.click()
                await page.wait_for_load_state("networkidle")
                break
        except Exception:
            continue

    await asyncio.sleep(random.uniform(1, 2))

    # Division = Domestic Relations
    for div_sel in [
        "select[id*='Division']", "select[id*='Court']", "select[name*='Division']",
    ]:
        try:
            el = await page.query_selector(div_sel)
            if el:
                for val in ("Domestic Relations", "DR", "Domestic", "DOMESTIC"):
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

    # Date range
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

    for t_sel in ["input[id*='DateTo']", "input[id*='EndDate']"]:
        try:
            el = await page.query_selector(t_sel)
            if el:
                await el.fill(date_to_str)
                break
        except Exception:
            continue

    for sub_sel in ["button:has-text('Search')", "button[type='submit']", "input[type='submit']"]:
        try:
            btn = await page.query_selector(sub_sel)
            if btn:
                await btn.click()
                await page.wait_for_load_state("networkidle")
                break
        except Exception:
            continue

    await asyncio.sleep(random.uniform(2, 3))

    page_num = 1
    while True:
        rows = await page.query_selector_all(
            "table.case-results tbody tr, table[id*='Result'] tbody tr, "
            "div.case-row, tr.case-row, tbody tr[class*='case']"
        )
        if not rows:
            break

        for row in rows:
            try:
                cells = await row.query_selector_all("td")
                if len(cells) < 2:
                    continue
                texts = [t.strip() for t in [await c.inner_text() for c in cells]]

                case_number = texts[0] if len(texts) > 0 else ""
                filing_date_str = texts[1] if len(texts) > 1 else ""
                petitioner = texts[2] if len(texts) > 2 else ""
                respondent = texts[3] if len(texts) > 3 else ""
                status = texts[4] if len(texts) > 4 else ""

                if not case_number and not petitioner:
                    continue

                results.append({
                    "_county": "Mahoning",
                    "_case_number": case_number,
                    "_filing_date": filing_date_str,
                    "_petitioner": petitioner,
                    "_respondent": respondent,
                    "_status": status,
                    "_source_name": "Mahoning County Common Pleas — Domestic Relations",
                })
            except Exception as e:
                log.debug(f"Error parsing Mahoning divorce row: {e}")
                continue

        next_btn = await page.query_selector(
            f"a[aria-label='Next Page'], a:has-text('Next '), "
            f"button:has-text('Next'), li.next a"
        )
        if not next_btn:
            break
        await next_btn.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(random.uniform(2, 3))
        page_num += 1

    log.info(f"Mahoning County divorce: {len(results)} DR case records scraped")
    return results


# =============================================================================
# Cuyahoga — BLOCKED stub
# =============================================================================

class CuyahogaDRBlockedError(RuntimeError):
    """Raised when Cuyahoga domestic relations records are requested (online access blocked)."""


async def _fetch_cuyahoga_async(_page: Page, _since: date) -> list[dict]:
    """Cuyahoga County domestic relations — blocked (confirmed 2026-04-20).

    cpdocket.cp.cuyahogacounty.gov explicitly excludes domestic relations and
    domestic violence cases from online access. Records are available only in-person
    at the Cuyahoga County Clerk of Courts, 1200 Ontario Street, Cleveland, OH 44113.
    Phone: (216) 443-7950.
    """
    raise CuyahogaDRBlockedError(
        "Cuyahoga County domestic relations records are not available online (confirmed 2026-04-20). "
        "In-person request required: Cuyahoga County Clerk of Courts, "
        "1200 Ontario Street, Cleveland, OH 44113. Phone: (216) 443-7950."
    )


# County scraper registry
_COUNTY_SCRAPERS = {
    "lake": _fetch_lake_async,
    "mahoning": _fetch_mahoning_async,
    "cuyahoga": _fetch_cuyahoga_async,
}


# =============================================================================
# Parse — map raw scraped dict to raw_leads schema
# =============================================================================

def parse_divorce(raw: dict, state: str) -> Optional[dict]:
    """Map a raw divorce case record to the raw_leads schema.

    filing_date: parsed from court's date field.
    owner_name:  petitioner name — used for county auditor cross-reference during enrichment.
    property_address: None — not available from court metadata.
    parcel_id: None — no parcel info available without property cross-reference.

    Both party names are stored in raw_data so enrichment can check either name
    against county auditor records (the respondent may be the property owner).

    Returns None if both petitioner and case_number are empty.
    """
    petitioner = raw.get("_petitioner", "").strip()
    respondent = raw.get("_respondent", "").strip()
    case_number = raw.get("_case_number", "").strip()
    county = raw.get("_county", "").strip()
    filing_date_str = raw.get("_filing_date", "").strip()

    if not petitioner and not case_number:
        return None

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
        "owner_name": petitioner,
        "property_address": None,
        "parcel_id": None,
        "filing_date": filing_date,
        "source_type": "divorce",
        "source_name": raw.get("_source_name", f"{county} County Common Pleas — Domestic Relations"),
        "state": state,
        "county": county,
        "raw_data": {
            "case_number": case_number,
            "petitioner": petitioner,
            "respondent": respondent,
            "case_status": raw.get("_status", ""),
            "address_unknown": True,  # enrichment resolves via auditor name lookup
        },
        "verified_raw": False,
        "verified_enriched": False,
        "enriched": False,
    }


# =============================================================================
# Main agent — Tier D store-only pipeline
# =============================================================================

def run(county: str, state: str = "OH") -> None:
    """Fetch divorce filing metadata and store as Tier D leads. No enrichment or routing."""
    log.info(f"Divorce agent starting — {county.title()} County, {state}")

    scraper_fn = _COUNTY_SCRAPERS.get(county.lower())
    if not scraper_fn:
        log.error(f"No divorce scraper registered for county: {county}")
        return

    since = date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)

    raw_records: list[dict] = []

    async def _run_async() -> list[dict]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=_USER_AGENT)
            page = await context.new_page()
            try:
                return await scraper_fn(page, since)
            except CuyahogaDRBlockedError as e:
                log.warning(str(e))
                try:
                    from db.client import get_client
                    get_client().table("sources").update({
                        "blocked": True,
                        "status": "blocked",
                        "needs_manual_review": True,
                    }).eq("source_name", "Cuyahoga County Common Pleas — Domestic Relations").execute()
                except Exception:
                    pass
                return []
            except Exception as e:
                log.error(f"Divorce scraper failed for {county}: {e}")
                from maintenance.self_healer import handle_failure
                handle_failure(f"divorce_{county}", str(e))
                return []
            finally:
                await browser.close()

    raw_records = asyncio.run(_run_async())

    if not raw_records:
        log.warning(f"Divorce agent: 0 records for {county} — source may be blocked or no new filings")
        return

    log.info(f"Processing {len(raw_records)} raw divorce records for {county}")
    new_records = 0

    for raw in raw_records:
        try:
            # 2. PARSE
            record = parse_divorce(raw, state)
            if record is None:
                continue

            # 3. DEDUPE — case_number as synthetic key; fall back to petitioner + county
            dup_check = is_duplicate(
                county=record["county"],
                source_type="divorce",
                parcel_id=raw.get("_case_number") or None,  # case_number as dedup key
                owner_name=record.get("owner_name"),
            )
            if dup_check:
                continue

            # 4. STORE
            record["tier"] = "D"
            insert_row("raw_leads", record)
            new_records += 1

        except Exception as e:
            log.error(f"Error processing divorce record for {county}: {e}")
            continue

    log.info(f"Divorce agent complete — {county.title()} County | {new_records} new Tier D records stored")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Divorce agent (Tier D) — metadata only, Ohio POC",
        epilog=(
            "Lake + Mahoning: case metadata (case number, party names, filing date). "
            "Cuyahoga: blocked — court restricts domestic relations access online. "
            "No property addresses scraped — enrichment resolves via auditor name lookup."
        ),
    )
    parser.add_argument("--county", help="County name (lake | mahoning | cuyahoga)")
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
