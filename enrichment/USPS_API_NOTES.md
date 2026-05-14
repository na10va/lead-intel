# USPS Addresses API 3.0 — Integration Notes

**Reference:** https://developers.usps.com/addressesv3  
**Status:** API Access Control initiative launching April 2026 — credentials not yet available.  
**Action required:** Once access launches, register at developers.usps.com and add OAuth credentials to `.env`.

---

## What we know

- **API style:** REST
- **Auth:** OAuth 2.0 client credentials flow
- **Base path:** `/addressesv3`
- **Env vars needed:** `USPS_CLIENT_ID`, `USPS_CLIENT_SECRET` (already in `.env`, left blank)

## Three endpoints

| Endpoint | Purpose |
|---|---|
| Address Standardization | Validates and corrects a domestic address; returns ZIP+4 |
| City/State Lookup | Returns valid city + state for a given ZIP Code |
| ZIP Code Lookup | Returns valid ZIP Code for a given city + state |

## How public_sources.py will use this

In `enrichment/public_sources.py`, the address validation step will:
1. POST the raw `property_address` from a scraped lead to the Address Standardization endpoint
2. Use the standardized address returned by USPS as the canonical address going forward
3. If the address fails validation (non-deliverable), flag `verified_raw = false` and log the rejection
4. Cache the OAuth token and refresh it on expiry — do not re-authenticate on every request

## Token endpoint (expected pattern)

OAuth 2.0 client credentials:
```
POST https://apis.usps.com/oauth2/v3/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id={USPS_CLIENT_ID}
&client_secret={USPS_CLIENT_SECRET}
```

Confirm exact token endpoint URL once API Access Control is live.
