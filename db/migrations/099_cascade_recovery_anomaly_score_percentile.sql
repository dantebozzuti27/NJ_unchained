-- =============================================================================
-- Migration 099: cascade recovery -- restore derived.v_anomaly_score_percentile
--                _by_kind_cycle, silently dropped by mig 095's CASCADE
--
-- VISION_2026 Pillar 2 (civic integrity) -- Phase governance §7
-- (operational reliability) cascade-recovery, second iteration.
--
-- WHY THIS EXISTS
-- ---------------
-- Mig 095 (severity-dependency-inversion) executes:
--     DROP VIEW IF EXISTS derived.v_entity_fraud_risk      CASCADE;
--     DROP VIEW IF EXISTS derived.v_entity_fraud_features  CASCADE;
-- to allow column-list re-shaping (Postgres CREATE OR REPLACE VIEW
-- rejects column-order or type changes; DROP+CREATE is the only path).
-- The CASCADE was correct for the rebuild but transitively dropped
-- THREE downstream consumers:
--     * derived.v_nj_federal_officials               (recovered by mig 096)
--     * derived.v_nj_civic_integrity_state_summary   (recovered by mig 096)
--     * derived.v_anomaly_score_percentile_by_kind_cycle  (NOT recovered)
--
-- Mig 096's header (lines 25-29) explicitly notes the substrate-honest
-- pattern:
--     "The substrate-honest pattern for any DROP ... CASCADE in a
--      migration: run \d+ on each transitively-dropped view BEFORE the
--      CASCADE, and recreate them in the same migration."
-- The percentile view was overlooked -- the operator who shipped 095/096
-- missed it because no live UI surface consumes the view (only the
-- deploy script and tests reference it). The platform still functioned
-- end-to-end, but the test suite has been red on
-- TestPercentileViewColumnShape + TestPercentileViewArithmetic since
-- 2026-05-11.
--
-- Production verification 2026-05-12 22:30 ET:
--   SELECT to_regclass('derived.v_anomaly_score_percentile_by_kind_cycle')
--   -> NULL  (confirmed dropped on Neon prod since mig 095 deploy)
--
-- WHAT THIS SHIPS
-- ---------------
-- Verbatim restoration of the view definition from mig 086 lines 355-381.
-- The view's SQL has not evolved between 086 and now -- the percentile
-- shape (PERCENT_RANK + CUME_DIST + COUNT(*) over the cycle x kind
-- partition) is exactly what 086 contracted for. Reproducing it here is
-- the cascade-recovery analog of mig 096's recreation of
-- v_nj_federal_officials.
--
-- The view's `formula_version` literal is preserved at
-- '2.1.0-fraud-evidence-substrate-v1' (the formula_version registered
-- by mig 086). The recovery does NOT change the substrate's claimed
-- formula -- the math is identical -- so an analyst comparing pctile
-- numbers across the recovery boundary sees no semantic shift.
--
-- WHY NOT FOLD THIS INTO MIG 098
-- -------------------------------
-- Architectural separation. Mig 098 ships the nj_state_candidate_on_leie
-- cross-source signal (a NEW capability). Mig 099 RECOVERS a previously-
-- existing capability that a prior migration's CASCADE silently
-- destroyed. Folding the two would conflate "new feature" with
-- "regression repair" in the migration ledger, making future audits of
-- 098 confusing. The two ship in the same code change-set but as
-- independent migrations, mirroring the 095/096 pair.
--
-- IDEMPOTENT VIA CREATE OR REPLACE VIEW + governance.schema_migrations
-- sha256 ledger. Safe to re-run.
-- =============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.7.2-cascade-recovery-anomaly-score-percentile-v1',
    'Pillar 2 (civic integrity) governance §7 (operational reliability) '
    'cascade-recovery, second iteration. Restores '
    'derived.v_anomaly_score_percentile_by_kind_cycle which mig 095''s '
    'DROP VIEW ... CASCADE transitively dropped at 2026-05-11 23:39 UTC. '
    'Mig 096 recovered v_nj_federal_officials and '
    'v_nj_civic_integrity_state_summary from the same CASCADE but missed '
    'this third dependent. Recovery is a verbatim re-CREATE OR REPLACE of '
    'the view definition from mig 086 -- the percentile substrate''s '
    'shape (PERCENT_RANK + CUME_DIST + COUNT over cycle x kind partition) '
    'has not evolved, so the recovery preserves bit-identical semantics. '
    'No live UI surface consumed the view in the gap window (only the '
    'deploy script + test suite); the platform end-to-end was '
    'unaffected, but the test suite has been red on '
    'TestPercentileViewColumnShape + TestPercentileViewArithmetic since '
    'the 095 deploy. This migration restores the view AND green-ifies '
    'those tests.',
    '2026-05-12'::DATE,
    'Stacks on 2.7.1-fraud-nj-state-candidate-on-leie-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- derived.v_anomaly_score_percentile_by_kind_cycle (verbatim from mig 086)
--
-- One row per (cycle, entity_kind, entity_id) with the empirical CDF of
-- risk_score within the (cycle, entity_kind) partition. Substrate for
-- the /risk and /risk/[kind]/[id] headline calibration label
-- ("exceeds N% of NJ <kind>s in cycle <year>").
--
-- Three percentile-shaped columns:
--   pctile_within_kind_cycle  -- PERCENT_RANK ((rank-1)/(N-1)).
--                                Top entity reads as 1.0. UI surface.
--   cume_dist_within_kind_cycle -- CUME_DIST (rank-with-ties / N).
--                                  Inclusive of ties. Reserved for
--                                  callers that want "at or above"
--                                  semantics.
--   n_peers_in_bucket          -- partition cardinality. UI suppresses
--                                 the calibration label when this is
--                                 small (operationally N < 10) to avoid
--                                 claims like "exceeds 100% of 1 peer."
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_anomaly_score_percentile_by_kind_cycle AS
WITH scored AS (
    SELECT
        cycle,
        entity_kind,
        entity_id,
        risk_score
    FROM derived.v_entity_fraud_risk
    WHERE risk_score IS NOT NULL
)
SELECT
    cycle,
    entity_kind,
    entity_id,
    risk_score,
    PERCENT_RANK() OVER (
        PARTITION BY cycle, entity_kind
        ORDER BY risk_score
    )::NUMERIC(7, 6)                                AS pctile_within_kind_cycle,
    CUME_DIST() OVER (
        PARTITION BY cycle, entity_kind
        ORDER BY risk_score
    )::NUMERIC(7, 6)                                AS cume_dist_within_kind_cycle,
    COUNT(*) OVER (PARTITION BY cycle, entity_kind)::INT
                                                    AS n_peers_in_bucket,
    -- Preserve the original formula_version literal so analysts comparing
    -- pctile numbers across the recovery boundary see no semantic shift.
    -- The CASCADE-recovery is bit-identical to the pre-095 view.
    '2.1.0-fraud-evidence-substrate-v1'::TEXT       AS formula_version
FROM scored;

COMMENT ON VIEW derived.v_anomaly_score_percentile_by_kind_cycle IS
    'Per-entity empirical CDF of risk_score within (cycle, entity_kind). '
    'Substrate for /risk and /risk/[kind]/[id] headline calibration label '
    '("exceeds N% of NJ <kind>s in cycle <year>"). Consumes '
    'derived.v_entity_fraud_risk; one row per entity, NULLs filtered out. '
    'pctile_within_kind_cycle uses PERCENT_RANK ((rank-1)/(N-1)); '
    'cume_dist_within_kind_cycle uses CUME_DIST (rank-with-ties/N) for '
    'callers wanting inclusive semantics. n_peers_in_bucket is the '
    'partition cardinality. Formula 2.1.0-fraud-evidence-substrate-v1. '
    'Recovered by mig 099 from the silent CASCADE-drop in mig 095.';

COMMENT ON COLUMN derived.v_anomaly_score_percentile_by_kind_cycle.pctile_within_kind_cycle IS
    'PERCENT_RANK: fraction of peers with strictly lower risk_score, '
    'normalized to [0, 1]. The top entity always reads as 1.0. The UI '
    'displays this as "exceeds <pct*100>% of peers".';

COMMENT ON COLUMN derived.v_anomaly_score_percentile_by_kind_cycle.cume_dist_within_kind_cycle IS
    'CUME_DIST: fraction of peers with risk_score <= self, in (0, 1]. '
    'Inclusive of ties. Exposed for callers that want "at or above" '
    'semantics rather than "strictly above".';

COMMENT ON COLUMN derived.v_anomaly_score_percentile_by_kind_cycle.n_peers_in_bucket IS
    'COUNT(*) over the (cycle, entity_kind) partition. The UI suppresses '
    'the calibration label when n_peers_in_bucket is small (operationally '
    'N < 10) to avoid claims like "exceeds 100% of 1 peer".';


COMMIT;
