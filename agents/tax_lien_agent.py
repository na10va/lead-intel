from __future__ import annotations
"""
agents/tax_lien_agent.py — Scrapes tax delinquent records from Ohio county treasurer portals.

Sources (Ohio POC):

  Cuyahoga:
    Source: Annual Delinquent Land Tax Notice PDF published by the Cuyahoga County
            Fiscal Officer (~October each year, per ORC §5721.03).
    URL pattern: cuyahogacms.blob.core.windows.net/home/docs/default-source/
                 fiscal-library/delinquent-publications/delinquenttaxlistnotice-MMDDYY.pdf
    Access: Public, no auth. Fetched via requests (no Playwright needed).
    Format: Multi-column text PDF. Each record: PARCEL_ID  OWNER_NAME  $AMOUNT
    Fields: parcel_id, owner_name, delinquent_amount. Property address is NOT
            in the PDF — enrichment populates it via county auditor parcel lookup.
    Cadence: Annual (published ~Oct 15). Agent re-ingests new records on each
             run; dedup skips parcels already in DB. Update CUYAHOGA_PDF_URL
             each year when the new publication is posted.
    Confirmed live: 2026-04-19 (2024 publication, 24 pages, ~1,500 records).

  Mahoning:
    BLOCKED — two infrastructure issues confirmed 2026-04-19:
    1. auditor.mahoningcountyoh.gov/DelinquencyReport
       Cloudflare 403 blocks all headless browsers. BrightData also blocked
       (government site policy — same as Lake County probate). Confirmed.
    2. mahoningoh-auditor.pivotpoint.us/DelinquencyReport
       TLS 1.0/1.1 handshake failure — not supported by Chromium or Python
       3.9 ssl module. curl also fails. Server-side protocol issue.
    Owner action needed: Contact Mahoning County Auditor (330-740-2010) to
    request a bulk CSV/Excel export, or evaluate Zyte API (handles Cloudflare
    better than BrightData for government sites). Source flagged automatically.

  Lake:
    No dedicated delinquent list view confirmed (research: 2026-04-19).
    iViewAuditor advanced search has no delinquent-status filter.
    Fallback attempted on each run — raises LakeNoListViewError if still absent.
    Owner action needed: Contact Lake County Auditor (440-350-2528) for export.

IRS FLTPS: No public API. Federal tax liens are captured downstream by the
foreclosure agent when cases reach the lis pendens / court filing stage.

filing_date convention:
    Tax delinquent lists are cumulative (not daily filings). filing_date is
    set to date.today() (ingest date). Delinquency year and amount are stored
    in raw_data. Gate 1 date check passes cleanly for all records.

CLI:
    python agents/tax_lien_agent.py --county cuyahoga --state OH
    python agents/tax_lien_agent.py --county lake --state OH
    python agents/tax_lien_agent.py --county mahoning --state OH
    python agents/tax_lien_agent.py --all-counties --state OH
"""

import argparse
import asyncio
import io
import re
from datetime import date
from typing import Callable

import pdfplumber
import requests
from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright

from db.client import get_client, insert_row, update_row
from enrichment.waterfall import enrich_lead
from routing.va_router import route_lead
from scoring.score import score_lead
from utils.deduper import is_duplicate
from utils.logger import get_logger
from verification.verify_leads import verify_raw_record

load_dotenv()

log = get_logger("tax_lien_agent")

OHIO_COUNTIES = ["cuyahoga", "lake", "mahoning"]

# Update this URL each October when the new annual publication is posted.
# URL pattern: delinquenttaxlistnotice-MMDDYY.pdf  (e.g. 101524 = Oct 15, 2024)
CUYAHOGA_PDF_URL = (
    "https://cuyahogacms.blob.core.windows.net/home/docs/default-source/"
    "fiscal-library/delinquent-publications/delinquenttaxlistnotice-101524.pdf"
)

# Cuyahoga parcel format: DDD-DD-DDD (confirmed from live PDF 2026-04-19)
_CUYAHOGA_PARCEL_RE = re.compile(r"(\d{3}-\d{2}-\d{3})")
_AMOUNT_RE = re.compile(r"\$([\d,]+\.?\d*)")

# Forfeited Land Sale legal advertisement — update URL each year after new sale is published.
# Page: cuyahogacounty.gov/fiscal-officer/departments/real-property/forfeited-lands
CUYAHOGA_FORFEITED_LANDS_URL = (
    "https://cuyahogacms.blob.core.windows.net/home/docs/default-source/"
    "fiscal-library/forfeitedlandsales/legaladvertisement.pdf?sfvrsn=6cdbd6f4_2"
)
# Matches DDD-DD-DDD, DDD-DD-DDDC, DDD-DD-DDDDC (Cuyahoga parcel variants)
_FORFEITED_PARCEL_RE = re.compile(r"\d{3}-\d{2}-\d{3,4}[A-Z]?")
_FORFEITED_RECORD_RE = re.compile(
    r"(\d{3}-\d{2}-\d{3,4}[A-Z]?)\s+((?:CV|BR)\s*\d{5,7})\s+(.+?)\s+(\$[\d,]+\.\d{2})"
)
_EMBEDDED_PARCEL_RE = re.compile(r"\s+\d{3}-\d{2}-\d{3,4}[A-Z]?\s+(?:CV|BR)\s*\d+.*$")
_EMBEDDED_CASE_RE = re.compile(r"\s+(?:CV|BR)\s*\d{5,7}\s+.*$")


# =============================================================================
# Cuyahoga County — Annual Delinquent Land Tax Notice PDF
#
# The PDF is published once per year (~Oct 15) by the Cuyahoga County Fiscal
# Officer under ORC §5721.03. It lists every parcel certified delinquent for
# one year or more. Data confirmed parseable on 2026-04-19 (24 pages, text PDF).
#
# Page layout: 3-column newspaper format.
# Per-record format: PARCEL_ID  OWNER_NAME  $AMOUNT  (no property address)
# Property address is populated by enrichment via county auditor parcel lookup.
#
# No Playwright needed — plain requests download. 3-5 s between retries only.
# =============================================================================

def _parse_cuyahoga_pdf_page(text: str) -> list[dict]:
    """Extract all delinquent records from one PDF page of text.

    Splits on parcel ID occurrences (DDD-DD-DDD), then extracts the owner name
    and delinquent amount from the text segment between consecutive parcel IDs.
    Handles the 3-column newspaper layout without needing column coordinates.
    """
    records: list[dict] = []
    positions = list(_CUYAHOGA_PARCEL_RE.finditer(text))

    for i, match in enumerate(positions):
        parcel_id = match.group(1)

        # Segment between this parcel ID and the next one (or end of page)
        seg_start = match.end()
        seg_end = positions[i + 1].start() if i + 1 < len(positions) else len(text)
        segment = text[seg_start:seg_end].strip()

        # Amount is the last $X.XX token before the next parcel ID
        amt_match = _AMOUNT_RE.search(segment)
        if amt_match:
            delinquent_amount = "$" + amt_match.group(1)
            # Owner name is everything before the amount
            owner_name = segment[: amt_match.start()].strip()
        else:
            delinquent_amount = ""
            owner_name = segment.strip()

        # Skip header rows and empty segments
        if not owner_name or owner_name.upper() in (
            "OWNER", "TAXPAYER", "NAME", "LEGAL ADVERTISING",
        ):
            continue

        records.append({
            "parcel_id": parcel_id,
            "owner_name": owner_name,
            "delinquent_amount": delinquent_amount,
            "property_address": None,   # not in PDF; enrichment fills via parcel lookup
            "delinquency_years": [],
            "_county": "Cuyahoga",
            "_source_url": CUYAHOGA_PDF_URL,
        })

    return records


def _fetch_cuyahoga(_page: Page, _since: date) -> list[dict]:
    """Download and parse the Cuyahoga annual delinquent tax PDF.

    Uses requests (not Playwright). `_page` and `_since` are unused — the PDF
    is a full annual list and has no date filter. Dedup handles idempotency.

    Raises RuntimeError if the PDF is unreachable (triggers self-healer).
    Update CUYAHOGA_PDF_URL each October when the new publication is posted.
    """
    log.info(f"Downloading Cuyahoga annual delinquent PDF: {CUYAHOGA_PDF_URL}")
    try:
        resp = requests.get(CUYAHOGA_PDF_URL, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(
            f"Cuyahoga PDF download failed: {e}. "
            "URL may need updating — check cuyahogacounty.gov/fiscal-officer/"
            "departments/real-property/delinquent-publication for new PDF link."
        ) from e

    results: list[dict] = []
    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        log.info(f"Cuyahoga PDF: {len(pdf.pages)} pages")
        for pg_num, pg in enumerate(pdf.pages):
            text = pg.extract_text() or ""
            if not text:
                log.debug(f"Cuyahoga PDF page {pg_num}: no extractable text")
                continue
            page_records = _parse_cuyahoga_pdf_page(text)
            results.extend(page_records)
            log.debug(f"Cuyahoga PDF page {pg_num}: {len(page_records)} records")

    log.info(f"Cuyahoga: parsed {len(results)} delinquent records from PDF")
    return results


# =============================================================================
# Lake County — auditor.lakecountyohio.gov/search/advancedsearch.aspx?mode=advanced
#
# iViewAuditor (ASP.NET). No dedicated delinquent list view confirmed (2026-04-19).
# The advanced search has no delinquent-status filter. This scraper checks on
# every run in case one is added. If still absent, raises LakeNoListViewError.
#
# Owner action: Contact Lake County Auditor (440-350-2528 / auditor@lakecountyohio.org)
# to request a bulk delinquent property CSV/Excel export.
# =============================================================================

class LakeNoListViewError(RuntimeError):
    """Raised when Lake County iViewAuditor has no delinquent list view or filter."""


async def _fetch_lake_async(page: Page, _since: date) -> list[dict]:
    """Attempt to scrape Lake County delinquent properties via iViewAuditor.

    Checks the advanced search form for a delinquent-status field.
    Raises LakeNoListViewError if no filter is found (caught in _run_async).
    """
    import random

    url = "https://auditor.lakecountyohio.gov/search/advancedsearch.aspx?mode=advanced"
    results: list[dict] = []

    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    # Check for a delinquent-status field — common iViewAuditor field names.
    # SELECTORS: update if iViewAuditor exposes a delinquent filter in future.
    delinquent_field = await page.query_selector(
        "select[id*='Delinquent'], select[id*='delinquent'], "
        "select[name*='Delinquent'], select[name*='TaxStatus'], "
        "input[id*='Delinquent'][type='checkbox'], "
        "input[name*='Delinquent'][type='checkbox']"
    )

    if not delinquent_field:
        raise LakeNoListViewError(
            "Lake County iViewAuditor advanced search has no delinquent-status filter (confirmed 2026-04-19). "
            "Contact Lake County Auditor at 440-350-2528 or auditor@lakecountyohio.org "
            "to request a bulk delinquent property export."
        )

    # Filter found — select "Yes" or equivalent
    tag = (await delinquent_field.evaluate("el => el.tagName")).upper()
    if tag == "SELECT":
        for val in ("Yes", "YES", "Y", "1", "true", "Delinquent"):
            try:
                await delinquent_field.select_option(label=val)
                break
            except Exception:
                try:
                    await delinquent_field.select_option(value=val)
                    break
                except Exception:
                    continue
    else:
        await delinquent_field.check()

    await asyncio.sleep(random.uniform(1, 2))

    submit_btn = await page.query_selector(
        "input[type='submit'][id*='Search'], button[type='submit'][id*='Search'], "
        "input[value='Search'], button[type='submit']"
    )
    if submit_btn:
        async with page.expect_navigation(wait_until="networkidle", timeout=30000):
            await submit_btn.click()
    else:
        await page.keyboard.press("Enter")
        await page.wait_for_load_state("networkidle")

    # Parse paginated ASP.NET GridView results
    # SELECTORS: update after live run confirms GridView control ID.
    col_map: dict[str, int] = {}
    page_num = 1

    while True:
        header_cells = await page.query_selector_all(
            "table#GridView1 th, table[id*='Grid'] th, table.grid th, table th"
        )
        if header_cells and not col_map:
            for i, th in enumerate(header_cells):
                col_map[(await th.inner_text()).strip().lower()] = i

        rows = await page.query_selector_all(
            "table#GridView1 tbody tr, table[id*='Grid'] tbody tr, table tbody tr"
        )
        if not rows:
            break

        for row in rows:
            cells = await row.query_selector_all("td")
            if not cells or len(cells) < 2:
                continue
            texts = [t.strip() for t in [await c.inner_text() for c in cells]]

            def _col(*names: str) -> str:
                for name in names:
                    for key, idx in col_map.items():
                        if name in key and idx < len(texts):
                            return texts[idx]
                return ""

            if col_map:
                parcel_id = _col("parcel", "pin", "account")
                owner_name = _col("owner", "taxpayer", "name")
                address = _col("address", "situs", "location", "property")
                delinquent_amount = _col("delinquent", "amount", "balance")
            else:
                parcel_id, owner_name = texts[0], texts[1] if len(texts) > 1 else ""
                address = texts[2] if len(texts) > 2 else ""
                delinquent_amount = texts[3] if len(texts) > 3 else ""

            if not parcel_id and not address:
                continue
            results.append({
                "owner_name": owner_name,
                "property_address": address or None,
                "parcel_id": parcel_id,
                "delinquent_amount": delinquent_amount,
                "delinquency_years": [],
                "_county": "Lake",
                "_source_url": url,
            })

        # ASP.NET GridView pagination
        next_link = await page.query_selector(
            f'a[href*="Page${page_num + 1}"], a[title="Next Page"], a[rel="next"]'
        )
        if not next_link:
            break
        await next_link.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(random.uniform(2, 4))
        page_num += 1

    log.info(f"Lake County: fetched {len(results)} delinquent records")
    return results


def _fetch_lake(page: Page, since: date) -> list[dict]:
    """Synchronous wrapper — runs _fetch_lake_async in the active event loop."""
    return _fetch_lake_async(page, since)  # called as coroutine by _run_async


# =============================================================================
# Mahoning County — BLOCKED (confirmed 2026-04-19)
#
# Two infrastructure issues prevent automated access:
#
# 1. auditor.mahoningcountyoh.gov/DelinquencyReport
#    Cloudflare 403 blocks all headless Playwright browsers.
#    BrightData Scraping Browser also blocked — government site policy
#    (same as Lake County probate; BrightData blocks .gov / government domains).
#    Confirmed: returns "Just a moment..." Cloudflare challenge page.
#
# 2. mahoningoh-auditor.pivotpoint.us/DelinquencyReport
#    TLS 1.0/1.1 handshake failure — Chromium, requests, and curl all fail.
#    Server appears to use an old SSL cipher suite that modern clients reject.
#    ERR_SSL_VERSION_OR_CIPHER_MISMATCH / SSLV3_ALERT_HANDSHAKE_FAILURE
#
# Resolution options (owner action required):
#   A. Contact Mahoning County Auditor (330-740-2010) for a bulk CSV/Excel
#      export — many Ohio counties provide these on request.
#   B. Evaluate Zyte API (https://www.zyte.com/smart-proxy-manager/) as an
#      alternative to BrightData for government sites. Zyte is specifically
#      designed for difficult targets including Cloudflare-protected pages.
#   C. For the PivotPoint SSL issue: try accessing via an older Python/OpenSSL
#      build or a proxy that terminates and re-establishes the TLS connection.
# =============================================================================

class MahoningBlockedError(RuntimeError):
    """Raised when all Mahoning County access paths are blocked."""


async def _fetch_mahoning(_page: Page, _since: date) -> list[dict]:
    """Mahoning County scraper — raises MahoningBlockedError (both paths blocked).

    Both URLs were confirmed blocked on 2026-04-19. This function documents
    the blockers and raises immediately so _run_async can alert the owner.
    Remove this stub and implement the scraper once a workaround is available.
    """
    raise MahoningBlockedError(
        "Mahoning County DelinquencyReport is blocked via two paths (confirmed 2026-04-19): "
        "1) auditor.mahoningcountyoh.gov — Cloudflare 403, BrightData government policy blocks. "
        "2) mahoningoh-auditor.pivotpoint.us — TLS 1.0/1.1 SSL handshake failure. "
        "Contact Mahoning County Auditor (330-740-2010) for bulk CSV export, "
        "or evaluate Zyte API as a Cloudflare-capable proxy alternative."
    )


# County scraper registry
# Values are coroutine functions (async def) or sync functions used as coroutines.
# _fetch_cuyahoga is sync (uses requests); it's wrapped in asyncio.to_thread below.
COUNTY_SCRAPERS: dict[str, Callable] = {
    "cuyahoga": _fetch_cuyahoga,
    "lake": _fetch_lake_async,
    "mahoning": _fetch_mahoning,
}


# =============================================================================
# Lake County — Excel file ingest (from Karen at Lake County Auditor's office)
#
# Karen emails the delinquent taxpayer report on the first Monday of each month
# in response to an automated request from scheduler/lake_county_email.py.
# The file is an Excel workbook with a fixed column layout (confirmed from the
# 04-01-2026 DELINQUENT TAXPAYER RPT.xlsx received April 1, 2026):
#
# Col  0: # (row counter)         Col 11: CERT DQ YR
# Col  1: PARCEL (dedup key)      Col 12: DELQ AMT
# Col  2: TRS CODE                Col 13: FH AMT
# Col  3: OWNER                   Col 14: SH AMT
# Col  4: MAILING (owner name)    Col 15: TOTAL DUE
# Col  5: # (mailing addr num)    Col 16: # (property addr num)
# Col  6: STREET (mailing)        Col 17: STREET (property)
# Col  7: SUF (mailing)           Col 18: SUF (property)
# Col  8: CITY (mailing)          Col 19: CITY (property)
# Col  9: ST (mailing)            Col 20: ST (property)
# Col 10: ZIP (mailing)           Col 21: ZIP (property)
#
# Dedup rule: lists include multiple owners per parcel (co-ownership).
# Keep the first row per PARCEL within the file, then run standard DB dedup.
# =============================================================================

def ingest_lake_county_file(filepath: str, state: str = "OH") -> None:
    """Ingest a Lake County delinquent taxpayer Excel report through the full pipeline.

    Called manually: python agents/tax_lien_agent.py --county lake --ingest-file <path>
    Or automatically after receiving Karen's monthly email reply.
    """
    import openpyxl

    log.info(f"Ingesting Lake County delinquent file: {filepath}")

    wb = openpyxl.load_workbook(filepath)
    ws = wb.active

    # Within-file dedup: keep first row per parcel (handles co-ownership rows)
    seen_parcels: set[str] = set()
    raw_records: list[dict] = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        parcel_id = str(row[1]).strip() if row[1] is not None else ""
        if not parcel_id or parcel_id.lower() == "none":
            continue
        if parcel_id in seen_parcels:
            continue
        seen_parcels.add(parcel_id)

        # Assemble property address from cols 16–21
        prop_num = str(int(row[16])) if row[16] and str(row[16]) != "0" else ""
        prop_street = str(row[17]).strip() if row[17] else ""
        prop_suf = str(row[18]).strip() if row[18] else ""
        prop_city = str(row[19]).strip() if row[19] else ""
        prop_state_val = str(row[20]).strip() if row[20] else state
        prop_zip = str(row[21]).strip() if row[21] else ""

        street_parts = [p for p in [prop_num, prop_street, prop_suf] if p]
        property_address = " ".join(street_parts)
        if prop_city:
            property_address += f", {prop_city}"
        if prop_state_val:
            property_address += f", {prop_state_val}"
        if prop_zip:
            property_address += f" {prop_zip}"

        total_due = row[15]
        delq_amt = row[12]
        delinquent_amount = (
            f"${float(total_due):.2f}" if total_due else
            f"${float(delq_amt):.2f}" if delq_amt else ""
        )

        cert_dq_yr = row[11]
        raw_records.append({
            "parcel_id": parcel_id,
            "owner_name": str(row[3]).strip() if row[3] else "",
            "property_address": property_address or None,
            "delinquent_amount": delinquent_amount,
            "delinquency_years": [cert_dq_yr] if cert_dq_yr else [],
            "_county": "Lake",
            "_source_url": filepath,
        })

    log.info(f"Lake County file: {len(raw_records)} unique parcels after within-file dedup")

    if not raw_records:
        log.warning("No records parsed from Lake County file — check file format")
        return

    client = get_client()
    new_records = 0
    tier_a_count = 0
    tier_b_count = 0

    for raw in raw_records:
        try:
            # PARSE
            record = parse_tax_lien(raw, "lake", state)
            if not record.get("owner_name") and not record.get("property_address"):
                continue

            # DEDUPE against DB
            if is_duplicate(
                county="Lake",
                source_type="tax_lien",
                parcel_id=record.get("parcel_id"),
                property_address=record.get("property_address"),
                owner_name=record.get("owner_name"),
            ):
                continue

            # STORE
            stored = insert_row("raw_leads", record)
            lead_id = stored.get("id")
            if not lead_id:
                log.error(f"Insert returned no ID for parcel {record.get('parcel_id')}")
                continue
            new_records += 1

            # VERIFY Gate 1
            if not verify_raw_record(lead_id):
                log.debug(f"Lead {lead_id} failed Gate 1 — skipping enrichment")
                continue

            # ENRICH
            enrich_lead(lead_id)

            # VERIFY Gate 2
            refreshed = (
                client.table("raw_leads")
                .select("*")
                .eq("id", lead_id)
                .single()
                .execute()
                .data
            )
            if not refreshed or not refreshed.get("verified_enriched"):
                log.debug(f"Lead {lead_id} failed Gate 2 — skipping scoring")
                continue

            # SCORE
            result = score_lead(refreshed)
            update_row("raw_leads", lead_id, {**result, "scored_at": "now()"})
            log.info(
                f"Scored: {record.get('parcel_id')} {record.get('owner_name')} | "
                f"distress={result['distress_score']} deal={result['deal_score']} "
                f"total={result['score']} tier={result['tier']}"
            )

            # ROUTE
            if result["tier"] == "A":
                tier_a_count += 1
                route_lead(lead_id, result["tier"])
            elif result["tier"] == "B":
                tier_b_count += 1
                route_lead(lead_id, result["tier"])

        except Exception as e:
            log.error(f"Error processing Lake County record {raw.get('parcel_id')}: {e}")
            continue

    log.info(
        f"Lake County file ingest complete — {new_records} new records stored "
        f"({tier_a_count} Tier A, {tier_b_count} Tier B)"
    )

# =============================================================================
# Cuyahoga County — Forfeited Land Sale legal advertisement PDF
#
# Published annually before the September sale. Lists all parcels forfeited to
# the State of Ohio for long-term tax delinquency and offered at public auction.
# Format: 4-column newspaper layout; per-record: PARCEL CASE_NO NAME $OPENING_BID
#
# URL: cuyahogacounty.gov/fiscal-officer/departments/real-property/forfeited-lands
# Update CUYAHOGA_FORFEITED_LANDS_URL each year after the new advertisement posts.
#
# filing_date convention: today (ingest date) — same as the delinquent tax list.
# Property address not in PDF; enrichment populates via county auditor parcel lookup.
# =============================================================================

def _parse_forfeited_lands_pdf(content: bytes) -> list[dict]:
    """Extract records from the Cuyahoga Forfeited Land Sale legal advertisement PDF.

    The PDF has a 4-column layout; pdfplumber extracts text left-to-right across
    columns, so records from different columns appear on the same line. The regex
    matches each record by anchoring on parcel ID + case number + dollar amount.
    Names that wrap into an adjacent column are cleaned by stripping any embedded
    parcel IDs or case numbers from the name field.
    """
    records = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        full_text = ""
        for pg in pdf.pages:
            full_text += " " + (pg.extract_text() or "")

    flat = " ".join(full_text.split())

    for m in _FORFEITED_RECORD_RE.finditer(flat):
        name = m.group(3).strip()
        # Remove column bleed-through: next parcel ID + case number onward
        name = _EMBEDDED_PARCEL_RE.sub("", name)
        name = _EMBEDDED_CASE_RE.sub("", name)
        name = name.strip().rstrip(",").strip()

        if not name:
            continue

        records.append({
            "parcel_id": m.group(1),
            "owner_name": name,
            "delinquent_amount": m.group(4),         # opening bid = tax owed
            "property_address": None,                 # not in PDF; enrichment fills
            "delinquency_years": [],
            "case_no": m.group(2).replace(" ", ""),
            "_county": "Cuyahoga",
            "_source_url": CUYAHOGA_FORFEITED_LANDS_URL,
        })

    return records


def ingest_cuyahoga_forfeited_lands(state: str = "OH") -> None:
    """Download and ingest the Cuyahoga County Forfeited Land Sale advertisement.

    Parses all property records from the legal advertisement PDF, deduplicates
    against existing raw_leads records, and runs each net-new record through
    the full 9-step pipeline (store → verify → enrich → score → route).

    CLI: python agents/tax_lien_agent.py --forfeited-lands
    """
    log.info(f"Downloading Cuyahoga Forfeited Land Sale PDF: {CUYAHOGA_FORFEITED_LANDS_URL}")
    try:
        resp = requests.get(
            CUYAHOGA_FORFEITED_LANDS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(
            f"Forfeited Lands PDF download failed: {e}. "
            "Update CUYAHOGA_FORFEITED_LANDS_URL if the annual advertisement has moved."
        )
        return

    raw_records = _parse_forfeited_lands_pdf(resp.content)
    log.info(f"Cuyahoga Forfeited Land Sale: parsed {len(raw_records)} records from PDF")

    if not raw_records:
        log.warning("No forfeited land records parsed — check PDF format or URL")
        return

    client = get_client()
    new_records = 0
    tier_a_count = 0
    tier_b_count = 0

    for raw in raw_records:
        try:
            record = parse_tax_lien(raw, "cuyahoga", state)
            record["source_name"] = "Cuyahoga County Forfeited Land Sale"
            # raw_data already set by parse_tax_lien; add case number
            record["raw_data"]["case_no"] = raw.get("case_no", "")

            if not record.get("owner_name") and not record.get("parcel_id"):
                continue

            if is_duplicate(
                county="Cuyahoga",
                source_type="tax_lien",
                parcel_id=record.get("parcel_id"),
                property_address=record.get("property_address"),
                owner_name=record.get("owner_name"),
            ):
                continue

            stored = insert_row("raw_leads", record)
            lead_id = stored.get("id")
            if not lead_id:
                log.error(f"Insert returned no ID for parcel {record.get('parcel_id')}")
                continue
            new_records += 1

            if not verify_raw_record(lead_id):
                log.debug(f"Lead {lead_id} failed Gate 1 — skipping enrichment")
                continue

            enrich_lead(lead_id)

            refreshed = (
                client.table("raw_leads")
                .select("*")
                .eq("id", lead_id)
                .single()
                .execute()
                .data
            )
            if not refreshed or not refreshed.get("verified_enriched"):
                log.debug(f"Lead {lead_id} failed Gate 2 — skipping scoring")
                continue

            result = score_lead(refreshed)
            update_row("raw_leads", lead_id, {**result, "scored_at": "now()"})
            log.info(
                f"Scored: {record.get('parcel_id')} {record.get('owner_name')} | "
                f"distress={result['distress_score']} deal={result['deal_score']} "
                f"total={result['score']} tier={result['tier']}"
            )

            if result["tier"] == "A":
                tier_a_count += 1
                route_lead(lead_id, result["tier"])
            elif result["tier"] == "B":
                tier_b_count += 1
                route_lead(lead_id, result["tier"])

        except Exception as e:
            log.error(f"Error processing forfeited land record {raw.get('parcel_id')}: {e}")
            continue

    log.info(
        f"Cuyahoga Forfeited Lands ingest complete — {new_records} new records stored "
        f"({tier_a_count} Tier A, {tier_b_count} Tier B)"
    )


# Counties that use pure requests (no Playwright page needed)
_SYNC_SCRAPERS = {"cuyahoga"}


# =============================================================================
# Parse — map raw scraped dict to raw_leads schema
# =============================================================================

def parse_tax_lien(raw: dict, county: str, state: str) -> dict:
    """Map a raw tax lien record to the raw_leads table schema.

    filing_date = today (ingest date) because delinquent lists are cumulative.
    Delinquency year and balance stored in raw_data for deal-scoring context.
    Property address may be None for Cuyahoga (PDF has no address — enrichment
    populates it via parcel ID lookup against the county auditor).
    """
    return {
        "owner_name": (raw.get("owner_name") or "").strip(),
        "property_address": (raw.get("property_address") or "").strip() or None,
        "parcel_id": (raw.get("parcel_id") or "").strip() or None,
        "filing_date": date.today().isoformat(),
        "raw_data": {
            "delinquent_amount": raw.get("delinquent_amount", ""),
            "delinquency_years": raw.get("delinquency_years", []),
            "source_url": raw.get("_source_url", ""),
        },
        "source_type": "tax_lien",
        "source_name": f"{county.title()} County Treasurer",
        "state": state,
        "county": county.title(),
        "verified_raw": False,
        "verified_enriched": False,
        "enriched": False,
    }


# =============================================================================
# Main agent — full 9-step pipeline
# =============================================================================

async def _run_async(county: str, state: str) -> None:
    """Async inner loop — fetch + full 9-step pipeline for one county."""
    scraper = COUNTY_SCRAPERS.get(county.lower())
    if not scraper:
        log.error(f"No scraper registered for county: {county}")
        return

    log.info(f"Tax lien agent starting — {county.title()} County, {state}")

    # 1. FETCH
    # Cuyahoga uses requests (no browser needed); Lake/Mahoning use Playwright.
    if county.lower() in _SYNC_SCRAPERS:
        try:
            raw_records = await asyncio.get_event_loop().run_in_executor(
                None, scraper, None, date.today()
            )
        except Exception as e:
            log.error(f"FETCH failed for {county}: {e}")
            from maintenance.self_healer import handle_failure
            handle_failure(f"tax_lien_{county}", str(e))
            return
    else:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            try:
                raw_records = await scraper(page, date.today())
            except LakeNoListViewError as e:
                log.warning(str(e))
                try:
                    get_client().table("sources").update({
                        "needs_manual_review": True,
                        "status": "degraded",
                    }).eq("source_name", "Lake County Treasurer").execute()
                except Exception:
                    pass
                await browser.close()
                return
            except MahoningBlockedError as e:
                log.warning(str(e))
                try:
                    get_client().table("sources").update({
                        "blocked": True,
                        "needs_manual_review": True,
                        "status": "blocked",
                    }).eq("source_name", "Mahoning County Treasurer").execute()
                except Exception:
                    pass
                await browser.close()
                return
            except Exception as e:
                log.error(f"FETCH failed for {county}: {e}")
                from maintenance.self_healer import handle_failure
                handle_failure(f"tax_lien_{county}", str(e))
                await browser.close()
                return

            await browser.close()

    if not raw_records:
        log.warning(f"No tax lien records returned for {county} — check source or selectors")
        from maintenance.self_healer import handle_failure
        handle_failure(f"tax_lien_{county}", "Zero records returned")
        return

    log.info(f"Processing {len(raw_records)} raw records for {county}")
    new_records = 0
    tier_a_count = 0
    tier_b_count = 0

    for raw in raw_records:
        try:
            # 2. PARSE
            record = parse_tax_lien(raw, county, state)
            if not record.get("owner_name") and not record.get("property_address"):
                continue

            # 3. DEDUPE — parcel_id is the natural idempotency key for tax liens
            if is_duplicate(
                county=county.title(),
                source_type="tax_lien",
                parcel_id=record.get("parcel_id"),
                property_address=record.get("property_address"),
                owner_name=record.get("owner_name"),
            ):
                continue

            # 4. STORE
            stored = insert_row("raw_leads", record)
            lead_id = stored.get("id")
            if not lead_id:
                log.error(f"Insert returned no ID for {record.get('parcel_id')}")
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
            update_row("raw_leads", lead_id, {**result, "scored_at": "now()"})
            log.info(
                f"Scored: {record.get('parcel_id')} {record.get('owner_name')} | "
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
            log.error(f"Error processing tax lien record for {county}: {e}")
            continue

    log.info(
        f"Tax lien agent complete — {county.title()} County | "
        f"{new_records} new records stored | {tier_a_count} Tier A, {tier_b_count} Tier B"
    )


def run(county: str, state: str = "OH") -> None:
    """Run the tax lien agent for one county (synchronous entry point)."""
    asyncio.run(_run_async(county, state))


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tax lien agent — Ohio POC",
        epilog=(
            "Cuyahoga: parses annual delinquent PDF (~1,500 records). "
            "Lake/Mahoning: currently blocked — see module docstring for resolution steps."
        ),
    )
    parser.add_argument("--county", help="County name (cuyahoga | lake | mahoning)")
    parser.add_argument("--state", default="OH", help="State code (default: OH)")
    parser.add_argument("--all-counties", action="store_true", help="Run all Ohio POC counties")
    parser.add_argument(
        "--ingest-file",
        metavar="PATH",
        help="Ingest a Lake County delinquent Excel file directly (skips scraping)",
    )
    parser.add_argument(
        "--forfeited-lands",
        action="store_true",
        help="Ingest the Cuyahoga County Forfeited Land Sale legal advertisement PDF",
    )
    args = parser.parse_args()

    if args.forfeited_lands:
        ingest_cuyahoga_forfeited_lands(args.state)
    elif args.ingest_file:
        ingest_lake_county_file(args.ingest_file, args.state)
    elif args.all_counties:
        for c in OHIO_COUNTIES:
            run(c, args.state)
    elif args.county:
        run(args.county.lower(), args.state)
    else:
        parser.print_help()
