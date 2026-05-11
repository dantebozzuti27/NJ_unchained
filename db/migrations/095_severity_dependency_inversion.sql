-- =============================================================================
-- Migration 095: severity dependency inversion -- L2/L3 views read severity
--                from ref.fraud_signal_severity_calibration (single source
--                of truth at the analytical surface)
--
-- VISION_2026 Pillar 2 (civic integrity) substrate hygiene. Executes the
-- "F4-extension" work that mig 088 line 62-65 explicitly punted to a
-- future migration:
--
--   "Rewire the refresher to consume severity_level from the calibration
--    table. That's still the F4-extension work; severity is hardcoded
--    in [refreshers]. The F4-extension migration will INVERT the
--    [dependency]."
--
-- BACKGROUND: the current dual-source-of-truth
-- ---------------------------------------------
-- The 17-signal-now-18-signal taxonomy carries `severity SMALLINT` in
-- TWO places:
--
--   1. Hardcoded literal in each of 18 refresher functions
--      (derived.refresh_signal_*, derived.refresh_*_observations).
--      Refreshers DELETE+INSERT into derived.fraud_signal_observation
--      with a hardcoded SMALLINT (e.g. `severity = 5` for entity_on_leie).
--
--   2. Documented in ref.fraud_signal_severity_calibration (seed 019).
--      Same SMALLINT value, plus calibration_basis + precedent_url +
--      precedent_summary that anchor the severity to a federal authority.
--
-- The duplicate is presently enforced by regression test
-- test_fraud_evidence_substrate.py::TestSeverityMatchesRefresher, which
-- asserts that for every fired observation, the severity column matches
-- the calibration table. The test is a runtime-substrate consistency
-- check, NOT a structural prevention of drift -- an operator who updates
-- the calibration table without re-running the refresher (or vice versa)
-- creates a drift window that the test catches only after the next
-- refresh cycle.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- This migration inverts the dependency at the ANALYTICAL SURFACE LAYER
-- (L2/L3 views), not at the refresher body layer:
--
--   1. ref.f_signal_severity(p_signal_id TEXT) RETURNS SMALLINT -- a
--      STABLE PARALLEL-SAFE function that returns the calibration-table
--      severity for a signal_id, RAISES if signal_id is unknown.
--      Provides a clean lookup API for any future caller (refreshers,
--      ad-hoc queries, audit functions).
--
--   2. derived.v_entity_fraud_features REWRITTEN to source severity from
--      ref.fraud_signal_severity_calibration via LEFT JOIN. The
--      MAX(severity) and ARRAY_AGG(severity) aggregations now consume
--      `COALESCE(sc.severity_level, o.severity)` -- substrate-honest
--      soft transition: when calibration has a row (the normal case for
--      every signal currently seeded), the calibration value wins; when
--      it doesn't (transient gap during a new-signal rollout), the
--      refresher hardcoded value is used as a graceful fallback so the
--      view doesn't blank out for the new signal.
--
--   3. derived.v_entity_fraud_evidence REWRITTEN identically -- the
--      `severity` column exposed to the UI now sources from
--      ref.fraud_signal_severity_calibration (COALESCE fallback to base
--      column for graceful new-signal handling).
--
--   4. derived.audit_severity_drift(p_cycle CHAR(4)) -- audit function
--      returning a TABLE of (signal_id, n_obs, hardcoded_severity,
--      calibration_severity, drifted BOOLEAN) so operators can surface
--      and quantify drift in production without parsing JSON or
--      reading raw observation rows.
--
-- WHY THIS IS THE RIGHT GRAIN
-- ---------------------------
-- The substrate-honest goal is: "the value that surfaces to the analyst
-- (UI badge, evidence panel, risk-score component) is canonically
-- sourced from a single documented reference table." After this
-- migration, that goal IS achieved:
--
--   * UI consumers read derived.v_entity_fraud_risk (which transitively
--     reads severities from L2) and derived.v_entity_fraud_evidence.
--     Both views now source severity from ref.fraud_signal_severity_-
--     calibration. Updating the calibration table value + re-querying
--     the view produces the new value -- no refresher re-run required.
--
--   * The base table's `severity` column becomes an audit artifact:
--     "what the refresher emitted at insert time." Reading it directly
--     is a substrate-honesty smell after this migration (the value
--     might disagree with the calibration). The audit function exposes
--     such disagreements.
--
--   * Refresher bodies are NOT rewritten in this migration. That is a
--     follow-up (mig 096+) where each refresher can be opportunistically
--     updated to call ref.f_signal_severity('<signal_id>') instead of
--     hardcoding a literal. The audit function lets us schedule those
--     rewrites without urgency -- drift is observable but not
--     functionally harmful, because the UI now ignores the base column.
--
-- WHAT THIS MIGRATION DELIBERATELY DOES NOT DO
-- --------------------------------------------
-- 1. Rewrite the 18 refresher function bodies. Each refresher is a
--    100-300 line DELETE+INSERT function and rewriting 18 of them in
--    one migration carries high-touch risk for low incremental value
--    (the analytical surface already reads from calibration after this
--    migration; refresher rewrites are cosmetic). Deferred to mig
--    096+ on a per-signal basis when the function is touched for
--    unrelated reasons.
--
-- 2. Drop the severity column from derived.fraud_signal_observation.
--    The column remains as an audit artifact -- which value the
--    refresher emitted at the time of insertion. A future migration
--    can drop it once we're confident no code path reads it directly
--    (the test suite + asset_checks.py grep above show ~40 direct
--    reads in tests; production code only goes through L2/L3 views).
--
-- 3. Add a trigger that overrides severity on insert. Tempting but
--    substrate-dishonest: it creates a hidden override and the column
--    no longer reflects what the refresher actually wrote -- audit
--    forensics become harder. Better to let the column drift be visible
--    via audit_severity_drift() so operators can choose remediation.
--
-- IDEMPOTENT via CREATE OR REPLACE + ON CONFLICT. Safe to re-run.
-- =============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.6.0-severity-dependency-inversion-v1',
    'Pillar 2 substrate hygiene: derived.v_entity_fraud_features and '
    'derived.v_entity_fraud_evidence now source `severity` from '
    'ref.fraud_signal_severity_calibration via LEFT JOIN (COALESCE '
    'fallback to base-table value for graceful new-signal transient '
    'handling). The base column derived.fraud_signal_observation.severity '
    'is now an audit artifact ("what the refresher emitted"); analytical '
    'surfaces canonically read the calibration table value. Adds '
    'ref.f_signal_severity(p_signal_id) lookup function (RAISE on missing) '
    'and derived.audit_severity_drift(p_cycle) audit function. The 18 '
    'refresher bodies are deliberately NOT rewritten in this migration -- '
    'rewriting them is cosmetic (the analytical surface already reads '
    'from calibration after this migration) and is deferred to mig 096+ '
    'on a per-signal basis when each refresher is touched for unrelated '
    'reasons.',
    '2026-05-11'::DATE,
    'Stacks on 2.5.0-master-refresher-consolidation-v1. Executes the '
    'F4-extension work explicitly punted by mig 088 line 62-65.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- ref.f_signal_severity(p_signal_id TEXT) -> SMALLINT
--
-- Scalar lookup that returns the calibration severity for a signal_id.
-- RAISES if signal_id has no row in ref.fraud_signal_severity_calibration
-- so a missing calibration is loud, not silent. STABLE so it's usable
-- inside CHECK constraints (via wrapper) and indexable in WHERE clauses.
-- PARALLEL SAFE so query planner can parallelize aggregations that use it.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ref.f_signal_severity(p_signal_id TEXT)
RETURNS SMALLINT
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
AS $$
DECLARE
    s SMALLINT;
BEGIN
    SELECT severity_level INTO s
    FROM   ref.fraud_signal_severity_calibration
    WHERE  signal_id = p_signal_id;

    IF s IS NULL THEN
        RAISE EXCEPTION
            'ref.f_signal_severity: signal_id % has no row in '
            'ref.fraud_signal_severity_calibration. Seed 019 (or its '
            'successor) must include every signal_id present in '
            'derived.fraud_signal_config.',
            p_signal_id
        USING ERRCODE = 'no_data_found';
    END IF;

    RETURN s;
END;
$$;

COMMENT ON FUNCTION ref.f_signal_severity(TEXT) IS
    'Returns severity_level for the given signal_id from '
    'ref.fraud_signal_severity_calibration. RAISES no_data_found if '
    'signal_id is unknown -- substrate-honest, never invents a default. '
    'Formula 2.6.0-severity-dependency-inversion-v1.';


-- ----------------------------------------------------------------------------
-- derived.v_entity_fraud_features REWRITTEN
--
-- Severity now sourced from ref.fraud_signal_severity_calibration via
-- LEFT JOIN. COALESCE(sc.severity_level, o.severity) is the substrate-
-- honest soft-transition expression: when the signal is calibrated (the
-- normal case for every signal currently seeded) the calibration value
-- wins; when it's not (transient gap during new-signal rollout) the
-- refresher hardcoded value is the graceful fallback.
--
-- ALL other behavior preserved from mig 061:
--   * JOIN to derived.fraud_signal_config for signal_families + min
--     actionable threshold
--   * WHERE raw_value >= cfg.min_actionable_threshold (per-signal floor)
--   * ARRAY_AGG(signal_family) column for the 3-arg L3a scoring fn
--
-- Use DROP+CREATE (not CREATE OR REPLACE) because we are changing the
-- shape of the underlying SELECT (the CTE adds a column lookup) and
-- Postgres requires column-order stability for CREATE OR REPLACE VIEW.
-- v_entity_fraud_risk depends on this view; CASCADE drops it and we
-- recreate it below with the identical 3-arg fraud_risk_score signature.
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS derived.v_entity_fraud_risk      CASCADE;
DROP VIEW IF EXISTS derived.v_entity_fraud_features  CASCADE;

CREATE VIEW derived.v_entity_fraud_features AS
WITH obs_with_calibrated_severity AS (
    -- Source severity from calibration (canonical) with graceful
    -- fallback to the refresher-emitted base-column value. We retain
    -- the JOIN to derived.fraud_signal_config so the min_actionable_-
    -- threshold filter + signal_family lookup are inside the same
    -- subquery -- single round-trip, no double JOIN.
    SELECT
        o.cycle,
        o.entity_kind,
        o.entity_id,
        o.signal_id,
        o.raw_value,
        o.peer_bucket,
        o.peer_percentile,
        o.evidence_url,
        o.materialized_at,
        COALESCE(sc.severity_level, o.severity)               AS severity,
        cfg.signal_family                                     AS signal_family
    FROM derived.fraud_signal_observation                    o
    JOIN derived.fraud_signal_config                         cfg
         ON cfg.signal_id = o.signal_id
    LEFT JOIN ref.fraud_signal_severity_calibration          sc
         ON sc.signal_id = o.signal_id
    WHERE o.raw_value >= cfg.min_actionable_threshold
)
SELECT
    cycle,
    entity_kind,
    entity_id,

    COUNT(*)::INT                                            AS n_signals_fired,
    MAX(severity)::SMALLINT                                  AS max_severity,
    MAX(peer_percentile)                                     AS max_peer_percentile,
    AVG(peer_percentile)                                     AS avg_peer_percentile,

    (ARRAY_AGG(peer_bucket
               ORDER BY severity DESC,
                        peer_percentile DESC,
                        signal_id))[1]
        AS primary_peer_bucket,

    ARRAY_AGG(signal_id        ORDER BY signal_id)           AS signals_fired,
    ARRAY_AGG(severity         ORDER BY signal_id)           AS severities,
    ARRAY_AGG(peer_percentile  ORDER BY signal_id)           AS peer_percentiles,
    ARRAY_AGG(peer_bucket      ORDER BY signal_id)           AS peer_buckets,
    ARRAY_AGG(raw_value        ORDER BY signal_id)           AS raw_values,
    ARRAY_AGG(evidence_url     ORDER BY signal_id)           AS evidence_urls,

    ARRAY_AGG(signal_family    ORDER BY signal_id)           AS signal_families,

    MAX(materialized_at)                                     AS last_observation_at
FROM   obs_with_calibrated_severity
GROUP  BY cycle, entity_kind, entity_id;

COMMENT ON VIEW derived.v_entity_fraud_features IS
    'TIER 4 v3 L2: per-entity wide pivot of L1 observations with per-'
    'signal min_actionable_threshold filter and signal_family tags '
    'joined from derived.fraud_signal_config. Severity sourced from '
    'ref.fraud_signal_severity_calibration via LEFT JOIN (COALESCE '
    'fallback to base-column value for graceful new-signal transient '
    'handling). One row per (cycle, entity_kind, entity_id) where at '
    'least one signal cleared its threshold. The L3 scoring function '
    'consumes (severities, peer_percentiles, signal_families). '
    'Formula 2.6.0-severity-dependency-inversion-v1.';


-- ----------------------------------------------------------------------------
-- derived.v_entity_fraud_risk RECREATED identical to mig 061
--
-- The CASCADE on the DROP above removed it. Recreate with the same
-- 3-arg fraud_risk_score signature (severities, peer_percentiles,
-- signal_families) so the diversity-bonus L3a scoring is unchanged.
-- This view's behavior is identical to its pre-mig-095 form -- the
-- changes are entirely upstream in v_entity_fraud_features.
-- ----------------------------------------------------------------------------
CREATE VIEW derived.v_entity_fraud_risk AS
SELECT
    f.cycle,
    f.entity_kind,
    f.entity_id,

    derived.fraud_risk_score(
        f.severities,
        f.peer_percentiles,
        f.signal_families
    ) AS risk_score,

    f.n_signals_fired,
    f.max_severity,
    f.max_peer_percentile,
    f.avg_peer_percentile,
    f.primary_peer_bucket,

    f.signals_fired,
    f.severities,
    f.peer_percentiles,
    f.peer_buckets,
    f.raw_values,
    f.evidence_urls,
    f.signal_families,

    f.last_observation_at
FROM derived.v_entity_fraud_features f;

COMMENT ON VIEW derived.v_entity_fraud_risk IS
    'TIER 4 v3 read surface: per-entity feature vector + risk_score '
    'with the multi-family diversity bonus. Sort DESC by risk_score '
    'for the analyst queue; filter by (cycle, entity_kind) for the '
    '/fec/risk/entities API; deep-link to /fec/risk/entities/{kind}/{id} '
    'for the evidence panel. Stacks on mig 061; mig 095 source '
    'severity from ref.fraud_signal_severity_calibration upstream.';


-- ----------------------------------------------------------------------------
-- derived.v_entity_fraud_evidence REWRITTEN
--
-- Surfaces COALESCE(sc.severity_level, o.severity) as `severity` in the
-- column list. Everything else is identical to mig 089's definition.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_entity_fraud_evidence AS
WITH cand_meta AS (
    SELECT
        cycle,
        cand_id,
        cand_name,
        cand_office,
        cand_office_st,
        cand_office_district,
        cand_pty_affiliation,
        cand_ici,
        cand_status,
        cand_election_yr,
        (cand_office_st = 'NJ')                                  AS is_nj
    FROM raw.fec_candidate
),
cmte_meta AS (
    SELECT
        cycle,
        cmte_id,
        cmte_nm,
        cmte_st,
        cmte_city,
        cmte_zip,
        tres_nm,
        cand_id                                                  AS pcc_cand_id,
        (cmte_st = 'NJ')                                         AS is_nj
    FROM raw.fec_committee
),
treas_meta AS (
    SELECT
        cycle,
        UPPER(TRIM(tres_nm))                                     AS treasurer_id,
        BOOL_OR(cmte_st = 'NJ')                                  AS is_nj,
        COUNT(DISTINCT cmte_id)                                  AS n_committees_treasured,
        COUNT(DISTINCT cmte_id) FILTER (WHERE cmte_st = 'NJ')    AS n_nj_committees_treasured
    FROM raw.fec_committee
    WHERE tres_nm IS NOT NULL AND tres_nm <> ''
    GROUP BY 1, 2
)
SELECT
    o.cycle,
    o.entity_kind,
    o.entity_id,
    o.signal_id,
    o.raw_value,
    -- Severity now reads from the calibration table (canonical source).
    -- COALESCE preserves the refresher's emitted value as a graceful
    -- fallback when calibration is missing the signal (transient gap
    -- during new-signal rollout).
    COALESCE(sc.severity_level, o.severity)                      AS severity,
    o.peer_bucket,
    o.peer_percentile,
    o.materialized_at,

    CASE o.entity_kind
        WHEN 'candidate' THEN COALESCE(cand.is_nj,  FALSE)
        WHEN 'committee' THEN COALESCE(cmte.is_nj,  FALSE)
        WHEN 'treasurer' THEN COALESCE(treas.is_nj, FALSE)
        WHEN 'address'   THEN (SPLIT_PART(o.entity_id, '|', 3) = 'NJ')
        ELSE FALSE
    END                                                          AS is_nj,

    CASE o.entity_kind
        WHEN 'candidate' THEN cand.cand_name
        WHEN 'committee' THEN cmte.cmte_nm
        WHEN 'treasurer' THEN o.entity_id
        WHEN 'address'   THEN SPLIT_PART(o.entity_id, '|', 1)
                              || COALESCE(', ' || SPLIT_PART(o.entity_id, '|', 2), '')
                              || COALESCE(' '  || SPLIT_PART(o.entity_id, '|', 3), '')
                              || COALESCE(' '  || SPLIT_PART(o.entity_id, '|', 4), '')
        ELSE o.entity_id
    END                                                          AS display_name,

    cand.cand_office                                             AS office_code,
    cand.cand_office_st                                          AS office_state,
    cand.cand_office_district                                    AS office_district,
    cand.cand_pty_affiliation                                    AS office_party,
    cand.cand_ici                                                AS office_incumbent_status,
    cand.cand_election_yr                                        AS office_election_year,

    treas.n_committees_treasured                                 AS treasurer_n_committees,
    treas.n_nj_committees_treasured                              AS treasurer_n_nj_committees,

    cmte.cmte_st                                                 AS committee_state,
    cmte.cmte_city                                               AS committee_city,
    cmte.tres_nm                                                 AS committee_treasurer_name,
    cmte.pcc_cand_id                                             AS committee_pcc_candidate_id,

    he.rule_text                                                 AS rule_text,
    he.citation_authority                                        AS citation_authority,
    he.citation_section                                          AS citation_section,
    he.citation_url                                              AS citation_url,

    REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
        COALESCE(he.plain_english_template, ''),
        '{{entity_id}}',       o.entity_id),
        '{{cycle}}',           o.cycle),
        '{{raw_value}}',       COALESCE(o.raw_value::TEXT, '')),
        '{{peer_percentile}}', COALESCE(ROUND(o.peer_percentile * 100, 1)::TEXT, '')),
        '{{entity_kind}}',     COALESCE(o.entity_kind, '')),
        '{{peer_bucket}}',     COALESCE(o.peer_bucket, ''))
                                                                 AS rendered_explanation,

    sc.calibration_basis                                         AS severity_basis,
    sc.precedent_url                                             AS severity_precedent_url,
    sc.precedent_summary                                         AS severity_precedent_summary,

    REPLACE(REPLACE(
        COALESCE(eut.url_template, o.evidence_url),
        '{{entity_id}}', o.entity_id),
        '{{cycle}}',     o.cycle)
                                                                 AS upstream_verify_url,
    eut.button_label                                             AS upstream_verify_label,
    eut.upstream_source                                          AS upstream_source,

    he.formula_version                                           AS formula_version
FROM   derived.fraud_signal_observation        o
LEFT JOIN cand_meta                            cand
       ON o.entity_kind = 'candidate'
      AND cand.cycle    = o.cycle
      AND cand.cand_id  = o.entity_id
LEFT JOIN cmte_meta                            cmte
       ON o.entity_kind = 'committee'
      AND cmte.cycle    = o.cycle
      AND cmte.cmte_id  = o.entity_id
LEFT JOIN treas_meta                           treas
       ON o.entity_kind     = 'treasurer'
      AND treas.cycle       = o.cycle
      AND treas.treasurer_id = UPPER(TRIM(o.entity_id))
LEFT JOIN ref.fraud_signal_human_explanation        he   ON he.signal_id  = o.signal_id
LEFT JOIN ref.fraud_signal_severity_calibration     sc   ON sc.signal_id  = o.signal_id
LEFT JOIN ref.fraud_signal_evidence_url_template    eut  ON eut.signal_id = o.signal_id;

COMMENT ON VIEW derived.v_entity_fraud_evidence IS
    'Canonical join from fraud_signal_observation -> rendered plain-English '
    '(all tokens substituted: entity_id, cycle, raw_value, peer_percentile, '
    'entity_kind, peer_bucket) + federal-authority citation + severity '
    'precedent + display metadata + NJ-relevance + upstream-verify URL. '
    'One row per fired signal. The `severity` column now sources from '
    'ref.fraud_signal_severity_calibration (COALESCE fallback to '
    'base-column value); stacks on 2.5.0-master-refresher-consolidation-v1; '
    'mig 095 executes the F4-extension dependency inversion explicitly '
    'punted by mig 088 line 62-65.';


-- ----------------------------------------------------------------------------
-- derived.audit_severity_drift(p_cycle CHAR(4))
--
-- Audit function: returns rows where the refresher-emitted severity (the
-- value in derived.fraud_signal_observation.severity) disagrees with the
-- calibration-table value for the same signal_id, plus an aggregate
-- row-count for context. Returns 0 rows when no drift exists.
--
-- The function is read-only and parallel-safe. Operators can call it
-- ad-hoc (`SELECT * FROM derived.audit_severity_drift('2024')`) or wire
-- it into a Dagster asset-check that alarms when n_obs > 0 for any
-- drifted row.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.audit_severity_drift(p_cycle CHAR(4))
RETURNS TABLE (
    signal_id            TEXT,
    n_obs                INT,
    hardcoded_severity   SMALLINT,
    calibration_severity SMALLINT,
    drifted              BOOLEAN
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT
        o.signal_id::TEXT,
        COUNT(*)::INT                                AS n_obs,
        MAX(o.severity)::SMALLINT                    AS hardcoded_severity,
        sc.severity_level                            AS calibration_severity,
        (MAX(o.severity) IS DISTINCT FROM sc.severity_level)
                                                     AS drifted
    FROM derived.fraud_signal_observation        o
    LEFT JOIN ref.fraud_signal_severity_calibration sc
           ON sc.signal_id = o.signal_id
    WHERE  o.cycle = p_cycle
    GROUP BY o.signal_id, sc.severity_level
    HAVING (MAX(o.severity) IS DISTINCT FROM sc.severity_level)
       OR  sc.severity_level IS NULL
    ORDER BY o.signal_id;
$$;

COMMENT ON FUNCTION derived.audit_severity_drift(CHAR(4)) IS
    'Surfaces signal_ids in the given cycle where the refresher-emitted '
    'severity disagrees with ref.fraud_signal_severity_calibration, plus '
    'any signal_ids that fired observations but have NO calibration row '
    '(via NULL calibration_severity). Returns 0 rows when no drift. '
    'Substrate-honest visibility: after mig 095, drift no longer affects '
    'UI rendering (L2/L3 views read calibration), but the audit function '
    'is the canonical observability surface for operator-driven '
    'reconciliation. Wire into a Dagster asset-check when the platform '
    'has sensors. Formula 2.6.0-severity-dependency-inversion-v1.';


COMMIT;
