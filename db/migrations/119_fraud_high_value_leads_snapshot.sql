-- ============================================================================
-- Migration: 119_fraud_high_value_leads_snapshot
--
-- FRAUD-F8 SERVING: a compact, self-contained snapshot of the top high-value
-- leads, so a free-tier serving DB (Neon, 512 MB) can present NATIONAL leads
-- without holding the multi-GB national CMS substrate they were computed from.
--
-- THE PROBLEM THIS SOLVES
-- -----------------------
-- derived.v_high_value_leads ranks leads live over raw.cms_* . On Neon those
-- raw tables are NJ-only (the national files overrun the free tier), so the
-- live view can only ever serve NJ leads. But the /leads queue shows only the
-- top ~40 rows -- a tiny result. We compute the ranking where the full national
-- substrate lives (a local/Oracle box), then push ONLY the top-N resolved rows
-- here. Neon serves national leads at ~<1 MB; Postgres is never exposed; the app
-- needs no migration. The snapshot is a CACHE of a versioned computation, not a
-- new source of truth.
--
-- SELF-CONTAINED BY DESIGN
-- ------------------------
-- The live view resolves display_name / state by JOINing raw.cms_* at query
-- time. Those joins would return NULL on Neon for a national NPI (no national
-- raw here). So this table stores display_name, provider_state and is_nj
-- PRE-RESOLVED by the loader against the substrate that actually has them.
--
-- VERIFIABLE-DATA INVARIANTS (README "Methodological invariants")
-- --------------------------------------------------------------
-- Every row carries provenance: formula_version, source_scope, source_vintage_
-- hash, snapshot_at, and data_quality = 'computed'. A given (formula_version,
-- source_vintage_hash) reproduces the same ranking. No magic numbers: every
-- value is copied verbatim from the versioned v_high_value_leads computation.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 0. ref.formula_version row.
-- 1. derived.high_value_leads_snapshot  -- plain table, loader-populated.
-- 2. supporting index for the lane-ordered read.
--
-- IDEMPOTENT (CREATE ... IF NOT EXISTS). Safe to re-run. No data inserted here;
-- population is the loader's job (scripts/load_national_leads_snapshot.py).
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 0. Formula version
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '3.7.0-fraud-national-leads-snapshot-v1',
    'Pillar 2 (civic integrity) FRAUD-F8 serving layer. Adds derived.'
    'high_value_leads_snapshot: a compact, pre-resolved cache of the top '
    'derived.v_high_value_leads rows, so a free-tier DB can serve NATIONAL '
    'leads without the national CMS substrate. Each row carries provenance '
    '(formula_version, source_scope, source_vintage_hash, snapshot_at, '
    'data_quality). Cache of a versioned computation; not a new source.',
    '2026-06-10',
    'Decouples serving from the substrate size: compute nationally on a '
    'self-hosted/Oracle box, push top-N here. Loader: '
    'scripts/load_national_leads_snapshot.py.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. derived.high_value_leads_snapshot
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS derived.high_value_leads_snapshot (
    -- Provenance (verifiable-data invariant) -----------------------------------
    source_scope         TEXT        NOT NULL
        CHECK (source_scope IN ('national', 'nj')),
    formula_version      TEXT        NOT NULL
        REFERENCES ref.formula_version (formula_version),
    source_vintage_hash  TEXT        NOT NULL,
    snapshot_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_quality         TEXT        NOT NULL DEFAULT 'computed'
        CHECK (data_quality IN ('measured', 'computed', 'modeled')),

    -- Identity ----------------------------------------------------------------
    lead_rank            INTEGER     NOT NULL,
    entity_kind          TEXT        NOT NULL,
    entity_id            TEXT        NOT NULL,
    display_name         TEXT,
    provider_state       TEXT,
    is_nj                BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Ranking substrate (copied verbatim from v_high_value_leads) --------------
    latest_cycle         CHAR(4)     NOT NULL,
    n_cycles             INTEGER     NOT NULL,
    n_signals            INTEGER     NOT NULL,
    n_families           INTEGER     NOT NULL,
    max_severity         INTEGER     NOT NULL,
    best_reward_tier     INTEGER     NOT NULL,
    reward_eligible      BOOLEAN     NOT NULL,
    has_prior_sanction   BOOLEAN     NOT NULL,
    repeat_violator      BOOLEAN     NOT NULL,
    multi_source         BOOLEAN     NOT NULL,

    -- Financial scale ---------------------------------------------------------
    provider_scale_usd   NUMERIC,
    peak_exposure_usd    NUMERIC,
    total_exposure_usd   NUMERIC,
    reward_low_usd       NUMERIC,
    reward_high_usd      NUMERIC,

    -- Driver + reportability --------------------------------------------------
    driver_signal_id     TEXT        NOT NULL,
    driver_signal_family TEXT,
    recovery_program     TEXT,
    recovery_channel     TEXT,
    recovery_channel_url TEXT,
    statute_citation     TEXT,
    statute_url          TEXT,

    PRIMARY KEY (source_scope, entity_kind, entity_id)
);

COMMENT ON TABLE derived.high_value_leads_snapshot IS
    'FRAUD-F8 serving cache: top derived.v_high_value_leads rows, pre-resolved '
    '(display_name/state) and stamped with provenance, so a free-tier serving '
    'DB can present national leads without the national CMS substrate. '
    'Loader-populated per source_scope; formula 3.7.0-fraud-national-leads-'
    'snapshot-v1.';

-- Lane-ordered read: undetected-first within a scope, then by global rank.
CREATE INDEX IF NOT EXISTS ix_hvl_snapshot_lane
    ON derived.high_value_leads_snapshot
    (source_scope, has_prior_sanction, lead_rank);

COMMIT;
