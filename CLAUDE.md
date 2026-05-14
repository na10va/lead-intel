# CLAUDE.md — US Distressed Property Lead Intelligence System

This file is read by Claude Code at the start of every session. It defines the architecture,
rules, and workflows for an automated real estate lead generation system targeting distressed
properties across all 50 US states.

---

## Project Overview

This system monitors public county and court records daily across all 50 US states to surface
motivated seller leads before competitors. It collects, deduplicates, enriches, scores, and
routes distressed property data automatically — requiring minimal manual intervention.

**Owner:** Direct Home Solutions LLC / The VNA Group
**Target geographies:** All 50 US states (start with Ohio proof of concept — Cuyahoga, Mahoning,
and Lake counties)
**Ultimate goal:** Be the first investor to contact a motivated seller after a triggering event
(probate filing, code violation, lis pendens, tax lien)

---

## Data Priority (Ranked)

**Tier A/B/C — Actively routed to VA and owner alerts:**
1. **Probate filings** — highest intent; executor must liquidate estate assets
2. **Code violations / vacant properties** — owner burden signals high motivation
3. **Foreclosure / Lis Pendens** — pre-foreclosure window is narrow; speed is critical
4. **Tax lien / delinquent** — slower-moving but high volume

**Tier D — Monitored, stored, not actively routed unless stacked with Tier A/B/C signal:**
5. **Divorce filings** — asset liquidation under court order; high intent when combined with property ownership
6. **Eviction filings (landlord-side)** — tired landlord signal; often wants out of the property entirely
7. **Bankruptcy (Chapter 7 & 13)** — federal court-ordered liquidation; structured and reliable data
8. **FSBO listings** — owner avoiding agent fees signals openness to direct conversation

---

## Tech Stack

| Layer | Tool | Why |
|---|---|---|
| Database | Supabase (Postgres) | Free tier, REST API, real-time, easy to query |
| Scraping | Python + Playwright | Handles JavaScript-heavy court portals |
| Scheduling | APScheduler (Python) | Daily cron-style runs, in-process |
| Enrichment | Waterfall model (see Enrichment section) | Free public sources first, Skip Sherpa API for gaps, Skip Matrix manually for Tier A failures |
| Notifications | Twilio (SMS) + SendGrid (email) | Instant alert on new scored leads |
| Orchestration | Claude Code agents | Writes and iterates on all components |
| Language | Python 3.11+ | Primary language for all agents and scripts |
| Config | .env file | All API keys and secrets stored here, never hardcoded |

**Budget ceiling:** $100–$500/month across all paid APIs combined. Log costs monthly.

---

## Repository Structure

```
/
├── CLAUDE.md                  ← This file
├── .env                       ← API keys (never commit this)
├── .env.example               ← Safe template for keys
├── requirements.txt           ← Python dependencies
├── README.md                  ← Human-readable project summary
│
├── agents/
│   ├── probate_agent.py         ← Scrapes probate court filings
│   ├── code_violation_agent.py  ← Scrapes municipal code violation records
│   ├── foreclosure_agent.py     ← Scrapes lis pendens / foreclosure filings
│   ├── tax_lien_agent.py        ← Scrapes tax delinquent records
│   ├── divorce_agent.py         ← Tier D: scrapes divorce filings (county courts)
│   ├── eviction_agent.py        ← Tier D: scrapes landlord-filed eviction records
│   ├── bankruptcy_agent.py      ← Tier D: pulls Chapter 7 & 13 filings via PACER
│   └── fsbo_agent.py            ← Tier D: scrapes FSBO listings (Craigslist, FSBO.com)
│
├── zillow/
│   ├── zillow_scraper.py        ← Scrapes active listings in Cuyahoga, Lake, Mahoning
│   ├── arv_calculator.py        ← Calculates Zestimate ARV + 90-day comp-based ARV
│   ├── deal_scorer.py           ← Scores listings at ≤75% of lower ARV, ranked tightest first
│   └── owner_notify.py          ← Sends deal alerts to owner only (never VA)
│
├── enrichment/
│   ├── waterfall.py           ← Master enrichment controller — runs Step 1→2→3 in order
│   ├── public_sources.py      ← Step 1: Ohio county auditor, USPS, assessor (free)
│   ├── skip_sherpa.py         ← Step 2: Skip Sherpa API for gaps after Step 1
│   └── skip_matrix_flag.py    ← Step 3: Flags Tier A failures for manual Skip Matrix run
│
├── maintenance/
│   ├── self_healer.py         ← Detects broken scrapers, attempts auto-repair via Claude Code
│   ├── monthly_report.py      ← Emails owner on 1st of month: health, costs, source status
│   ├── dependency_check.py    ← Monthly check for outdated Python packages
│   ├── source_revalidator.py  ← Every 30 days, re-confirms all source URLs + data fields
│   └── cost_watchdog.py       ← Pauses pipeline + alerts owner if daily spend exceeds $50
│
├── scoring/
│   └── score.py               ← Scores and ranks leads (see logic below)
│
├── verification/
│   ├── verify_leads.py        ← Cross-checks scraped data against source for accuracy
│   ├── verify_sources.py      ← Confirms each source is live and returning valid data
│   ├── verify_enrichment.py   ← Validates enriched fields (phone format, address, equity)
│   └── daily_report.py        ← Generates and sends the owner's daily verification digest
│
├── routing/
│   ├── notify.py              ← Sends SMS + email alerts
│   └── va_router.py           ← Routes leads to VA queue (Google Sheet or CRM)
│
├── db/
│   ├── schema.sql             ← Supabase table definitions
│   └── client.py             ← Supabase Python client wrapper
│
├── scheduler/
│   └── run_daily.py           ← Master scheduler — runs all agents daily
│
└── utils/
    ├── logger.py              ← Centralized logging
    └── deduper.py             ← Deduplication logic
```

---

## Agent Architecture

Each agent follows this exact pattern — do not deviate:

```
1. FETCH   → Pull new filings from source (county website, court portal, public API)
2. PARSE   → Extract structured fields (name, address, parcel ID, filing date, type)
3. DEDUPE  → Check against Supabase; skip if record already exists
4. STORE   → Insert new records into `raw_leads` table with source + timestamp
5. VERIFY  → Cross-check record accuracy before any enrichment or routing (see Verification section)
6. ENRICH  → Trigger enrichment pipeline (owner phone, estimated value, equity)
7. VERIFY  → Re-validate enriched fields for completeness and format integrity
8. SCORE   → Run scoring model; update `score` and `tier` fields
9. ROUTE   → If score >= threshold, trigger notification + VA routing
```

Agents must be **idempotent** — running twice on the same day must not create duplicate records.

---

## Data Sources by Type

### Probate
- State court online portals (e.g. Ohio: McohioCourts.gov)
- PACER for federal probate matters
- CourtListener (free API for public court data)
- Each county may have its own portal — agent must handle county-by-county variation

### Code Violations / Vacant Properties
- Municipal open data portals (most large cities publish via Socrata or ArcGIS)
- HUD vacant property datasets (federal)
- County assessor "vacant" classification flags

### Foreclosure / Lis Pendens
- County recorder of deeds websites — **checked real-time / same-day, not daily**
- PACER for federal filings
- PropertyRadar API (paid, worth evaluating within budget)
- State-specific legal notice databases (e.g. Ohio Legal News)
- **Monitoring cadence:** Run every 4 hours between 7 AM–7 PM EST. New records trigger
  immediate SMS alert to owner regardless of tier — speed is the edge here.

### Tax Lien / Delinquent

**Cuyahoga County:**
- Downloads annual Delinquent Land Tax PDF from county treasurer website
- Parses 12,573+ records automatically
- Update `CUYAHOGA_PDF_URL` in tax_lien_agent.py each October when new list publishes

**Lake County — Email Request Workflow:**
- Contact: Karen at Lake County Auditor's office
- Process: Send email on the **first Monday of every month** from `info@thevnagroup.com`
- Karen replies with the delinquent list when ready (CSV, Excel, or PDF)
- The scheduler auto-sends this email via SendGrid on the first Monday of each month
- When the file arrives, the pipeline ingests it automatically
- **Deduplication note:** Lake County lists include multiple owners per parcel (co-ownership).
  Always deduplicate on PARCEL field (keep first owner) before ingesting.
  First list received: April 1, 2026 — 1,683 unique properties after dedup.
- Email template stored in `scheduler/lake_county_email.py`

**Mahoning County — Weekly Manual Check:**
- Source: https://auditor.mahoningcountyoh.gov/
- Cadence: Every Monday at 7 AM EST — automated Playwright scraper checks for updated list
- If list is available: download and ingest automatically
- If site structure changes or download fails: self-healer flags and SMS owner
- Contact for bulk CSV: Mahoning County Auditor 330-740-2010

**IRS Federal Tax Liens:**
- No public API — filed at county recorders
- Skip for Ohio POC — county-level data covers the use case

### Divorce Filings (Tier D)
- Ohio county common pleas court portals (domestic relations division)
- CourtListener API for indexed cases
- Cross-reference property ownership records to confirm subject owns real estate in target counties

### Eviction Filings — Landlord-Side Only (Tier D)
- Ohio municipal court portals (each county has its own)
- Filter for plaintiff = landlord, defendant = tenant (not the reverse)
- Flag properties where the same landlord has filed 2+ evictions — strong tired landlord signal

### Bankruptcy Chapter 7 & 13 (Tier D)
- PACER federal court API (most reliable structured source)
- Filter for Ohio Northern District (covers Cuyahoga, Lake, Mahoning)
- Cross-reference filer address against target county property records

### FSBO Listings (Tier D)
- FSBO.com Ohio listings
- Craigslist housing > for sale by owner (Akron/Cleveland/Youngstown markets)
- Zillow FSBO filter (separate from the Zillow Deal Finder module below)
- Note: FSBO agent only stores listings — it does NOT score or route. All FSBO leads
  flow into the Zillow Deal Finder module for ARV-based scoring instead.

**When a source blocks scraping:** Log the block, flag the source in Supabase as `blocked=true`,
and alert the owner. Do not retry more than 3 times per day on a blocked source.

---

## Database Schema (Supabase)

### `raw_leads` table

| Column | Type | Notes |
|---|---|---|
| id | uuid | Primary key, auto-generated |
| source_type | text | probate / code_violation / foreclosure / tax_lien / divorce / eviction / bankruptcy / fsbo |
| source_name | text | e.g. "Cuyahoga County Probate Court" |
| state | text | 2-letter state code |
| county | text | County name |
| owner_name | text | Property owner or decedent name |
| property_address | text | Full street address |
| parcel_id | text | County assessor parcel ID |
| filing_date | date | Date of the triggering event |
| raw_data | jsonb | Full scraped record, unparsed |
| created_at | timestamp | When we ingested it |
| enriched | boolean | Whether enrichment has run |
| score | integer | Lead score 0–100 |
| tier | text | A / B / C |
| routed_to_va | boolean | Whether sent to VA queue |
| alerted | boolean | Whether SMS/email sent |
| verified_raw | boolean | Passed Gate 1 post-scrape verification |
| verified_enriched | boolean | Passed Gate 2 post-enrichment verification |
| verification_notes | text | Reason for failure if either gate rejected the record |

### `sources` table
Tracks each data source, its URL, last scraped timestamp, success/fail status, and block flag.

---

## Verification Layer

Verification runs **twice per lead** — once after scraping, once after enrichment. It also runs
once per day as a source-health check independent of individual leads. Nothing gets routed to
the VA or triggers a notification unless it has passed both verification gates.

---

### Gate 1 — Post-Scrape Record Verification (`verify_leads.py`)

After a record is scraped and stored, verify it before enrichment begins.

**Required fields check** — reject the record and log a warning if any of these are missing:
- `owner_name` — must be non-empty string
- `property_address` — must contain a street number and street name at minimum
- `state` — must be a valid 2-letter US state code
- `county` — must be non-empty string
- `filing_date` — must be a valid date, not in the future, not older than 180 days
- `source_type` — must be one of: probate / code_violation / foreclosure / tax_lien

**Cross-reference check** — for each new record, the agent must:
1. Re-fetch the source URL or case number and confirm the filing still exists
2. Confirm the property address is a real, parseable US address (use `usaddress` Python library)
3. Confirm the parcel ID format matches the known format for that county (if available)

**Duplicate signal check** — flag but do not reject if:
- Same owner name appears more than once across different source types (this is a stacked signal — good)
- Same address appears in 2+ source types within 30 days (also a stacked signal — boost score)

**Verification status field:** Add `verified_raw` (boolean) to `raw_leads` table.
Set `verified_raw=false` on any record that fails a required field or cross-reference check.
Failed records are stored but never enriched, scored, or routed.

---

### Gate 2 — Post-Enrichment Verification (`verify_enrichment.py`)

After BatchLeads enrichment runs, validate the returned data before scoring.

**Phone number validation:**
- Must match format `+1XXXXXXXXXX` (E.164)
- Must pass a basic carrier lookup or at minimum a Twilio Lookup API check
- Flag disconnected or landline-only numbers; prefer mobile

**Address validation:**
- Owner mailing address (if different from property) must be parseable
- If mailing address is out-of-state, confirm and set `owner_out_of_state=true` flag

**Financial data validation:**
- Estimated property value must be > $10,000 (filter out obvious errors)
- Estimated equity percentage must be between 0–100%
- If equity data is missing, set `equity_unknown=true` — do not assume 0

**Verification status field:** Add `verified_enriched` (boolean) to `raw_leads` table.
Only records where `verified_enriched=true` proceed to scoring and routing.

---

### Gate 3 — Daily Source Health Check (`verify_sources.py`)

Runs independently at **6:45 AM EST**, 15 minutes before the main pipeline.

For every active source in the `sources` table:
1. Send a test request to the source URL
2. Confirm HTTP 200 response (or expected redirect)
3. Confirm the expected HTML element or data field is present in the response
4. Log `last_checked`, `status` (healthy / degraded / blocked), and response time

**If a source is degraded or blocked:**
- Flag it in the `sources` table immediately
- Send the owner an SMS alert: `"[SOURCE ALERT] {source_name} is not responding. Leads from
  this source paused until resolved."`
- Skip that source in today's pipeline run — do not process partial/unreliable data

**If more than 2 sources are down simultaneously:**
- Send an elevated alert and pause the full pipeline until manually restarted

---

### Daily Verification Digest (`daily_report.py`)

Every day at **8:00 AM EST** (after the pipeline completes), send the owner an email summarizing:

| Item | Detail |
|---|---|
| Sources checked | How many sources ran today |
| Sources healthy | Count of sources that passed health check |
| Sources degraded/blocked | Count + names of problem sources |
| Raw records scraped | Total new records pulled today |
| Records failed Gate 1 | Count that failed post-scrape verification |
| Records failed Gate 2 | Count that failed post-enrichment verification |
| Records passed both gates | Count of clean, verified leads |
| Tier A leads routed | Count sent to VA + alerted by SMS |
| Tier B leads routed | Count sent to VA by email |
| Tier C leads stored | Count stored for weekly review |
| Tier D leads stored | Count by type (divorce / eviction / bankruptcy / FSBO) |
| Tier D stacked upgrades | Count of Tier D leads that boosted a primary lead's tier |
| Zillow deals found | Count of listings at ≤75% ARV surfaced to owner today |
| Estimated API cost today | BatchLeads + Twilio + SendGrid spend |

This digest is the owner's daily proof that the system ran, what it found, and that the data
being acted on has been verified — not just scraped.

---

## Lead Scoring Logic — Two-Axis Model

Every lead receives two independent scores that combine into a final score out of 100.
**Both axes must score well to reach Tier A.** A highly distressed property with no deal
economics still surfaces — but routes as Tier B or C, not Tier A.

---

### Axis 1 — Distress Score (0–50)
How motivated is the seller?

| Signal | Points |
|---|---|
| Probate filing present | +15 |
| Code violation present | +10 |
| Foreclosure / lis pendens present | +10 |
| Tax delinquent present | +8 |
| Vacant property flag | +5 |
| Owner is out-of-state | +5 |
| Multiple signals stacked (2+) | +5 bonus |
| Filing date within last 30 days | +3 freshness bonus |
| Divorce filing present (Tier D stack) | +4 bonus |
| Eviction filing present (Tier D stack) | +4 bonus |
| Bankruptcy filing present (Tier D stack) | +4 bonus |

**Tier D signals alone max out at 12 points and never trigger routing on their own.**
They only add value when stacked with a Tier A/B/C signal already present.

---

### Axis 2 — Deal Score (0–50)
How good is the economics?

| Signal | Points |
|---|---|
| Estimated equity > 50% | +25 |
| Estimated equity 30–50% | +20 |
| Estimated equity 15–30% | +12 |
| Estimated equity < 15% | +5 |
| Equity unknown | +8 (neutral — do not penalize missing data) |
| Last sale > 10 years ago | +10 (long hold = more equity likely) |
| Last sale 5–10 years ago | +6 |
| Property value $75K–$300K (Ohio sweet spot) | +10 |
| Property value $300K–$500K | +5 |
| Property value < $75K or > $500K | +0 |

---

### Combined Score and Tiers

| Tier | Score | Action |
|---|---|---|
| **Tier A** | 70–100 | SMS + email to owner. Route to VA same day. |
| **Tier B** | 45–69 | Email to owner. Route to VA within 48 hours. |
| **Tier C** | 20–44 | Store in database. Review weekly. No active routing. |
| **Tier D** | 0–19 | Store only. Never routed. Surfaced in weekly digest. |

**Tier D leads (divorce/eviction/bankruptcy/FSBO) are stored and visible in the database
but never trigger notifications or VA routing on their own. They only upgrade tiers when
stacked with a Tier A/B/C signal from the primary data types.**

---

## Notification Rules

- **SMS (Twilio):** Tier A only. Message format:
  `"[TIER A LEAD] {owner_name} | {address} | {county}, {state} | Score: {score} | Source: {source_type}"`
- **Email (SendGrid):** Tier A + B. Include full enriched record.
- **VA Queue:** Write Tier A + B leads to a designated Google Sheet tab or GHL sub-account.
  Include: name, address, phone numbers (skip traced), score, tier, source, filing date.

---

## Enrichment Rules — Waterfall Model

Every lead passes through enrichment in strict order. The pipeline escalates to the next
step only when the previous step fails to return a verified mobile phone number.
**Never skip steps. Never run Step 2 or 3 if Step 1 already returned a valid mobile.**

---

### Step 1 — Free Ohio Public Sources (`public_sources.py`)

Try these in order for every new lead before spending any money:

**Ohio County Auditor Records**
- Pull owner mailing address from the county auditor's property search
- Available for all 3 Ohio counties via their public web portals
- Cuyahoga: auditor.cuyahogacounty.us
- Lake: lakecountyohio.gov/auditor
- Mahoning: mahoningcountyauditor.org

**USPS Address Validation**
- Validate and standardize the property address using USPS free API
- Confirms deliverability and corrects formatting before skip tracing
- Endpoint: https://secure.shippingapis.com/ShippingAPI.dll

**Ohio Secretary of State (for LLCs / Trusts)**
- If owner name contains "LLC", "Trust", "Holdings", or similar — look up the registered
  agent and principal name via Ohio SOS business search (free, public)
- This pierces corporate ownership before paying for skip tracing

**Result:** If Step 1 returns a confirmed mailing address + owner name, mark
`enrichment_step = 1` and proceed to scoring. If no phone number found, escalate to Step 2.

---

### Step 2 — Skip Sherpa API (`skip_sherpa.py`)

Triggered only when Step 1 did not return a verified mobile phone number.

- Send owner name + property address to Skip Sherpa API
- Request up to 3 phone numbers (mobile preferred) + email address
- Log API call and cost to `api_costs` table
- Rate limit: maximum 500 calls per day to stay within budget
- If Skip Sherpa returns a valid mobile: mark `enrichment_step = 2`, proceed to scoring
- If Skip Sherpa returns no mobile or only landlines: escalate to Step 3 flag

**Skip Sherpa API key:** Store as `SKIP_SHERPA_API_KEY` in `.env`

---

### Step 3 — Skip Matrix Manual Flag (`skip_matrix_flag.py`)

Triggered only for Tier A leads where Steps 1 and 2 both failed to return a mobile number.

- Do NOT call Skip Matrix API automatically — it is a manual service
- Flag the record in Supabase: `skip_matrix_needed = true`
- Add the record to a dedicated `skip_matrix_queue` Google Sheet tab
- Send owner a weekly email every Monday at 7 AM: "X Tier A leads need Skip Matrix —
  download the list here" with a direct link to the queue sheet
- Owner manually runs the list through Skip Matrix dashboard and uploads results back

**This step is intentionally manual.** Skip Matrix's premium data quality justifies the
extra step for the highest-value leads only.

---

### Enrichment Cost Tracking

Log every Step 2 API call to the `api_costs` table:

| Column | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| service | text | skip_sherpa / twilio / sendgrid |
| lead_id | uuid | Foreign key to raw_leads |
| cost_usd | float | Cost of this specific call |
| called_at | timestamp | When the API was called |
| result | text | success / no_mobile / failed |

Review monthly in the maintenance report. If Skip Sherpa spend exceeds $200/month,
review whether Step 1 coverage can be improved to reduce Step 2 volume.

---

## Scheduling

| Time (EST) | Step |
|---|---|
| 6:45 AM | Source health check (`verify_sources.py`) — abort degraded sources |
| 7:00 AM | Run all 8 agents in parallel — Tier A/B/C + Tier D |
| 7:15 AM | Gate 1 verification — post-scrape record validation |
| 7:20 AM | Enrichment — BatchLeads skip trace on all Gate 1 passing records (Tier A/B/C only) |
| 7:35 AM | Gate 2 verification — post-enrichment field validation |
| 7:40 AM | Scoring — two-axis score and tier all Gate 2 passing records |
| 7:45 AM | Routing — SMS + email + VA queue for Tier A and B |
| 7:50 AM | Zillow Deal Finder run — scrape, ARV calc, score, owner alert |
| 8:00 AM | Daily verification digest email sent to owner |
| Every 4 hrs (7AM–7PM) | Foreclosure/lis pendens real-time check — immediate SMS on new filing |

If any step fails, log the error, skip that step, and continue to the next where possible.
The daily digest always sends — even if the pipeline partially failed.

---

## Automated Maintenance

The system maintains itself. No manual intervention required unless the system explicitly
alerts the owner. All maintenance runs silently in the background on its own schedule.

---

### 1. Self-Healing Scrapers (`maintenance/self_healer.py`)

**Trigger:** Any scraper that returns 0 records for 2 consecutive runs, or throws an
unhandled exception during the FETCH or PARSE step.

**Auto-repair sequence:**
1. Log the failure with full error details and the last known working selector
2. Re-fetch the source URL and compare current HTML structure to the last known structure
3. Attempt to identify the new selector automatically using Claude Code's code editing tools
4. If a fix is found: apply it, re-run the agent, and log the self-repair in `maintenance_log`
5. If no fix found within 15 minutes: send owner SMS alert with the specific scraper name
   and error message. Flag source as `needs_manual_review = true` in the `sources` table.

**The goal:** Most scraper breaks are minor selector changes. The self-healer fixes these
without the owner ever knowing. Only escalates when it genuinely can't resolve the issue.

---

### 2. Monthly Health Report (`maintenance/monthly_report.py`)

**Schedule:** 1st of every month at 8:00 AM EST.

Email the owner a full system health summary covering:

| Item | Detail |
|---|---|
| Pipeline uptime | % of days the pipeline ran successfully last month |
| Sources healthy | Count of sources that never broke |
| Sources self-healed | Count of sources that broke and auto-repaired |
| Sources needing attention | Count still flagged for manual review |
| Total leads ingested | All tiers combined for the month |
| Tier A leads | Count + how many converted to VA outreach |
| Skip Matrix queue | Count of leads still waiting for manual enrichment |
| Total API spend | Skip Sherpa + Twilio + SendGrid for the month |
| Cost per verified lead | Total spend / total verified leads |
| Outdated dependencies | Any Python packages flagged by dependency check |

---

### 3. Dependency Update Check (`maintenance/dependency_check.py`)

**Schedule:** 1st of every month, runs before the monthly health report.

- Run `pip list --outdated` and log results
- Flag any package with a known security vulnerability (cross-reference PyPI advisories)
- Include flagged packages in the monthly health report
- **Never auto-update dependencies.** Only flag them. Owner approves updates manually
  to avoid breaking changes in production.

---

### 4. Proactive Source Re-Validation (`maintenance/source_revalidator.py`)

**Schedule:** Every 30 days, independent of the daily health check.

For every source in the `sources` table — even healthy ones:
1. Fetch the source URL fresh
2. Confirm the expected data fields are still present and in the same format
3. Confirm the county portal hasn't added a new login wall or CAPTCHA
4. Log validation result and timestamp to `source_validation_log` table
5. If structure has changed but data is still accessible: flag for self-healer review
6. If source now requires authentication: alert owner immediately

**Why this matters:** County websites don't announce changes. A source can silently
start returning incomplete data without throwing an error. This catches silent degradation
before it affects lead quality.

---

### 5. Cost Watchdog (`maintenance/cost_watchdog.py`)

**Runs:** After every enrichment batch (multiple times daily).

- Sum all API costs logged to `api_costs` for the current calendar day
- If total exceeds **$50 in a single day**: immediately pause the enrichment pipeline
  and send owner SMS: `"[COST ALERT] Daily API spend hit $50. Pipeline paused. Review
  api_costs table and restart manually with: python scheduler/run_daily.py --resume"`
- If total exceeds **$200 in a single month**: send email warning but do not pause
- Log all watchdog checks to `cost_watchdog_log` table

**Why $50:** A runaway loop or duplicate enrichment bug could drain hundreds of dollars
in minutes. $50 is a safe ceiling that stops the bleeding before it becomes a real problem.

---

### Maintenance Log Table (`maintenance_log`)

All automated maintenance events are written to a dedicated Supabase table:

| Column | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| event_type | text | self_heal / revalidation / cost_alert / dependency_flag |
| source_name | text | Which source or component was affected |
| description | text | What happened and what action was taken |
| resolved | boolean | Whether the issue was auto-resolved or needs attention |
| created_at | timestamp | When the event occurred |

---

- **Never hardcode API keys.** Always read from `.env` using `python-dotenv`.
- **Never skip deduplication.** Every agent must check before inserting.
- **Log everything.** Use `utils/logger.py` for all agent activity — success, failure, record count.
- **Fail gracefully.** If one agent crashes, others must continue running.
- **Write tests for scoring logic.** The scoring function must have unit tests in `/tests/`.
- **Start with Ohio POC.** Before building national coverage, prove the pipeline works on
  Cuyahoga, Mahoning, and Lake counties in Ohio. Then expand state by state.
- **Comment all scraping logic.** Court portal HTML structures change — comments help future
  agents understand what each selector targets and why.

---

## Expansion Roadmap (Do Not Build Yet)

The following are future phases. Do not build until the Ohio POC is stable:

- National county coverage (automated discovery of county portal URLs)
- PropStream or ATTOM API integration as a paid data supplement
- GoHighLevel CRM integration for VA workflow automation
- Automated skip trace + ringless voicemail drop on Tier A leads
- Weekly summary dashboard (Supabase + simple HTML report)
- **Human intelligence / referral intake form** — a simple web form for estate attorneys,
  probate paralegals, agents, and network contacts to submit pre-filing tips directly into
  the pipeline. Requires building a referral network first (REIA meetups, attorney outreach).
  Tag these leads `source_type = referral` and fast-track to scoring immediately.

---

## Key Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run a single agent manually (for testing)
python agents/probate_agent.py --county cuyahoga --state OH

# Run full daily pipeline manually
python scheduler/run_daily.py

# Run scoring on all unscored leads
python scoring/score.py

# Run tests
pytest tests/

# Check Supabase connection
python db/client.py --test

# Run source health check manually
python verification/verify_sources.py

# Run Gate 1 verification on all unverified raw leads
python verification/verify_leads.py

# Run Gate 2 verification on all unenriched leads
python verification/verify_enrichment.py

# Send daily digest manually (e.g. to test formatting)
python verification/daily_report.py --test

# Run Tier D agents manually
python agents/divorce_agent.py --county cuyahoga --state OH
python agents/eviction_agent.py --county cuyahoga --state OH
python agents/bankruptcy_agent.py --district ohio_northern
python agents/fsbo_agent.py --county cuyahoga --state OH

# Run Zillow Deal Finder manually
python zillow/zillow_scraper.py

# Run maintenance scripts manually
python maintenance/self_healer.py --source probate_cuyahoga
python maintenance/monthly_report.py --test
python maintenance/dependency_check.py
python maintenance/source_revalidator.py
python maintenance/cost_watchdog.py --check
```

---

## Zillow Deal Finder — Owner Use Only

**This module is separate from the main lead pipeline. Leads found here go to the owner
directly for personal outreach. The VA never sees or acts on these leads.**

---

### Purpose

Scrape active MLS and FSBO listings in Cuyahoga, Lake, and Mahoning counties, calculate
ARV two ways, and surface any listing priced at or below 75% of ARV — ranked tightest to
loosest. Owner reviews the list daily and makes calls personally.

---

### Geography

- Cuyahoga County, OH
- Lake County, OH
- Mahoning County, OH

Do not expand beyond these three counties until the owner explicitly requests it.

---

### ARV Calculation — Two Methods

**Method 1 — Zestimate ARV:**
Pull Zillow's Zestimate for the subject property. Fast but sometimes inaccurate. Used as
a quick reference column, not the primary filter.

**Method 2 — Comp-Based ARV:**
Pull the last 90 days of sold listings in the same zip code with similar characteristics:
- Bedroom count within ±1
- Bathroom count within ±1
- Square footage within ±20%
- Same property type (single family, multi-family, etc.)

Calculate the median price per square foot of comps, multiply by subject property square
footage. This is the primary ARV used for scoring.

**When the two methods diverge by more than 15%:** Flag the listing with a `⚠️ ARV
Conflict` label in the output. Owner should manually review before calling.

---

### Scoring and Filtering

| Price as % of Comp-Based ARV | Label | Priority |
|---|---|---|
| ≤ 65% | 🔥 Deep Value | Call today |
| 66–70% | ✅ On Target | Call today |
| 71–75% | 👀 Worth a Look | Call this week |
| > 75% | — | Do not surface |

Sort output by ascending % of ARV (best deals first).

**No hard cutoff rule.** The 70% rule is a guide, not a filter. Surface everything ≤75%
and let the owner decide. Negotiation handles the gap.

---

### Output Format

Deliver a daily email to the owner at **7:50 AM EST** with a clean table:

| Address | List Price | Zestimate ARV | Comp ARV | % of Comp ARV | Beds | Baths | SqFt | Days on Market | Listing Type | Label |
|---|---|---|---|---|---|---|---|---|---|---|
| 123 Elm St, Cleveland | $85,000 | $128,000 | $132,000 | 64% | 3 | 1 | 1,100 | 4 | Agent | 🔥 Deep Value |

Include a direct Zillow link for each listing.

---

### Data Storage

Store all Zillow Deal Finder results in a separate Supabase table: `zillow_deals`.
Do not mix with `raw_leads`. Schema:

| Column | Type | Notes |
|---|---|---|
| id | uuid | Primary key |
| address | text | Full property address |
| county | text | Cuyahoga / Lake / Mahoning |
| list_price | integer | Current listing price |
| zestimate_arv | integer | Zillow Zestimate |
| comp_arv | integer | 90-day comp-based ARV |
| pct_of_comp_arv | float | list_price / comp_arv |
| beds | integer | Bedroom count |
| baths | float | Bathroom count |
| sqft | integer | Square footage |
| days_on_market | integer | Days listed |
| listing_type | text | agent / fsbo |
| arv_conflict | boolean | True if Zestimate and comp ARV diverge > 15% |
| label | text | Deep Value / On Target / Worth a Look |
| zillow_url | text | Direct link to listing |
| alerted_owner | boolean | Whether included in daily email |
| created_at | timestamp | When scraped |

---

### Rules

- **Never route Zillow deals to the VA.** Owner calls only.
- **Never enrich Zillow leads** with Skip Sherpa — listing data already contains contact info.
- **Do not score against the distress scoring model.** This is a separate scoring system.
- Zillow's terms of service restrict automated scraping. Use a respectful crawl rate
  (minimum 3–5 second delay between requests) and rotate user agents. If Zillow blocks
  the scraper, evaluate Zillow's official API or a third-party data provider (e.g. BrightData,
  RentCast) as an alternative within budget.

---

# Run maintenance scripts manually
python maintenance/self_healer.py --source probate_cuyahoga
python maintenance/monthly_report.py --test
python maintenance/dependency_check.py
python maintenance/source_revalidator.py
python maintenance/cost_watchdog.py --check
```

---

## Warnings

- Court websites are fragile. Selectors break without notice. The self-healer handles most
  breaks automatically — but always review the maintenance log weekly.
- Some Ohio county probate portals require a CAPTCHA workaround — research each portal before
  assuming it's freely scrapable.
- Skip Sherpa API charges per record. The cost watchdog pauses enrichment if daily spend
  hits $50 — never disable the watchdog.
- Do not contact any property owner directly from this system. Routing to VA is the final step.
  All outreach is handled by humans.
- Never run Skip Matrix through the automated pipeline. It is a manual-only service reserved
  for Tier A leads that Steps 1 and 2 could not enrich.
