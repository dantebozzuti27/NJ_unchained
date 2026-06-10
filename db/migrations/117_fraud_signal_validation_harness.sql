-- ============================================================================
-- Migration: 117_fraud_signal_validation_harness
--
-- FRAUD-F8 PRECISION: empirical validation of behavioral anomaly detectors
-- against the platform's own ground-truth labels.
--
-- THE PROBLEM THIS SOLVES
-- -----------------------
-- Our behavioral detectors (opioid_prescribing_outlier, services_per_
-- beneficiary_outlier, antipsychotic_elderly_outlier, ...) are UNSUPERVISED
-- tail-outliers: "top 1% of your specialty on rate X". That maximizes recall
-- but says nothing about PRECISION -- most outliers are legitimate (sicker
-- panels, true high-volume specialists). An anomaly is not a fraud.
--
-- We are, however, sitting on labeled ground truth: the prior-sanction signals
-- (LEIE / NJ-Medicaid / SAM exclusion-billing) flag providers ALREADY known to
-- enforcement. So we can ask, per behavioral signal and per cycle:
--
--     Of the providers this anomaly flags, what fraction are ALSO on an
--     exclusion list -- and is that fraction higher than the background rate
--     of sanctioned providers in the billing universe?
--
--   precision = P(sanctioned | flagged by signal)
--   base_rate = P(sanctioned)                       (whole billing universe)
--   lift      = precision / base_rate
--
-- lift > 1  => the anomaly concentrates known fraud above chance (it carries
--              real signal). lift ~ 1 => the anomaly is noise w.r.t. fraud.
--
-- This converts "top 1%" into a measured, versioned precision/lift statistic
-- that (a) tells us which detectors to trust, and (b) is the basis for
-- precision-weighting the lead ranking in a later increment.
--
-- GROUND TRUTH (positives)
-- ------------------------
-- Per cycle, the positive set = distinct provider NPIs that triggered ANY
-- prior-sanction provider signal (ref.fraud_reportability_channel.
-- is_prior_sanction = TRUE) in that cycle. These are providers who are both
-- (i) on an exclusion/debarment list and (ii) billing Medicare -- the only
-- operational "known bad provider" labels the platform holds.
--
-- UNIVERSE (denominator)
-- ----------------------
-- Per cycle, every distinct billing provider NPI seen in raw.cms_partd_
-- prescriber UNION raw.cms_physician_provider for that data_year. The data is
-- NJ-filtered at load, so the universe is naturally the NJ Medicare provider
-- population.
--
-- HONESTY ABOUT SMALL SAMPLES (verifiable-data §5)
-- ------------------------------------------------
-- NJ-filtered, the positive set is small (excluded providers who still bill
-- are rare), so a behavioral signal's overlap with it can be a handful of NPIs.
-- A naive point precision off 3 overlaps is not significant. The view therefore
-- also emits precision_wilson_lo95, a Wilson-score 95% lower confidence bound on
-- precision, and exposes the raw counts (n_flagged, n_true_positive, n_positives,
-- n_universe) so any consumer can see the sample size and refuse to over-read a
-- thin estimate. NO interpolation, NO imputation: a cycle with zero positives
-- yields a NULL base_rate / lift (cannot be validated), never a fabricated value.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 0. ref.formula_version row.
-- 1. derived.v_signal_validation  -- one row per (cycle, behavioral signal).
--
-- Read-only view over existing tables; no new columns, no refresher, no master-
-- refresher change. IDEMPOTENT (CREATE OR REPLACE). Safe to re-run.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- 0. Formula version
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '3.5.0-fraud-signal-validation-harness-v1',
    'Pillar 2 (civic integrity) FRAUD-F8 precision layer. Adds '
    'derived.v_signal_validation: per (cycle, behavioral signal) precision, '
    'base rate, and lift of each unsupervised anomaly detector measured against '
    'the prior-sanction (LEIE / NJ-Medicaid / SAM exclusion-billing) ground-'
    'truth provider set. Emits a Wilson-score 95% lower bound on precision and '
    'the raw counts so thin estimates are not over-read. Read-only view; no new '
    'signals.',
    '2026-06-09',
    'Foundation for precision-weighting the lead ranking. Lift > 1 means the '
    'detector concentrates known-sanctioned providers above the background rate.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. derived.v_signal_validation
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_signal_validation AS
WITH
-- Billing universe per cycle: every distinct, real provider NPI that billed
-- Medicare (Part D prescriber OR Part B physician summary) in the data year.
universe AS (
    SELECT data_year::text AS cycle, npi
    FROM raw.cms_partd_prescriber
    WHERE npi ~ '^[0-9]{10}$' AND npi <> '0000000000'
    UNION
    SELECT data_year::text AS cycle, npi
    FROM raw.cms_physician_provider
    WHERE npi ~ '^[0-9]{10}$' AND npi <> '0000000000'
),
universe_n AS (
    SELECT cycle, COUNT(*) AS n_universe
    FROM universe
    GROUP BY cycle
),
-- Provider-level observations tagged with whether the firing signal is itself a
-- prior-sanction (ground-truth) predicate or a behavioral candidate.
prov_obs AS (
    SELECT
        o.cycle,
        o.entity_id AS npi,
        o.signal_id,
        cfg.signal_family,
        COALESCE(ch.is_prior_sanction, FALSE) AS is_prior_sanction
    FROM derived.fraud_signal_observation o
    JOIN derived.fraud_signal_config cfg
          ON cfg.signal_id = o.signal_id
    LEFT JOIN ref.fraud_reportability_channel ch
          ON ch.signal_id = o.signal_id
    WHERE o.entity_kind = 'provider'
      AND o.entity_id ~ '^[0-9]{10}$'
      AND o.entity_id <> '0000000000'
),
-- Ground-truth positives per cycle: NPIs flagged by ANY prior-sanction signal.
positives AS (
    SELECT DISTINCT cycle, npi
    FROM prov_obs
    WHERE is_prior_sanction
),
positives_n AS (
    SELECT cycle, COUNT(*) AS n_positives
    FROM positives
    GROUP BY cycle
),
-- Behavioral (candidate) detectors = provider signals that are NOT themselves a
-- prior sanction. These are the anomaly detectors whose precision we estimate.
flagged AS (
    SELECT
        cycle,
        signal_id,
        MIN(signal_family) AS signal_family,
        COUNT(DISTINCT npi) AS n_flagged
    FROM prov_obs
    WHERE NOT is_prior_sanction
    GROUP BY cycle, signal_id
),
-- Flagged providers that are also in the ground-truth positive set (true
-- positives for that behavioral signal in that cycle).
overlap AS (
    SELECT b.cycle, b.signal_id, COUNT(DISTINCT b.npi) AS n_true_positive
    FROM prov_obs b
    JOIN positives p
          ON p.cycle = b.cycle AND p.npi = b.npi
    WHERE NOT b.is_prior_sanction
    GROUP BY b.cycle, b.signal_id
),
base AS (
    SELECT
        f.cycle,
        f.signal_id,
        f.signal_family,
        un.n_universe,
        COALESCE(pn.n_positives, 0)        AS n_positives,
        f.n_flagged,
        COALESCE(ov.n_true_positive, 0)    AS n_true_positive
    FROM flagged f
    JOIN universe_n un  ON un.cycle = f.cycle
    LEFT JOIN positives_n pn ON pn.cycle = f.cycle
    LEFT JOIN overlap ov     ON ov.cycle = f.cycle AND ov.signal_id = f.signal_id
)
SELECT
    cycle,
    signal_id,
    signal_family,
    n_universe,
    n_positives,
    n_flagged,
    n_true_positive,
    -- Background rate of sanctioned providers in the billing universe.
    (n_positives::numeric / NULLIF(n_universe, 0))                      AS base_rate,
    -- precision = P(sanctioned | flagged by this detector).
    (n_true_positive::numeric / NULLIF(n_flagged, 0))                   AS precision,
    -- lift = precision / base_rate. NULL when there is no ground truth this
    -- cycle (cannot be validated) -- explicit gap, never a fabricated 0/1.
    (
        (n_true_positive::numeric / NULLIF(n_flagged, 0))
        / NULLIF(n_positives::numeric / NULLIF(n_universe, 0), 0)
    )                                                                  AS lift,
    -- Wilson-score 95% lower confidence bound on precision. Honest small-sample
    -- floor: with few overlaps a high point precision is not significant.
    -- z = 1.959964 is the 0.975 quantile of the standard normal distribution
    -- (two-sided 95% CI); a fixed mathematical constant, not a tunable knob.
    -- Source: standard normal distribution. Clamped to [0,1].
    CASE WHEN n_flagged > 0 THEN
        GREATEST(0::numeric, LEAST(1::numeric,
            (
                (n_true_positive::numeric / n_flagged)
                + (1.959964 ^ 2) / (2 * n_flagged)
                - 1.959964 * sqrt(
                    (
                        (n_true_positive::numeric / n_flagged)
                        * (1 - (n_true_positive::numeric / n_flagged))
                        + (1.959964 ^ 2) / (4 * n_flagged)
                    ) / n_flagged
                  )
            ) / (1 + (1.959964 ^ 2) / n_flagged)
        ))
    END                                                                AS precision_wilson_lo95
FROM base;

COMMENT ON VIEW derived.v_signal_validation IS
    'FRAUD-F8 precision harness. One row per (cycle, behavioral signal): '
    'precision = P(provider on an exclusion list | flagged by the detector), '
    'base_rate = background sanctioned-provider rate in the CMS billing '
    'universe, lift = precision/base_rate (>1 means the anomaly concentrates '
    'known fraud above chance). Ground truth = providers flagged by any '
    'prior-sanction signal (ref.fraud_reportability_channel.is_prior_sanction). '
    'Emits precision_wilson_lo95 (Wilson 95% lower bound) and raw counts so thin '
    'estimates are not over-read. lift/base_rate are NULL when a cycle has no '
    'ground truth (explicit gap, never imputed). Read-only over existing tables.';


COMMIT;
