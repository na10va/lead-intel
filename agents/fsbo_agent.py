from __future__ import annotations
"""
agents/fsbo_agent.py — Tier D: scrapes FSBO listings from FSBO.com.

IMPORTANT: Tier D — stored only. FSBO agent does NOT score or route.
All FSBO leads flow into the Zillow Deal Finder module for ARV-based scoring.
Never enriched with Skip Sherpa — listing data already includes contact info.

Source:
    FSBO.com Ohio listings — https://www.fsbo.com/search/ohio
    Craigslist: DROPPED — explicit ToS ban + $60M+ enforcement history (Instamotor).
                Legal risk is not justified for Tier D data. Do not reintroduce.

Access method:
    Playwright + headless Chromium. FSBO.com uses React with server-side rendering;
    listings load synchronously on the search results page. Respectful crawl rate:
    3–5 second delay between page requests per CLAUDE.md rules.

    If FSBO.com blocks the scraper (HTTP 403 or empty results for 2+ days):
    - Flag source in Supabase as blocked=True
    - Alert owner via SMS
    - Evaluate Apify actor (benthepythondev/fsbo-real-estate-scraper, ~$0.015/listing)
      or direct FSBO.com API inquiry as alternatives within budget.

County filtering:
    FSBO.com has no county-level filter — only state and city. Results are filtered
    post-fetch by matching the listing city against COUNTY_CITIES. Listings in cities
    not in the target counties are discarded without storing.

Fields captured:
    address, list_price, beds, baths, sqft, lot_size, year_built, property_type,
    seller_name, seller_phone, listing_url. These feed into zillow_deals for ARV scoring.

Storage table:
    Records are written to both raw_leads (source_type="fsbo", tier="D") AND to the
    zillow_deals table stub so the Zillow Deal Finder can pick them up for ARV scoring.
    The zillow_deals insert is best-effort — failure does not abort the raw_leads insert.

CLI:
    python agents/fsbo_agent.py --county cuyahoga --state OH
    python agents/fsbo_agent.py --county lake --state OH
    python agents/fsbo_agent.py --county mahoning --state OH
    python agents/fsbo_agent.py --all-counties --state OH
"""

import argparse
import asyncio
import random
import re
from datetime import date
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright

from db.client import get_client, insert_row
from utils.deduper import is_duplicate
from utils.logger import get_logger

load_dotenv()

log = get_logger("fsbo_agent")

OHIO_COUNTIES = ["cuyahoga", "lake", "mahoning"]

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

FSBO_OHIO_URL = "https://www.fsbo.com/search/ohio"

# Cities used to assign county — filter FSBO.com results to target counties.
# FSBO.com returns city with each listing; unmatched cities are discarded.
COUNTY_CITIES: dict[str, set[str]] = {
    "Cuyahoga": {
        "cleveland", "cleveland heights", "lakewood", "parma", "euclid",
        "garfield heights", "maple heights", "shaker heights", "east cleveland",
        "westlake", "north olmsted", "strongsville", "berea", "brook park",
        "solon", "bedford", "bedford heights", "south euclid", "university heights",
        "richmond heights", "highland heights", "lyndhurst", "mayfield heights",
        "brecksville", "independence", "seven hills", "north royalton",
        "broadview heights", "olmsted falls", "fairview park", "rocky river",
        "bay village", "avon lake", "avon", "north ridgeville", "middleburg heights",
    },
    "Lake": {
        "mentor", "painesville", "willoughby", "eastlake", "wickliffe", "madison",
        "perry", "fairport harbor", "kirtland", "willoughby hills", "grand river",
        "concord", "leroy", "chardon", "mentor on the lake",
    },
    "Mahoning": {
        "youngstown", "boardman", "austintown", "canfield", "poland", "struthers",
        "campbell", "girard", "liberty", "hubbard", "niles", "vienna",
        "lowellville", "new middletown", "north jackson",
    },
}


def _city_to_county(city: str) -> Optional[str]:
    """Map a listing city to one of the three target Ohio counties. Returns None if no match."""
    city_lower = city.lower().strip()
    for county, cities in COUNTY_CITIES.items():
        if city_lower in cities:
            return county
    return None


def _parse_price(text: str) -> Optional[int]:
    """Parse a price string like '$125,000' or '125000' to integer cents. Returns None on failure."""
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_int(text: str) -> Optional[int]:
    """Parse a numeric string, stripping commas and non-digit chars. Returns None on failure."""
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_float(text: str) -> Optional[float]:
    """Parse a float string like '2.5' from mixed text. Returns None on failure."""
    match = re.search(r"[\d]+\.?[\d]*", text)
    return float(match.group()) if match else None


# =============================================================================
# FSBO.com scraper — Playwright
# =============================================================================

async def _fetch_fsbo_com_async(page: Page, target_county: Optional[str] = None) -> list[dict]:
    """Scrape FSBO.com Ohio listings and return those in target counties.

    Navigates the Ohio search page, iterates all result pages, extracts listing
    metadata, and filters by county using COUNTY_CITIES city matching.

    If target_county is provided, only listings matching that county are returned.
    If None, all three target counties are included (used for --all-counties run).

    SELECTORS: FSBO.com uses React with SSR. Listing cards render in the initial HTML
    payload. Confirmed structure as of 2026-04-20 — update selectors if 0 results
    persist for 2+ consecutive days (likely a markup redesign).

    Crawl rate: 3–5 second delay between pages per CLAUDE.md and FSBO.com ToS guidance.
    Maximum 3 retries on any blocked response before flagging the source.
    """
    results: list[dict] = []
    block_count = 0

    await page.goto(FSBO_OHIO_URL, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(3, 5))

    page_num = 1
    while True:
        # Detect block (Cloudflare challenge or login wall)
        page_title = await page.title()
        if any(kw in page_title.lower() for kw in ("just a moment", "access denied", "sign in")):
            block_count += 1
            log.warning(f"FSBO.com: possible block on page {page_num} (title: {page_title})")
            if block_count >= 3:
                log.error("FSBO.com blocked 3+ times — flagging source and aborting")
                _flag_fsbo_blocked()
                break
            await asyncio.sleep(random.uniform(10, 20))
            await page.reload(wait_until="networkidle")
            continue

        # Extract listing cards from the current page
        # SELECTORS: FSBO.com listing cards — update after live run confirms exact class names
        cards = await page.query_selector_all(
            "div[class*='listing-card'], "
            "div[class*='ListingCard'], "
            "article[class*='listing'], "
            "div[class*='property-card'], "
            "li[class*='listing']"
        )

        if not cards:
            # Try alternate: extract from JSON-LD structured data on the page
            cards_json = await _extract_jsonld_listings(page)
            if cards_json:
                results.extend(cards_json)
                log.debug(f"FSBO.com page {page_num}: {len(cards_json)} listings via JSON-LD")
            else:
                log.debug(f"FSBO.com page {page_num}: no listing cards found — may be last page")
                break
        else:
            for card in cards:
                raw = await _extract_card_fields(card)
                if raw:
                    results.append(raw)
            log.debug(f"FSBO.com page {page_num}: {len(cards)} cards parsed")

        # Pagination — FSBO.com uses numbered page links or a Next button
        # SELECTORS: update if pagination markup changes
        next_btn = await page.query_selector(
            "a[aria-label='Next page'], a[aria-label='Next Page'], "
            "a:has-text('Next'), button:has-text('Next'), "
            "a[rel='next'], li.next a, "
            f"a[href*='page={page_num + 1}'], a[href*='p={page_num + 1}']"
        )
        if not next_btn:
            log.debug(f"FSBO.com: no next page link after page {page_num} — done")
            break

        await next_btn.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(random.uniform(3, 5))    # respectful crawl rate
        page_num += 1

        # Safety cap — FSBO.com Ohio shouldn't have more than 50 pages
        if page_num > 50:
            log.warning("FSBO.com: hit 50-page safety cap — stopping pagination")
            break

    # Filter to target counties
    county_filtered: list[dict] = []
    for r in results:
        city = r.get("_city", "")
        county = _city_to_county(city)
        if county is None:
            continue
        if target_county and county.lower() != target_county.lower():
            continue
        r["_county"] = county
        county_filtered.append(r)

    log.info(
        f"FSBO.com: {len(results)} total Ohio listings scraped | "
        f"{len(county_filtered)} in target counties"
    )
    return county_filtered


async def _extract_card_fields(card) -> Optional[dict]:
    """Extract structured fields from a single FSBO.com listing card element.

    SELECTORS: card-level selectors for FSBO.com listing cards.
    Confirmed field names as of 2026-04-20 — update after first live run.
    """
    try:
        # Price
        price_el = await card.query_selector(
            "[class*='price'], [class*='Price'], span[itemprop='price'], "
            "div[class*='listing-price']"
        )
        price_text = await price_el.inner_text() if price_el else ""

        # Address — full address usually in a single element or split street/city
        addr_el = await card.query_selector(
            "[class*='address'], [class*='Address'], [itemprop='streetAddress'], "
            "address, div[class*='location']"
        )
        address_text = await addr_el.inner_text() if addr_el else ""

        # City (sometimes separate from street address)
        city_el = await card.query_selector(
            "[itemprop='addressLocality'], [class*='city'], [class*='City']"
        )
        city_text = (await city_el.inner_text() if city_el else "").strip()

        # Extract city from address_text if not in separate element
        if not city_text and "," in address_text:
            parts = address_text.split(",")
            if len(parts) >= 2:
                city_text = parts[-2].strip()  # e.g. "123 Main St, Cleveland, OH 44101"

        # Beds / Baths / SqFt
        beds_el = await card.query_selector(
            "[class*='beds'], [class*='Beds'], [aria-label*='bed'], span[class*='bed']"
        )
        beds_text = await beds_el.inner_text() if beds_el else ""

        baths_el = await card.query_selector(
            "[class*='baths'], [class*='Baths'], [aria-label*='bath'], span[class*='bath']"
        )
        baths_text = await baths_el.inner_text() if baths_el else ""

        sqft_el = await card.query_selector(
            "[class*='sqft'], [class*='SqFt'], [class*='squarefeet'], [aria-label*='sq']"
        )
        sqft_text = await sqft_el.inner_text() if sqft_el else ""

        # Listing URL
        link_el = await card.query_selector("a[href]")
        href = await link_el.get_attribute("href") if link_el else ""
        listing_url = f"https://www.fsbo.com{href}" if href and href.startswith("/") else href

        # Seller phone (sometimes visible on search results page)
        phone_el = await card.query_selector(
            "[class*='phone'], [class*='Phone'], [href^='tel:']"
        )
        seller_phone = ""
        if phone_el:
            href_val = await phone_el.get_attribute("href") or ""
            seller_phone = href_val.replace("tel:", "").strip() or await phone_el.inner_text()

        if not address_text and not city_text:
            return None

        return {
            "_address": address_text.strip(),
            "_city": city_text.strip(),
            "_list_price": _parse_price(price_text),
            "_beds": _parse_int(beds_text),
            "_baths": _parse_float(baths_text),
            "_sqft": _parse_int(sqft_text),
            "_seller_phone": seller_phone.strip(),
            "_listing_url": listing_url,
            "_source": "fsbo.com",
        }
    except Exception as e:
        log.debug(f"Error extracting FSBO card fields: {e}")
        return None


async def _extract_jsonld_listings(page: Page) -> list[dict]:
    """Extract listings from JSON-LD structured data if card selectors find nothing.

    FSBO.com may embed listing data as application/ld+json schema.org markup.
    This is a fallback for when the HTML card selectors don't match.
    """
    try:
        scripts = await page.query_selector_all("script[type='application/ld+json']")
        results: list[dict] = []
        import json
        for script in scripts:
            text = await script.inner_text()
            try:
                data = json.loads(text)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") in ("RealEstateListing", "Product", "Offer"):
                        addr = item.get("address", {})
                        results.append({
                            "_address": addr.get("streetAddress", ""),
                            "_city": addr.get("addressLocality", ""),
                            "_list_price": _parse_price(str(item.get("price", ""))),
                            "_beds": None,
                            "_baths": None,
                            "_sqft": None,
                            "_seller_phone": "",
                            "_listing_url": item.get("url", ""),
                            "_source": "fsbo.com",
                        })
            except (json.JSONDecodeError, AttributeError):
                continue
        return results
    except Exception:
        return []


def _flag_fsbo_blocked() -> None:
    """Mark FSBO.com as blocked in Supabase and alert owner via SMS."""
    try:
        get_client().table("sources").update({
            "blocked": True,
            "status": "blocked",
            "needs_manual_review": True,
        }).eq("source_name", "FSBO.com").execute()
    except Exception:
        pass
    try:
        from routing.notify import send_sms
        send_sms(
            "[SOURCE ALERT] FSBO.com is blocking the scraper. "
            "Evaluate Apify actor (benthepythondev/fsbo-real-estate-scraper) "
            "as an alternative. Source flagged."
        )
    except Exception:
        pass


# =============================================================================
# Parse — map raw scraped dict to raw_leads and zillow_deals schemas
# =============================================================================

def parse_fsbo_listing(raw: dict, state: str) -> dict:
    """Map a raw FSBO listing to the raw_leads table schema.

    FSBO leads are stored as Tier D in raw_leads AND written to zillow_deals
    so the Zillow Deal Finder can pick them up for ARV-based scoring.
    """
    county = raw.get("_county", "")
    address = raw.get("_address", "").strip()

    return {
        "owner_name": None,          # not available from FSBO.com search results
        "property_address": address or None,
        "parcel_id": None,
        "filing_date": date.today().isoformat(),
        "source_type": "fsbo",
        "source_name": "FSBO.com",
        "state": state,
        "county": county,
        "raw_data": {
            "list_price": raw.get("_list_price"),
            "beds": raw.get("_beds"),
            "baths": raw.get("_baths"),
            "sqft": raw.get("_sqft"),
            "seller_phone": raw.get("_seller_phone", ""),
            "listing_url": raw.get("_listing_url", ""),
            "source": raw.get("_source", "fsbo.com"),
        },
        "verified_raw": False,
        "verified_enriched": False,
        "enriched": False,
    }


def _insert_zillow_deals_row(raw: dict, county: str) -> None:
    """Write an FSBO listing to the zillow_deals table for ARV scoring.

    Best-effort — failure is logged but does not abort the raw_leads insert.
    The zillow_deals table schema requires address, county, list_price, and listing_type.
    ARV fields (zestimate_arv, comp_arv, pct_of_comp_arv) are NULL until the
    Zillow Deal Finder module runs its ARV calculation on this record.
    """
    try:
        insert_row("zillow_deals", {
            "address": raw.get("_address", ""),
            "county": county,
            "list_price": raw.get("_list_price"),
            "zestimate_arv": None,
            "comp_arv": None,
            "pct_of_comp_arv": None,
            "beds": raw.get("_beds"),
            "baths": raw.get("_baths"),
            "sqft": raw.get("_sqft"),
            "days_on_market": None,
            "listing_type": "fsbo",
            "arv_conflict": False,
            "label": None,
            "zillow_url": raw.get("_listing_url", ""),
            "alerted_owner": False,
        })
    except Exception as e:
        log.debug(f"zillow_deals insert failed for FSBO listing (non-fatal): {e}")


# =============================================================================
# Main agent — Tier D store-only pipeline
# =============================================================================

def run(county: str, state: str = "OH") -> None:
    """Scrape FSBO.com and store listings as Tier D leads. No enrichment or routing."""
    log.info(f"FSBO agent starting — {county.title()} County, {state}")

    async def _run_async() -> list[dict]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=_USER_AGENT)
            page = await context.new_page()
            try:
                return await _fetch_fsbo_com_async(page, target_county=county)
            except Exception as e:
                log.error(f"FSBO.com scraper failed for {county}: {e}")
                from maintenance.self_healer import handle_failure
                handle_failure(f"fsbo_{county}", str(e))
                return []
            finally:
                await browser.close()

    raw_listings = asyncio.run(_run_async())

    if not raw_listings:
        log.warning(f"FSBO agent: 0 listings for {county} — source may be blocked or no active listings")
        return

    log.info(f"Processing {len(raw_listings)} FSBO listings for {county}")
    new_records = 0

    for raw in raw_listings:
        try:
            # 2. PARSE
            record = parse_fsbo_listing(raw, state)
            if not record.get("property_address"):
                continue

            # 3. DEDUPE — address is the natural key; FSBO has no parcel ID
            if is_duplicate(
                county=record["county"],
                source_type="fsbo",
                parcel_id=None,
                property_address=record.get("property_address"),
                owner_name=record.get("owner_name"),
            ):
                continue

            # 4. STORE — raw_leads (Tier D)
            record["tier"] = "D"
            insert_row("raw_leads", record)
            new_records += 1

            # Also insert into zillow_deals for ARV scoring by Zillow Deal Finder
            _insert_zillow_deals_row(raw, record["county"])

        except Exception as e:
            log.error(f"Error processing FSBO listing for {county}: {e}")
            continue

    log.info(f"FSBO agent complete — {county.title()} County | {new_records} new Tier D records stored")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FSBO agent (Tier D) — FSBO.com Ohio listings",
        epilog=(
            "Source: FSBO.com only. Craigslist dropped (ToS ban + enforcement history). "
            "Listings are stored in raw_leads (Tier D) and zillow_deals for ARV scoring. "
            "If blocked: evaluate Apify actor benthepythondev/fsbo-real-estate-scraper."
        ),
    )
    parser.add_argument("--county", help="County name (cuyahoga | lake | mahoning)")
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
