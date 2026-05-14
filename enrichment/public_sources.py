"""
enrichment/public_sources.py — Step 1: Free Ohio public sources.

Tried in order for every new lead before spending any money:
    1. Ohio County Auditor Records (owner mailing address)
    2. USPS Addresses API 3.0 (address standardization — OAuth)
    3. Ohio Secretary of State (for LLC/Trust ownership piercing)

If a confirmed mailing address + owner name is returned, mark enrichment_step=1.
Phone number is NOT available from these free sources — if no mobile found,
waterfall.py escalates to Step 2 (Skip Sherpa).

USPS API: See enrichment/USPS_API_NOTES.md — OAuth credentials pending launch.
"""

import os
import time
from typing import Optional

import requests

from utils.logger import get_logger

log = get_logger("enrichment.public_sources")

COUNTY_AUDITOR_URLS = {
    "Cuyahoga": "https://auditor.cuyahogacounty.us",
    "Lake": "https://www.lakecountyohio.gov/auditor",
    "Mahoning": "https://www.mahoningcountyauditor.org",
}

OHIO_SOS_SEARCH_URL = "https://businesssearch.ohiosos.gov"

# USPS Addresses API 3.0 — OAuth 2.0 client credentials
# Credentials pending: https://developers.usps.com/addressesv3
# See enrichment/USPS_API_NOTES.md for full integration plan
USPS_TOKEN_URL = "https://apis.usps.com/oauth2/v3/token"
USPS_ADDRESS_URL = "https://apis.usps.com/addresses/v3/address"

_usps_token_cache: dict = {}


def _get_usps_token() -> Optional[str]:
    """Fetch a USPS OAuth token, using a cached token if still valid.

    TODO: Activate once USPS API Access Control launches (April 2026).
    Credentials: USPS_CLIENT_ID + USPS_CLIENT_SECRET in .env.
    """
    client_id = os.getenv("USPS_CLIENT_ID")
    client_secret = os.getenv("USPS_CLIENT_SECRET")

    if not client_id or not client_secret:
        log.debug("USPS credentials not configured — skipping USPS validation")
        return None

    # Return cached token if still valid (with 60s buffer)
    if _usps_token_cache.get("token") and _usps_token_cache.get("expires_at", 0) > time.time() + 60:
        return _usps_token_cache["token"]

    # TODO: Implement token fetch once USPS API Access Control is live
    raise NotImplementedError("USPS OAuth token fetch not yet implemented")


def validate_address_usps(address: str) -> Optional[dict]:
    """Validate and standardize an address using USPS Addresses API 3.0.

    Returns a dict with standardized address fields, or None if unavailable.

    TODO: Implement once USPS API Access Control launches.
    Reference: enrichment/USPS_API_NOTES.md
    """
    token = _get_usps_token()
    if not token:
        return None

    raise NotImplementedError("USPS address validation not yet implemented")


def lookup_county_auditor(owner_name: str, property_address: str, county: str) -> dict:
    """Look up owner mailing address from the county auditor property search.

    TODO: Implement per-county auditor scraping.
    Returns dict with owner_mailing_address and any additional owner details.
    These are public portals — no auth required, but add 2s delay between requests.
    """
    raise NotImplementedError(f"County auditor lookup not yet implemented for {county}")


def lookup_ohio_sos(owner_name: str) -> Optional[dict]:
    """Look up registered agent and principal for LLC/Trust owners via Ohio SOS.

    Only called when owner_name contains LLC, Trust, Holdings, Properties, etc.
    Returns dict with principal_name and registered_agent if found, else None.

    TODO: Implement Ohio SOS business search scraping.
    URL: https://businesssearch.ohiosos.gov
    """
    corporate_keywords = ["llc", "trust", "holdings", "properties", "investments", "group", "inc"]
    if not any(kw in owner_name.lower() for kw in corporate_keywords):
        return None

    raise NotImplementedError("Ohio SOS lookup not yet implemented")


def run_public_sources(lead: dict) -> dict:
    """Run all free Ohio public source lookups for a lead.

    Returns a result dict with available enrichment fields and mobile_found flag.
    mobile_found is always False from public sources — they don't have phone numbers.
    """
    owner_name = lead.get("owner_name", "")
    property_address = lead.get("property_address", "")
    county = lead.get("county", "")

    result: dict = {
        "mobile_found": False,
        "equity_unknown": True,
    }

    # County auditor lookup
    try:
        auditor_data = lookup_county_auditor(owner_name, property_address, county)
        result.update(auditor_data)
    except NotImplementedError:
        log.debug(f"County auditor lookup not implemented for {county}")
    except Exception as e:
        log.warning(f"County auditor lookup failed for {county}: {e}")

    # USPS address validation
    try:
        usps_data = validate_address_usps(property_address)
        if usps_data:
            result["property_address_standardized"] = usps_data.get("standardized_address")
    except NotImplementedError:
        log.debug("USPS validation not yet available — credentials pending")
    except Exception as e:
        log.warning(f"USPS validation failed: {e}")

    # Ohio SOS corporate ownership pierce
    try:
        sos_data = lookup_ohio_sos(owner_name)
        if sos_data:
            result["owner_name_pierced"] = sos_data.get("principal_name")
            log.info(f"Ohio SOS pierced corporate owner: {owner_name} → {sos_data.get('principal_name')}")
    except NotImplementedError:
        log.debug("Ohio SOS lookup not yet implemented")
    except Exception as e:
        log.warning(f"Ohio SOS lookup failed: {e}")

    return result
