-- =============================================================================
-- Migration 097: governance.fraud_signal_baseline -- substrate for the
--                bi-weekly per-signal observation-distribution drift detector
--
-- WHY THIS EXISTS
-- ---------------
-- After mig 094 (master-refresher-consolidation), every Pillar 2 fraud
-- signal is invoked by a single SQL dispatcher
-- (derived.refresh_all_fraud_signal_observations). After mig 095/096
-- (severity-dependency-inversion + cascade-recovery), the calibration
-- table is the canonical single source of truth for severity at the
-- analytical surface. The remaining substrate-hygiene gap is
-- OBSERVATIONAL: there is no automated detection of "the master
-- refresher ran cleanly but one signal's observation count dropped to
-- zero (or 10x'd) compared to the last cycle".
--
-- This migration ships the data substrate that makes per-signal drift
-- detection possible. Two artifacts:
--
--   1. governance.fraud_signal_baseline -- append-only table that
--      records per-cycle, per-signal observation counts at each refresh.
--      PRIMARY KEY (cycle, signal_id, captured_at) so we accumulate a
--      time series of samples per (cycle, signal_id).
--
--   2. governance.v_fraud_signal_baseline_stats -- view that rolls up
--      the table into per-(cycle, signal_id) mean + sample-stddev +
--      n_samples + first/last sample timestamps. The asset check in
--      orchestration/asset_checks.py (separate commit) consumes this
--      view to compute z-scores against the latest observation.
--
-- DESIGN NOTES
-- ------------
-- * APPEND-ONLY. Each refresh captures a new row; we never UPDATE
--   existing rows. This gives us a forensic record of "what the count
--   was at each refresh", which matters because the drift detector
--   needs n>=3 samples for a 2σ test (a single-sample series has
--   sample-stddev = NULL by definition).
--
-- * KEYED ON CAPTURED_AT. The natural key per measurement is
--   (cycle, signal_id, captured_at). Same (cycle, signal_id) at
--   different captured_at times = different rows.
--
-- * RETENTION: there is no retention policy in this migration. After
--   a year of bi-weekly captures we'd have ~26 samples per
--   (cycle, signal_id), which at 18 signals × 2 active cycles is ~936
--   rows. Even at 10 years that's ~9,360 rows -- trivial. No need for
--   archival logic.
--
-- * SEEDING WITH PROD VALUES: the migration seeds the table with the
--   current production per-signal counts (captured 2026-05-11) so the
--   first drift check has at least 1 sample to compare against. After
--   3+ captures, the 2σ test becomes meaningful.
--
-- IDEMPOTENT. Safe to re-run.
-- =============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.7.0-fraud-signal-drift-baseline-v1',
    'Ships governance.fraud_signal_baseline (append-only per-cycle/'
    'signal/captured_at observation-count history) and '
    'governance.v_fraud_signal_baseline_stats (per-(cycle, signal_id) '
    'mean + sample-stddev rollup). Substrate for the bi-weekly '
    'per-signal drift detector. Initial seed captured the 2026-05-11 '
    'production counts so the first drift check has a baseline to '
    'compare against; meaningful 2σ comparisons require n_samples >= 3 '
    'and are gated accordingly in the consuming asset check.',
    '2026-05-11'::DATE,
    'Stacks on 2.6.1-cascade-recovery-nj-official-views-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- governance.fraud_signal_baseline
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS governance.fraud_signal_baseline (
    cycle        CHAR(4)                     NOT NULL,
    signal_id    TEXT                        NOT NULL,
    n_obs        INT                         NOT NULL,
    captured_at  TIMESTAMPTZ                 NOT NULL DEFAULT now(),
    formula_version TEXT,
    notes        TEXT,
    PRIMARY KEY (cycle, signal_id, captured_at),
    CONSTRAINT fraud_signal_baseline_n_obs_non_negative
        CHECK (n_obs >= 0)
);

CREATE INDEX IF NOT EXISTS idx_fraud_signal_baseline_cycle_signal
    ON governance.fraud_signal_baseline (cycle, signal_id);
CREATE INDEX IF NOT EXISTS idx_fraud_signal_baseline_captured_at
    ON governance.fraud_signal_baseline (captured_at DESC);

COMMENT ON TABLE governance.fraud_signal_baseline IS
    'Append-only history of per-cycle, per-signal observation counts. '
    'Each refresh of derived.fraud_signal_observation should INSERT one '
    'row per (cycle, signal_id) capturing the post-refresh count. The '
    'companion view v_fraud_signal_baseline_stats rolls this into '
    'mean + stddev for the bi-weekly drift detector. Substrate-honest: '
    'append-only so no operator can silently rewrite history (forensic '
    'audit preserved). Mig 097.';

COMMENT ON COLUMN governance.fraud_signal_baseline.captured_at IS
    'Wall-clock timestamp of the refresh that produced this count. '
    'Part of the PK so multiple samples per (cycle, signal_id) '
    'accumulate without conflict; required for stddev calculation '
    '(n_samples >= 2).';


-- ----------------------------------------------------------------------------
-- governance.v_fraud_signal_baseline_stats
--
-- Per-(cycle, signal_id) rollup of the baseline table. Used by the
-- drift asset check to compute z-scores against current observation
-- counts. STDDEV_SAMP returns NULL when n_samples = 1 -- we propagate
-- the NULL rather than fabricating a default; the consuming check
-- gates on n_samples >= 3 before computing a z-score.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW governance.v_fraud_signal_baseline_stats AS
SELECT
    cycle,
    signal_id,
    AVG(n_obs)::NUMERIC                                      AS mean_n,
    STDDEV_SAMP(n_obs)::NUMERIC                              AS stddev_n,
    COUNT(*)::INT                                            AS n_samples,
    MIN(n_obs)::INT                                          AS min_n,
    MAX(n_obs)::INT                                          AS max_n,
    MIN(captured_at)                                         AS first_sample_at,
    MAX(captured_at)                                         AS last_sample_at
FROM governance.fraud_signal_baseline
GROUP BY cycle, signal_id;

COMMENT ON VIEW governance.v_fraud_signal_baseline_stats IS
    'Per-(cycle, signal_id) rollup of the fraud_signal_baseline history. '
    'mean_n and stddev_n are SAMPLE statistics (STDDEV_SAMP, so n=1 '
    'returns NULL stddev; never invent a default). The consuming asset '
    'check gates 2σ drift detection on n_samples >= 3. Mig 097.';


-- ----------------------------------------------------------------------------
-- Initial seed: capture the 2026-05-11 production counts so the first
-- drift check has a baseline. Hardcoded from a SELECT on the live Neon
-- substrate; future captures will accumulate automatically as the
-- bi-weekly schedule re-materializes derived.fraud_signal_observation.
-- ----------------------------------------------------------------------------
INSERT INTO governance.fraud_signal_baseline
    (cycle, signal_id, n_obs, captured_at, formula_version, notes)
VALUES
    -- Cycle 2024 (10 signals firing as of 2026-05-11; 8 structural-FEC
    -- + 1 LEIE + 1 LEIE-strict-address)
    ('2024', 'candidate_broken_pcc',         155,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2024', 'candidate_multiple_pccs',       48,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2024', 'candidate_namesakes',          609,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2024', 'candidate_no_pcc',            1760,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2024', 'committee_address_clusters',   530,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2024', 'committee_name_collisions',    387,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2024', 'entity_on_leie',              1227,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2024', 'entity_on_leie_strict_address',  1,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2024', 'treasurer_concentration',     1072,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2024', 'treasurer_is_candidate',      1568,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    -- Cycle 2026 (9 signals; entity_on_leie_strict_address has 0 obs)
    ('2026', 'candidate_broken_pcc',         105,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2026', 'candidate_multiple_pccs',       21,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2026', 'candidate_namesakes',          522,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2026', 'candidate_no_pcc',             813,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2026', 'committee_address_clusters',   528,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2026', 'committee_name_collisions',    298,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2026', 'entity_on_leie',              1039,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2026', 'treasurer_concentration',      991,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy'),
    ('2026', 'treasurer_is_candidate',      1434,
     '2026-05-11T12:00:00+00:00'::TIMESTAMPTZ,
     '2.7.0-fraud-signal-drift-baseline-v1',
     'Initial seed: production count at mig 097 deploy')
ON CONFLICT (cycle, signal_id, captured_at) DO NOTHING;


-- ----------------------------------------------------------------------------
-- governance.capture_fraud_signal_baseline()
--
-- Helper function: called by the bi-weekly refresh asset (after the
-- master refresher completes) to capture the current per-signal
-- observation counts into the baseline table. Returns the number of
-- rows inserted (one per cycle x signal_id with COUNT > 0; signals
-- with zero observations also get captured so a regression to 0 is
-- visible in the drift detector).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION governance.capture_fraud_signal_baseline(
    p_cycle CHAR(4)
) RETURNS INT
LANGUAGE plpgsql
AS $$
DECLARE
    n_inserted INT := 0;
BEGIN
    -- We insert one row per (cycle, signal_id) using ALL signals that
    -- currently appear in derived.fraud_signal_config (the canonical
    -- universe), LEFT JOINed to the observation table. Signals with
    -- zero current observations get n_obs = 0 captured. This makes a
    -- "regression to 0" detectable -- otherwise the signal would just
    -- vanish from the time series.
    WITH inserted AS (
        INSERT INTO governance.fraud_signal_baseline
            (cycle, signal_id, n_obs, captured_at,
             formula_version, notes)
        SELECT
            p_cycle,
            cfg.signal_id,
            COALESCE(obs.n_obs, 0)                                AS n_obs,
            now()                                                 AS captured_at,
            '2.7.0-fraud-signal-drift-baseline-v1'                AS formula_version,
            'Captured by governance.capture_fraud_signal_baseline' AS notes
        FROM derived.fraud_signal_config cfg
        LEFT JOIN (
            SELECT signal_id, COUNT(*)::INT AS n_obs
            FROM   derived.fraud_signal_observation
            WHERE  cycle = p_cycle
            GROUP BY signal_id
        ) obs ON obs.signal_id = cfg.signal_id
        ON CONFLICT (cycle, signal_id, captured_at) DO NOTHING
        RETURNING 1
    )
    SELECT COUNT(*)::INT INTO n_inserted FROM inserted;

    RETURN n_inserted;
END;
$$;

COMMENT ON FUNCTION governance.capture_fraud_signal_baseline(CHAR(4)) IS
    'Captures the current per-signal observation counts for the given '
    'cycle into governance.fraud_signal_baseline. Insertion uses now() '
    'as captured_at, so concurrent calls within the same microsecond '
    'would conflict on the PK -- in practice this is fine because the '
    'bi-weekly refresh schedule has 14-day cadence. Includes every '
    'signal_id in derived.fraud_signal_config (LEFT JOIN COALESCE 0) '
    'so a signal that regresses to zero observations is still captured '
    '(otherwise the time series would silently lose the data point). '
    'Returns the number of rows inserted. Mig 097.';


COMMIT;
