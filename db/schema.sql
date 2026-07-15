-- =============================================================================
-- Lead Intelligence System — Supabase Schema
-- Owner: Direct Home Solutions LLC / The VNA Group
-- Run this file once against your Supabase project to initialize all tables.
-- Safe to re-run: all statements use CREATE TABLE IF NOT EXISTS.
-- =============================================================================

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- =============================================================================
-- TABLE: raw_leads
-- Every distressed property lead scraped from any source.
-- Nothing leaves this table to scoring or routing unless both verification
-- gates have passed (verified_raw = true AND verified_enriched = true).
-- =============================================================================
CREATE TABLE IF NOT EXISTS raw_leads (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Source metadata
    source_type         TEXT        NOT NULL
                            CHECK (source_type IN (
                                'probate', 'code_violation', 'foreclosure',
                                'tax_lien', 'divorce', 'eviction',
                                'bankruptcy', 'fsbo', 'referral'
                            )),
    source_name         TEXT        NOT NULL,           -- e.g. "Cuyahoga County Probate Court"
    state               TEXT        NOT NULL,           -- 2-letter state code, e.g. "OH"
    county              TEXT        NOT NULL,

    -- Lead identity
    owner_name          TEXT,
    property_address    TEXT,
    parcel_id           TEXT,
    filing_date         DATE,
    raw_data            JSONB,                          -- Full scraped record, unparsed

    -- Enrichment (waterfall model)
    enriched            BOOLEAN     NOT NULL DEFAULT FALSE,
    enrichment_step     INTEGER,                        -- 1 = public sources, 2 = Skip Sherpa, 3 = Skip Matrix flagged
    phone_1             TEXT,
    phone_1_dnc         BOOLEAN,                            -- TRUE = on DNC registry
    phone_2             TEXT,
    phone_2_dnc         BOOLEAN,
    phone_3             TEXT,
    litigator           BOOLEAN     NOT NULL DEFAULT FALSE, -- TRUE = known TCPA litigant, never call
    owner_email         TEXT,
    owner_mailing_address TEXT,
    owner_out_of_state  BOOLEAN     NOT NULL DEFAULT FALSE,
    equity_unknown      BOOLEAN     NOT NULL DEFAULT FALSE,
    estimated_value     INTEGER,                        -- in USD
    estimated_equity_pct FLOAT,                        -- 0.0–100.0
    last_sale_date      DATE,
    last_sale_price     INTEGER,                        -- in USD
    skip_matrix_needed  BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Verification — Gate 1 (post-scrape)
    verified_raw        BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Verification — Gate 2 (post-enrichment)
    verified_enriched   BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Combined verification notes
    verification_notes  TEXT,

    -- Scoring
    distress_score      INTEGER,                        -- Axis 1: 0–50
    deal_score          INTEGER,                        -- Axis 2: 0–50
    score               INTEGER,                        -- Combined: 0–100
    tier                TEXT        CHECK (tier IN ('A', 'B', 'C', 'D')),

    -- Routing
    routed_to_va        BOOLEAN     NOT NULL DEFAULT FALSE,
    alerted             BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Timestamps
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enriched_at         TIMESTAMPTZ,
    scored_at           TIMESTAMPTZ,
    routed_at           TIMESTAMPTZ
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_raw_leads_source_type    ON raw_leads (source_type);
CREATE INDEX IF NOT EXISTS idx_raw_leads_county_state   ON raw_leads (county, state);
CREATE INDEX IF NOT EXISTS idx_raw_leads_filing_date    ON raw_leads (filing_date DESC);
CREATE INDEX IF NOT EXISTS idx_raw_leads_tier           ON raw_leads (tier);
CREATE INDEX IF NOT EXISTS idx_raw_leads_score          ON raw_leads (score DESC);
CREATE INDEX IF NOT EXISTS idx_raw_leads_verified       ON raw_leads (verified_raw, verified_enriched);
CREATE INDEX IF NOT EXISTS idx_raw_leads_created_at     ON raw_leads (created_at DESC);
-- Parcel ID + county uniqueness — prevents duplicate records for same property
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_leads_parcel_county_source
    ON raw_leads (parcel_id, county, source_type)
    WHERE parcel_id IS NOT NULL;


-- =============================================================================
-- TABLE: sources
-- Every data source the pipeline scrapes. The daily health check and monthly
-- re-validator update rows here. Agents read this table to know what to scrape.
-- =============================================================================
CREATE TABLE IF NOT EXISTS sources (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Identity
    source_name             TEXT        NOT NULL UNIQUE,  -- e.g. "Cuyahoga County Probate Court"
    source_type             TEXT        NOT NULL
                                CHECK (source_type IN (
                                    'probate', 'code_violation', 'foreclosure',
                                    'tax_lien', 'divorce', 'eviction',
                                    'bankruptcy', 'fsbo'
                                )),
    state                   TEXT        NOT NULL,
    county                  TEXT,
    url                     TEXT        NOT NULL,

    -- Health status
    status                  TEXT        NOT NULL DEFAULT 'healthy'
                                CHECK (status IN ('healthy', 'degraded', 'blocked')),
    blocked                 BOOLEAN     NOT NULL DEFAULT FALSE,
    needs_manual_review     BOOLEAN     NOT NULL DEFAULT FALSE,
    retry_count_today       INTEGER     NOT NULL DEFAULT 0,

    -- Scraping metadata
    expected_element        TEXT,       -- CSS selector or field name used in health check
    last_scraped_at         TIMESTAMPTZ,
    last_checked_at         TIMESTAMPTZ,
    last_healthy_at         TIMESTAMPTZ,
    response_time_ms        INTEGER,

    -- Revalidation
    last_revalidated_at     TIMESTAMPTZ,
    structure_changed       BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Timestamps
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sources_source_type  ON sources (source_type);
CREATE INDEX IF NOT EXISTS idx_sources_status       ON sources (status);
CREATE INDEX IF NOT EXISTS idx_sources_county_state ON sources (county, state);


-- =============================================================================
-- TABLE: zillow_deals
-- Zillow Deal Finder results. Completely separate from raw_leads.
-- Owner-only — never routed to VA. Never enriched via Skip Sherpa.
-- =============================================================================
CREATE TABLE IF NOT EXISTS zillow_deals (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Property details
    address         TEXT        NOT NULL,
    county          TEXT        NOT NULL
                        CHECK (county IN ('Cuyahoga', 'Lake', 'Mahoning')),
    list_price      INTEGER     NOT NULL,               -- in USD
    zestimate_arv   INTEGER,                            -- Zillow Zestimate
    comp_arv        INTEGER,                            -- 90-day comp-based ARV
    pct_of_comp_arv FLOAT,                              -- list_price / comp_arv * 100
    beds            INTEGER,
    baths           FLOAT,
    sqft            INTEGER,
    days_on_market  INTEGER,
    listing_type    TEXT        CHECK (listing_type IN ('agent', 'fsbo')),

    -- Scoring
    arv_conflict    BOOLEAN     NOT NULL DEFAULT FALSE, -- TRUE if Zestimate vs comp ARV diverge > 15%
    label           TEXT        CHECK (label IN ('Deep Value', 'On Target', 'Worth a Look')),

    -- Links and alerts
    zillow_url      TEXT,
    alerted_owner   BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Timestamps
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_zillow_deals_county          ON zillow_deals (county);
CREATE INDEX IF NOT EXISTS idx_zillow_deals_pct_of_comp_arv ON zillow_deals (pct_of_comp_arv ASC);
CREATE INDEX IF NOT EXISTS idx_zillow_deals_created_at      ON zillow_deals (created_at DESC);
-- Note: same-day deduplication for zillow_deals is handled in zillow_scraper.py, not enforced here.


-- =============================================================================
-- TABLE: api_costs
-- Per-call cost log for every paid API: Skip Sherpa, Twilio, SendGrid.
-- The cost watchdog reads this table to enforce the $50/day ceiling.
-- =============================================================================
CREATE TABLE IF NOT EXISTS api_costs (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    service     TEXT        NOT NULL
                    CHECK (service IN ('skip_sherpa', 'tracerfy', 'twilio', 'sendgrid')),
    lead_id     UUID        REFERENCES raw_leads (id) ON DELETE SET NULL,
    cost_usd    FLOAT       NOT NULL,
    called_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result      TEXT        NOT NULL
                    CHECK (result IN ('success', 'no_mobile', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_api_costs_service    ON api_costs (service);
CREATE INDEX IF NOT EXISTS idx_api_costs_called_at  ON api_costs (called_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_costs_lead_id    ON api_costs (lead_id);
-- cost_watchdog queries today's spend using: WHERE called_at >= CURRENT_DATE


-- =============================================================================
-- TABLE: maintenance_log
-- All automated maintenance events: self-heals, revalidations, cost alerts,
-- dependency flags. Written by maintenance/ scripts, read by monthly_report.py.
-- =============================================================================
CREATE TABLE IF NOT EXISTS maintenance_log (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type  TEXT        NOT NULL
                    CHECK (event_type IN (
                        'self_heal', 'revalidation', 'cost_alert', 'dependency_flag',
                        'source_blocked', 'pipeline_paused', 'pipeline_resumed'
                    )),
    source_name TEXT,                   -- Which source or component was affected (nullable)
    description TEXT        NOT NULL,   -- What happened and what action was taken
    resolved    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_maintenance_log_event_type  ON maintenance_log (event_type);
CREATE INDEX IF NOT EXISTS idx_maintenance_log_resolved    ON maintenance_log (resolved);
CREATE INDEX IF NOT EXISTS idx_maintenance_log_created_at  ON maintenance_log (created_at DESC);


-- =============================================================================
-- TABLE: source_validation_log
-- Written by source_revalidator.py every 30 days for every source — even healthy
-- ones. Tracks structural changes and silent degradation over time.
-- =============================================================================
CREATE TABLE IF NOT EXISTS source_validation_log (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           UUID        NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    source_name         TEXT        NOT NULL,
    status              TEXT        NOT NULL
                            CHECK (status IN ('healthy', 'degraded', 'blocked', 'auth_required')),
    structure_changed   BOOLEAN     NOT NULL DEFAULT FALSE,
    response_time_ms    INTEGER,
    notes               TEXT,
    validated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_validation_log_source_id    ON source_validation_log (source_id);
CREATE INDEX IF NOT EXISTS idx_source_validation_log_validated_at ON source_validation_log (validated_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_validation_log_status       ON source_validation_log (status);


-- =============================================================================
-- TABLE: cost_watchdog_log
-- Every time cost_watchdog.py runs, it logs the result here.
-- Provides an audit trail of daily spend and any pauses triggered.
-- =============================================================================
CREATE TABLE IF NOT EXISTS cost_watchdog_log (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    daily_spend_usd FLOAT       NOT NULL,
    monthly_spend_usd FLOAT     NOT NULL,
    threshold_hit   TEXT        CHECK (threshold_hit IN ('daily_50', 'monthly_200')),
    action_taken    TEXT        CHECK (action_taken IN ('none', 'pipeline_paused', 'alert_sent')),
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cost_watchdog_log_checked_at ON cost_watchdog_log (checked_at DESC);
