-- =============================================================================
-- Migration 091: derived.v_nj_civic_integrity_state_summary
--
-- Cross-pillar surface: lets a Pillar-1 housing-affordability page (any
-- NJ county /housing/[id]) render a substrate-honest "state-wide civic
-- integrity context" callout for a given FEC cycle.
--
-- Why state-level rather than per-county: the crosswalk that maps a
-- committee's mailing-address ZIP -> county_fips is HUD's USPS-Crosswalk
-- file, which lives behind a registration / API key barrier
-- (huduser.gov returns HTTP 202 to anonymous bulk-data requests). We
-- intentionally do NOT fabricate per-county counts when we cannot ground
-- them in an authoritative crosswalk; the deferred-work item to ingest
-- the HUD crosswalk is documented in work_left.txt.
--
-- Substrate-honest framing: the view aggregates over the three
-- entity-kinds whose physical NJ membership we CAN attest from FEC bulk
-- alone:
--   * candidates (cand_office_st = 'NJ')
--   * committees (cmte_st = 'NJ')
--   * address-clusters (entity_id encodes physical state; SPLIT_PART(., '|', 3) = 'NJ')
--
-- Treasurers are NOT counted here because a single treasurer can serve
-- both NJ and non-NJ committees; the existing v_entity_fraud_evidence
-- BOOL_OR-based is_nj flag handles that nuance for the /risk page, but
-- aggregating treasurer counts to a state-level summary would either
-- double-count multi-state treasurers or under-count NJ exposure
-- depending on the dedup choice. The cleaner answer at the cross-pillar
-- surface is candidate + committee + address only.
--
-- Idempotent: CREATE OR REPLACE VIEW. Safe to re-run.
-- =============================================================================

CREATE OR REPLACE VIEW derived.v_nj_civic_integrity_state_summary AS
WITH nj_candidates AS (
    SELECT
        c.cycle,
        c.cand_id,
        COALESCE(r.n_signals_fired, 0) AS n_signals_fired,
        COALESCE(r.risk_score, 0)::NUMERIC AS risk_score
    FROM raw.fec_candidate c
    LEFT JOIN derived.v_entity_fraud_risk r
      ON r.entity_kind = 'candidate'
     AND r.cycle = c.cycle
     AND r.entity_id = c.cand_id
    WHERE c.cand_office_st = 'NJ'
),
nj_committees AS (
    SELECT
        cm.cycle,
        cm.cmte_id,
        COALESCE(r.n_signals_fired, 0) AS n_signals_fired,
        COALESCE(r.risk_score, 0)::NUMERIC AS risk_score
    FROM raw.fec_committee cm
    LEFT JOIN derived.v_entity_fraud_risk r
      ON r.entity_kind = 'committee'
     AND r.cycle = cm.cycle
     AND r.entity_id = cm.cmte_id
    WHERE cm.cmte_st = 'NJ'
),
nj_addresses AS (
    SELECT
        r.cycle,
        r.entity_id,
        r.n_signals_fired,
        r.risk_score::NUMERIC AS risk_score
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
    )::INT AS total_nj_entities_with_signals,
    -- Headline max-score across all NJ-relevant kinds
    GREATEST(
        (SELECT COALESCE(MAX(nc.risk_score), 0) FROM nj_candidates nc
         WHERE nc.cycle = s.cycle),
        (SELECT COALESCE(MAX(nm.risk_score), 0) FROM nj_committees nm
         WHERE nm.cycle = s.cycle),
        (SELECT COALESCE(MAX(na.risk_score), 0) FROM nj_addresses na
         WHERE na.cycle = s.cycle)
    )::NUMERIC AS max_nj_risk_score
FROM (
    SELECT DISTINCT cycle FROM raw.fec_candidate WHERE cand_office_st = 'NJ'
    UNION
    SELECT DISTINCT cycle FROM raw.fec_committee WHERE cmte_st = 'NJ'
) s
ORDER BY cycle DESC;

COMMENT ON VIEW derived.v_nj_civic_integrity_state_summary IS
'State-wide NJ civic-integrity roll-up per FEC cycle. Used by the
cross-pillar callout on /housing/[id] to surface "for cycle X there are
N NJ-relevant federal entities with structural-anomaly signals firing"
without claiming per-county granularity (which would require the HUD
USPS-County crosswalk, currently not loaded). Aggregates candidates
(cand_office_st=NJ), committees (cmte_st=NJ), and address clusters
(entity_id physical state token = NJ); excludes treasurers because the
state-level dedup of multi-state treasurers is ambiguous.';
