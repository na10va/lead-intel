-- =============================================================================
-- Migration 001 — Add geocoding and map columns to raw_leads
-- Run once against your Supabase project (SQL Editor).
-- Safe to re-run: all statements use ADD COLUMN IF NOT EXISTS.
-- =============================================================================

ALTER TABLE raw_leads
    ADD COLUMN IF NOT EXISTS geocoded_address  TEXT,           -- Standardized address from Google Maps Geocoding API
    ADD COLUMN IF NOT EXISTS geocoded_lat      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS geocoded_lng      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS street_view_url   TEXT,           -- Google Street View Static API thumbnail URL
    ADD COLUMN IF NOT EXISTS maps_url          TEXT;           -- Google Maps search hyperlink
