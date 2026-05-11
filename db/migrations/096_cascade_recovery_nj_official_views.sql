-- =============================================================================
-- Migration 096: cascade-recovery -- recreate the two NJ-relevant views
--                that mig 095's DROP VIEW ... CASCADE transitively dropped
--
-- BACKGROUND
-- ----------
-- Mig 095 (severity-dependency-inversion) DROPped derived.v_entity_fraud_risk
-- with CASCADE so derived.v_entity_fraud_features could be recreated with
-- a new shape (the CTE that pulls severity from
-- ref.fraud_signal_severity_calibration). The CASCADE drop transitively
-- removed every view that LEFT JOINed v_entity_fraud_risk:
--
--   * derived.v_nj_federal_officials               (originally mig 090)
--   * derived.v_nj_civic_integrity_state_summary   (originally mig 091)
--
-- Mig 095 recreated v_entity_fraud_risk itself but did NOT recreate
-- these two downstream views, because their inlining would have
-- bloated mig 095 and the dependency was not foreseen at draft time.
-- After mig 095 applied to production, /risk?scope=nj rendered
-- "relation derived.v_nj_federal_officials does not exist" because the
-- /risk page loads NJ-relevant officials from v_nj_federal_officials.
--
-- ROOT-CAUSE LESSON
-- -----------------
-- The substrate-honest pattern for any DROP ... CASCADE in a migration:
-- run `\d+` on each transitively-dropped view BEFORE the CASCADE, and
-- include their recreate-statements in the migration. The pg catalog
-- query that surfaces transitive dependents is:
--
--   SELECT dependent.relname
--   FROM   pg_depend dep
--   JOIN   pg_rewrite rw     ON dep.objid     = rw.oid
--   JOIN   pg_class dependent ON rw.ev_class  = dependent.oid
--   JOIN   pg_class source   ON dep.refobjid = source.oid
--   WHERE  source.relname = '<view_being_dropped>'
--     AND  dependent.relname != source.relname;
--
-- Future migrations that touch v_entity_fraud_features or
-- v_entity_fraud_risk MUST run this query first.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- Bit-identical recreation of v_nj_federal_officials (per mig 090's
-- latest definition) and v_nj_civic_integrity_state_summary (per
-- mig 091). Both use CREATE OR REPLACE / DROP-IF-EXISTS so they are
-- idempotent and can be re-applied without harm.
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
    '2.6.1-cascade-recovery-nj-official-views-v1',
    'Recreates derived.v_nj_federal_officials and '
    'derived.v_nj_civic_integrity_state_summary that mig 095''s DROP '
    'VIEW ... CASCADE transitively dropped. Both views LEFT JOIN '
    'derived.v_entity_fraud_risk; CASCADE detected and removed them but '
    'mig 095 did not include their recreate-statements. This migration '
    'closes that gap. Bit-identical to the latest definitions in '
    'migs 090 + 091.',
    '2026-05-11'::DATE,
    'Hotfix follow-up to 2.6.0-severity-dependency-inversion-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- derived.v_nj_federal_officials -- bit-identical to mig 090
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS derived.v_nj_federal_officials;

CREATE VIEW derived.v_nj_federal_officials AS
WITH base AS (
    SELECT
        c.cycle,
        c.cand_id,
        c.cand_name,
        c.cand_office,
        c.cand_office_st,
        c.cand_office_district,
        c.cand_pty_affiliation,
        c.cand_ici,
        c.cand_status,
        c.cand_election_yr,
        -- Tenure proxy: in how many earlier cycles did this exact
        -- cand_id run as a true incumbent (ici='I' AND status='C')?
        -- Higher = more likely the candidate is the actual sitting
        -- member.
        (
            SELECT COUNT(*)
            FROM raw.fec_candidate c2
            WHERE c2.cand_id = c.cand_id
              AND c2.cycle < c.cycle
              AND c2.cand_ici = 'I'
              AND c2.cand_status = 'C'
        )::INT AS prior_incumbent_cycles,
        (
            SELECT COUNT(*)
            FROM raw.fec_candidate c3
            WHERE c3.cand_id = c.cand_id
              AND c3.cycle <= c.cycle
        )::INT AS total_cycles_filed
    FROM raw.fec_candidate c
    WHERE c.cand_office_st = 'NJ'
      AND c.cand_office IN ('S', 'H')
      AND c.cand_ici = 'I'
      -- Allow 'N' (incumbent not seeking re-election this cycle) and
      -- 'F' (future candidate) so we catch sitting members who do
      -- not have an active 'C' filing.
      AND c.cand_status IN ('C', 'N', 'F')
),
ranked AS (
    SELECT
        b.*,
        ROW_NUMBER() OVER (
            PARTITION BY
                b.cycle,
                b.cand_office,
                CASE
                    -- Senate has 2 seats per state both at district
                    -- '00'; use cand_id so each senator is its own
                    -- partition.
                    WHEN b.cand_office = 'S' THEN b.cand_id
                    ELSE b.cand_office_district
                END
            ORDER BY
                b.prior_incumbent_cycles DESC,
                (b.cand_status = 'C')::INT DESC,
                b.total_cycles_filed DESC,
                b.cand_id ASC
        ) AS rn
    FROM base b
)
SELECT
    r.cycle,
    r.cand_id                                                AS entity_id,
    r.cand_name                                              AS official_name,
    r.cand_office                                            AS office_code,
    r.cand_office_district                                   AS office_district,
    r.cand_pty_affiliation                                   AS office_party,
    r.cand_ici                                               AS incumbent_status,
    r.cand_election_yr                                       AS election_year,
    r.prior_incumbent_cycles,
    CASE r.cand_office
        WHEN 'S' THEN 'U.S. Senator'
        WHEN 'H' THEN 'U.S. Representative'
        WHEN 'P' THEN 'U.S. President'
        ELSE r.cand_office
    END                                                      AS office_label,
    COALESCE(risk.risk_score, 0)::NUMERIC                    AS risk_score,
    COALESCE(risk.n_signals_fired, 0)                        AS n_signals_fired,
    COALESCE(risk.signals_fired, ARRAY[]::TEXT[])            AS signals_fired,
    COALESCE(risk.max_severity::INT, 0)                      AS max_severity,
    risk.last_observation_at
FROM ranked r
LEFT JOIN derived.v_entity_fraud_risk risk
  ON  risk.entity_kind = 'candidate'
 AND  risk.cycle       = r.cycle
 AND  risk.entity_id   = r.cand_id
WHERE r.rn = 1
ORDER BY r.cand_office DESC,
         r.cand_office_district NULLS FIRST,
         r.cand_id ASC;

COMMENT ON VIEW derived.v_nj_federal_officials IS
'NJ federal incumbents (Senate + House) for a given FEC cycle, '
'deduplicated per (cycle, office, district) using a tenure proxy '
'(count of prior cycles where the cand_id actually ran as ici=I AND '
'status=C). Catches sitting members who are not seeking re-election '
'in the current cycle (e.g. Sherrill running for governor). Senate '
'partitions by cand_id since both NJ senators share '
'office_district=00. Substrate-honest: relies on FEC self-declaration '
'and historical filings; for newcomers (prior_incumbent_cycles=0) '
'the result is only as accurate as FEC Form 2 self-reporting. '
'Recreated by mig 096 after mig 095''s CASCADE drop; bit-identical '
'to mig 090.';


-- ----------------------------------------------------------------------------
-- derived.v_nj_civic_integrity_state_summary -- bit-identical to mig 091
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_nj_civic_integrity_state_summary AS
WITH nj_candidates AS (
    SELECT
        c.cycle,
        c.cand_id,
        COALESCE(r.n_signals_fired, 0)         AS n_signals_fired,
        COALESCE(r.risk_score, 0)::NUMERIC     AS risk_score
    FROM raw.fec_candidate c
    LEFT JOIN derived.v_entity_fraud_risk r
      ON  r.entity_kind = 'candidate'
     AND  r.cycle       = c.cycle
     AND  r.entity_id   = c.cand_id
    WHERE c.cand_office_st = 'NJ'
),
nj_committees AS (
    SELECT
        cm.cycle,
        cm.cmte_id,
        COALESCE(r.n_signals_fired, 0)         AS n_signals_fired,
        COALESCE(r.risk_score, 0)::NUMERIC     AS risk_score
    FROM raw.fec_committee cm
    LEFT JOIN derived.v_entity_fraud_risk r
      ON  r.entity_kind = 'committee'
     AND  r.cycle       = cm.cycle
     AND  r.entity_id   = cm.cmte_id
    WHERE cm.cmte_st = 'NJ'
),
nj_addresses AS (
    SELECT
        r.cycle,
        r.entity_id,
        r.n_signals_fired,
        r.risk_score::NUMERIC                  AS risk_score
    FROM derived.v_entity_fraud_risk r
    WHERE r.entity_kind = 'address'
      AND SPLIT_PART(r.entity_id, '|', 3) = 'NJ'
)
SELECT
    cycle,
    -- Candidate roll-up
    (SELECT COUNT(*)::INT FROM nj_candidates nc WHERE nc.cycle = s.cycle)
        AS n_candidates_total,
    (SELECT COUNT(*)::INT FROM nj_candidates nc
     WHERE nc.cycle = s.cycle AND nc.n_signals_fired > 0)
        AS n_candidates_with_signals,
    (SELECT COALESCE(MAX(nc.risk_score), 0)::NUMERIC FROM nj_candidates nc
     WHERE nc.cycle = s.cycle)
        AS max_candidate_risk_score,
    -- Committee roll-up
    (SELECT COUNT(*)::INT FROM nj_committees nm WHERE nm.cycle = s.cycle)
        AS n_committees_total,
    (SELECT COUNT(*)::INT FROM nj_committees nm
     WHERE nm.cycle = s.cycle AND nm.n_signals_fired > 0)
        AS n_committees_with_signals,
    (SELECT COALESCE(MAX(nm.risk_score), 0)::NUMERIC FROM nj_committees nm
     WHERE nm.cycle = s.cycle)
        AS max_committee_risk_score,
    -- Address-cluster roll-up (no "total" because the universe of NJ
    -- physical addresses is uncountable; we only see the ones where
    -- signals fired)
    (SELECT COUNT(*)::INT FROM nj_addresses na WHERE na.cycle = s.cycle)
        AS n_addresses_with_signals,
    (SELECT COALESCE(MAX(na.risk_score), 0)::NUMERIC FROM nj_addresses na
     WHERE na.cycle = s.cycle)
        AS max_address_risk_score,
    -- Aggregate headline: total NJ-relevant entities with >=1 signal
    (
      (SELECT COUNT(*) FROM nj_candidates nc
       WHERE nc.cycle = s.cycle AND nc.n_signals_fired > 0)
      + (SELECT COUNT(*) FROM nj_committees nm
       WHERE nm.cycle = s.cycle AND nm.n_signals_fired > 0)
      + (SELECT COUNT(*) FROM nj_addresses na WHERE na.cycle = s.cycle)
    )::INT                                                   AS total_nj_entities_with_signals,
    -- Headline max-score across all NJ-relevant kinds
    GREATEST(
        (SELECT COALESCE(MAX(nc.risk_score), 0) FROM nj_candidates nc
         WHERE nc.cycle = s.cycle),
        (SELECT COALESCE(MAX(nm.risk_score), 0) FROM nj_committees nm
         WHERE nm.cycle = s.cycle),
        (SELECT COALESCE(MAX(na.risk_score), 0) FROM nj_addresses na
         WHERE na.cycle = s.cycle)
    )::NUMERIC                                               AS max_nj_risk_score
FROM (
    SELECT DISTINCT cycle FROM raw.fec_candidate WHERE cand_office_st = 'NJ'
    UNION
    SELECT DISTINCT cycle FROM raw.fec_committee WHERE cmte_st = 'NJ'
) s
ORDER BY cycle DESC;

COMMENT ON VIEW derived.v_nj_civic_integrity_state_summary IS
'State-wide NJ civic-integrity roll-up per FEC cycle. Used by the '
'cross-pillar callout on /housing/[id] to surface "for cycle X there '
'are N NJ-relevant federal entities with structural-anomaly signals '
'firing" without claiming per-county granularity (which would require '
'the HUD USPS-County crosswalk, currently not loaded). Aggregates '
'candidates (cand_office_st=NJ), committees (cmte_st=NJ), and address '
'clusters (entity_id LIKE %|NJ|%). Recreated by mig 096 after mig '
'095''s CASCADE drop; bit-identical to mig 091.';


COMMIT;
