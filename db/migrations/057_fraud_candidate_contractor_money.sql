-- ============================================================================
-- Migration: 057_fraud_candidate_contractor_money
--
-- TIER 4 v3 / FRAUD-F1 (signal layer): cross-source signal
-- `candidate_funded_by_nj_contractor_employees`. The candidate-side
-- projection of `donor_employed_by_nj_contractor` (migration 056).
--
-- WHAT IT IS
-- ----------
-- For each FEC candidate in the cycle, sum the contributions whose
-- donor's self-reported employer canonicalizes to a NJ-pop USAspending
-- contractor. The signal grain is one row per (cycle, cand_id) where
-- raw_value > 0. peer_percentile is bucketed by (cand_office,
-- cand_office_st) so a House-NJ candidate is ranked against other
-- House-NJ candidates, not against presidential candidates with
-- nationwide donor pools.
--
-- WHY THIS PAIRS WITH SIGNAL 056
-- ------------------------------
-- 056 surfaces *donor clusters* (employers) that overlap with NJ
-- federal contractors. By itself, that signal answers "which
-- contractors' employees are politically active?" But the analyst's
-- canonical question is "WHICH CANDIDATES received money from
-- contractor employees?" -- the signal that flags potential pay-to-
-- play. This migration adds that candidate-side projection.
--
-- WHY WE DON'T DEDUCE FROM 056'S WRITTEN ROWS
-- -------------------------------------------
-- 056 writes one row per (cycle, canonical_employer). The candidate-
-- side rollup needs the contribution-level join to identify SPECIFIC
-- transactions, then group by candidate. We re-derive the matched
-- employer set from 056's L1 output (a one-line CTE), then join
-- through fec_contribution -> fec_committee -> fec_candidate.
--
-- DEPENDENCY ORDER (substrate-honesty)
-- ------------------------------------
-- This refresher MUST run after 056's refresher in the same cycle:
--    1. derived.refresh_signal_donor_employed_by_nj_contractor(cycle)
--       -> populates the donor_cluster rows we read from
--    2. derived.refresh_signal_candidate_funded_by_nj_contractor_employees
--       (cycle) -> reads them; produces candidate rows
-- Dagster enforces the dependency via asset_dep edges (see
-- orchestration/assets.py).
--
-- WHAT'S OUT OF SCOPE FOR V1
-- --------------------------
-- 1. Multi-cycle aggregation. The signal is per-cycle. A "candidate
--    persistently funded by contractor employees across 4 cycles"
--    pattern is a downstream view that consumes this signal.
-- 2. Sub-cycle time slicing. We sum across the entire cycle; we do
--    not flag "money concentrated in the 30 days before a key
--    procurement vote" (that requires roll-call data, separate
--    ingester).
-- 3. Joint Fundraising Committee (JFC) / Leadership PAC unrolling.
--    Contributions to JFCs / Leadership PACs map to CANDIDATES via
--    a separate FEC linkage (fec_ccl) we have not yet ingested. We
--    join only on cmte.cand_id (principal campaign committee +
--    authorized committees), so JFC contributions credited to a
--    candidate via secondary linkage are missed. Documented; not a
--    correctness bug.
-- 4. Independent expenditures (Super PAC ads supporting a candidate
--    without coordinating). Those are not "contributions to the
--    candidate" in the strict campaign-finance sense; they are a
--    separate signal.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- REFRESHER: derived.refresh_signal_candidate_funded_by_nj_contractor_employees
-- ----------------------------------------------------------------------------
-- Idempotent on its own (cycle, signal_id) slice. Reads the matched-
-- employer set from the L1 table (no separate canonical-name
-- recomputation), joins through fec_contribution -> fec_committee ->
-- fec_candidate, sums positive amounts per candidate, percentile-ranks
-- per (office, state) bucket via CUME_DIST.
--
-- BUCKET CHOICE: (cand_office, cand_office_st)
--    * H-NJ candidates rank against each other (~13 candidates)
--    * S-NJ candidates rank against each other (small N)
--    * P-US presidential candidates rank against each other
-- This is the same bucket structure the existing FEC structural
-- signals (treasurer_concentration, candidate_no_pcc) use, so the L2
-- pivot can show analysts apples-to-apples comparisons.
--
-- We use CUME_DIST (not PERCENT_RANK) so a single-candidate bucket
-- (rare but possible for a special-election cycle slice) yields 1.0
-- rather than the degenerate 0/0=0.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_candidate_funded_by_nj_contractor_employees(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'candidate_funded_by_nj_contractor_employees';

    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    WITH matched_employers AS (
        -- Pull the contractor-overlap employer set produced by 056.
        -- Reading from L1 (rather than recomputing the canonicalizer
        -- against USAspending) means this signal cannot drift from
        -- 056's keying: any change to the canonicalizer or the
        -- USAspending freshness window is reflected automatically.
        SELECT entity_id AS canonical_employer
        FROM derived.fraud_signal_observation
        WHERE cycle = p_cycle
          AND signal_id = 'donor_employed_by_nj_contractor'
    ),
    flagged_contributions AS (
        -- Per-contribution rows where the donor's employer matches a
        -- flagged cluster. Mirrors 056's filter set:
        --   * memo_cd != 'X' (FEC double-count exclusion)
        --   * transaction_amt > 0 (positive contributions only)
        SELECT
            c.cycle,
            c.cmte_id,
            c.transaction_amt,
            c.name,
            c.employer
        FROM raw.fec_contribution c
        JOIN matched_employers m
          ON derived.f_canonical_employer_name(c.employer) = m.canonical_employer
        WHERE c.cycle = p_cycle
          AND c.employer IS NOT NULL
          AND derived.f_canonical_employer_name(c.employer) <> ''
          AND (c.memo_cd IS NULL OR c.memo_cd <> 'X')
          AND c.transaction_amt > 0
    ),
    per_candidate AS (
        -- Roll up to the principal-campaign-committee level via
        -- fec_committee.cand_id, then to fec_candidate for the office /
        -- state bucket. cmte.cand_id is non-NULL only for principal
        -- and authorized committees -- contributions to non-candidate
        -- committees (Super PACs, JFCs, etc.) drop out here. That is
        -- the substrate-honest definition of "candidate received the
        -- money": joint-fundraising and leadership-PAC linkages are
        -- secondary FEC linkages we have not ingested (see header).
        SELECT
            cand.cycle,
            cand.cand_id,
            cand.cand_office,
            cand.cand_office_st,
            SUM(fc.transaction_amt)::NUMERIC AS sum_amt,
            COUNT(*)                         AS n_contributions,
            COUNT(DISTINCT fc.name)          AS n_unique_donors,
            COUNT(DISTINCT
                  derived.f_canonical_employer_name(fc.employer))
                                             AS n_distinct_employers
        FROM flagged_contributions fc
        JOIN raw.fec_committee cmte
          ON cmte.cycle = fc.cycle
         AND cmte.cmte_id = fc.cmte_id
        JOIN raw.fec_candidate cand
          ON cand.cycle = cmte.cycle
         AND cand.cand_id = cmte.cand_id
        WHERE cmte.cand_id IS NOT NULL
        GROUP BY cand.cycle, cand.cand_id,
                 cand.cand_office, cand.cand_office_st
    ),
    ranked AS (
        SELECT
            cycle, cand_id, cand_office, cand_office_st,
            sum_amt, n_contributions, n_unique_donors,
            n_distinct_employers,
            -- CUME_DIST per-bucket: a candidate at the top of their
            -- (office, state) cohort gets 1.0; bottom gets 1/N.
            CUME_DIST() OVER (
                PARTITION BY cand_office, cand_office_st
                ORDER BY sum_amt
            ) AS pctile
        FROM per_candidate
    )
    SELECT
        p_cycle                                                AS cycle,
        'candidate'                                            AS entity_kind,
        cand_id                                                AS entity_id,
        'candidate_funded_by_nj_contractor_employees'          AS signal_id,
        sum_amt                                                AS raw_value,
        3::SMALLINT                                            AS severity,
        'office=' || COALESCE(cand_office, '?')
            || '|state=' || COALESCE(cand_office_st, '?')      AS peer_bucket,
        pctile                                                 AS peer_percentile,
        '/fec/risk/entities/candidate/' || cand_id
            || '?signal=candidate_funded_by_nj_contractor_employees'
            || '&cycle=' || p_cycle                            AS evidence_url
    FROM ranked
    -- Floor: only candidates with non-zero received-money show up. A
    -- per_candidate row with sum_amt = 0 is impossible given the
    -- transaction_amt > 0 filter upstream, but we keep the WHERE
    -- clause as defense-in-depth.
    WHERE sum_amt > 0;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_candidate_funded_by_nj_contractor_employees(CHAR) IS
    'Refresh candidate_funded_by_nj_contractor_employees for one FEC '
    'cycle. MUST run after refresh_signal_donor_employed_by_nj_contractor '
    'for the same cycle (it reads that signal''s L1 rows). Idempotent '
    'on its (cycle, signal_id) slice. One row per (cycle, cand_id) '
    'where the candidate''s principal/authorized committees received '
    '>0 from contractor-employed donors. raw_value = sum of positive '
    'contribution amounts (memo_cd=X excluded). peer_percentile is '
    'CUME_DIST per (cand_office, cand_office_st) bucket. Returns the '
    'number of rows inserted.';
