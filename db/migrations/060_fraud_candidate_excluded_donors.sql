-- ============================================================================
-- Migration: 060_fraud_candidate_excluded_donors
--
-- TIER 4 v3 / FRAUD-F5d candidate-side projection of donor_on_leie
-- (signal_id 'candidate_funded_by_excluded_donors'). SIXTH cross-
-- source signal in the fraud engine.
--
-- WHAT THIS SIGNAL ANSWERS
-- ------------------------
-- "Which candidates received money from federally-excluded
-- individuals?" The candidate-side projection of donor_on_leie
-- (migration 059): it rolls a flagged donor's contributions through
-- fec_committee.cand_id to fec_candidate, surfacing candidates whose
-- campaigns are funded by people on the federal exclusion list.
--
-- This signal is the LEIE analogue of
-- candidate_funded_by_nj_contractor_employees (migration 057).
-- Same SQL shape, same bucketing (office x state), same CUME_DIST
-- per-bucket percentile, same fec_committee.cand_id-IS-NOT-NULL
-- filter for principal / authorized committees.
--
-- The two signals are intentionally parallel because they answer
-- the same analyst question against two different upstream
-- substrates:
--    057: donor's EMPLOYER overlaps a federal contractor.
--    060: donor THEMSELVES is on the federal exclusion list.
-- A candidate who fires on both is doubly concerning; the L3a
-- composite score sums their severities and surfaces the signature.
--
-- SEVERITY ESCALATION VS. 057
-- ---------------------------
-- 057 (contractor-employee donations) is severity 3 (HIGH): a donor
-- working at a federal contractor is correlative -- the contracting
-- relationship is at-arm's-length employment.
-- 060 is severity 5 (CRITICAL): a LEIE-excluded donor is themselves
-- on the federal exclusion list -- there is no at-arm's-length
-- distance. A candidate accepting that money is a direct procurement-
-- influence concern, not a downstream-correlation concern.
--
-- ORDERING CONTRACT
-- -----------------
-- This signal MUST run after derived.refresh_signal_donor_on_leie
-- for the same cycle. It reads the (cycle, signal_id='donor_on_leie')
-- slice of derived.fraud_signal_observation as input. The Dagster
-- asset's dep edge enforces the order; this header documents the
-- contract for readers running the SQL directly.
--
-- WHAT'S OUT OF SCOPE FOR V1
-- --------------------------
-- 1. Joint Fundraising Committee / Leadership PAC unrolling --
--    requires raw.fec_ccl ingestion. Same gap as 057. JFCs and
--    Leadership PACs have fec_committee.cand_id IS NULL and
--    therefore drop out here. A candidate who actually received
--    LEIE-donor money via a JFC will not show up in this signal
--    until raw.fec_ccl is added to the platform.
-- 2. Independent expenditures (Super PAC spending FOR a candidate).
--    A Super PAC that received LEIE-donor money and then spent on
--    behalf of a candidate is, in the FEC schema, two arm's-length
--    transactions with no direct cand_id linkage. Surfacing the
--    candidate requires an FEC F-5/F-7 expenditure ingester (raw
--    table currently absent).
-- 3. Time-of-contribution proximity. We aggregate the entire cycle.
--    A future iteration may flag "money concentrated in the 30 days
--    before a key procurement vote" once roll-call data is ingested.
-- 4. Multi-cycle aggregation. A "candidate persistently funded by
--    excluded donors across N cycles" pattern is a downstream view
--    consuming this signal.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- REFRESHER: derived.refresh_signal_candidate_funded_by_excluded_donors(cycle)
-- ----------------------------------------------------------------------------
-- Mirrors derived.refresh_signal_candidate_funded_by_nj_contractor_employees
-- (migration 057) byte-for-byte except for:
--   * The matched-set CTE reads from signal_id='donor_on_leie',
--     producing canonical "LAST|FIRST" donor keys instead of
--     canonical employer names.
--   * The fec_contribution join is on
--     f_canonical_lastfirst_from_fec(c.name) instead of
--     f_canonical_employer_name(c.employer).
--   * severity is 5 (CRITICAL) instead of 3 (HIGH).
--   * signal_id and evidence_url slug are renamed accordingly.
--
-- The parallel structure is deliberate: any future analyst tooling
-- that wants to enumerate "candidate-side projections of donor-side
-- signals" can iterate over a known shape.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_candidate_funded_by_excluded_donors(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'candidate_funded_by_excluded_donors';

    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    WITH matched_donors AS (
        -- Pull the LEIE-overlap donor set produced by 059.
        -- Reading from L1 (rather than recomputing the canonicalizer
        -- against LEIE) means this signal cannot drift from 059's
        -- keying: any change to f_canonical_lastfirst_from_fec or
        -- to the LEIE active-window filter is reflected
        -- automatically.
        SELECT entity_id AS canonical_donor
        FROM derived.fraud_signal_observation
        WHERE cycle = p_cycle
          AND signal_id = 'donor_on_leie'
    ),
    flagged_contributions AS (
        -- Per-contribution rows where the donor's canonical name
        -- matches a LEIE-flagged individual. Filter set:
        --   * memo_cd != 'X' (FEC double-count exclusion)
        --   * transaction_amt > 0 (positive contributions only)
        -- Note that the upstream 059 refresher already filters
        -- memo + positive at the donor-aggregation level; we re-
        -- apply the same filters here at the per-contribution
        -- level so the candidate-roll-up uses identical accounting.
        -- A discrepancy between the two filter sets would mean a
        -- donor's flagged total in 059 doesn't equal the sum of
        -- their flagged contributions here, which would confuse
        -- the analyst.
        SELECT
            c.cycle,
            c.cmte_id,
            c.transaction_amt,
            c.name
        FROM raw.fec_contribution c
        JOIN matched_donors m
          ON derived.f_canonical_lastfirst_from_fec(c.name)
             = m.canonical_donor
        WHERE c.cycle = p_cycle
          AND c.name IS NOT NULL
          AND derived.f_canonical_lastfirst_from_fec(c.name)
              IS NOT NULL
          AND (c.memo_cd IS NULL OR c.memo_cd <> 'X')
          AND c.transaction_amt > 0
    ),
    per_candidate AS (
        -- Roll up to the principal-campaign-committee level via
        -- fec_committee.cand_id, then to fec_candidate for the
        -- office / state bucket. cmte.cand_id is non-NULL only for
        -- principal and authorized committees -- contributions to
        -- non-candidate committees (Super PACs, JFCs, etc.) drop
        -- out here. That is the substrate-honest definition of
        -- "candidate received the money": joint-fundraising and
        -- leadership-PAC linkages are secondary FEC linkages we
        -- have not ingested (see header).
        SELECT
            cand.cycle,
            cand.cand_id,
            cand.cand_office,
            cand.cand_office_st,
            SUM(fc.transaction_amt)::NUMERIC          AS sum_amt,
            COUNT(*)                                  AS n_contributions,
            COUNT(DISTINCT fc.name)                   AS n_donor_name_variants,
            COUNT(DISTINCT
                  derived.f_canonical_lastfirst_from_fec(fc.name))
                                                      AS n_distinct_donors
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
            sum_amt, n_contributions, n_donor_name_variants,
            n_distinct_donors,
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
        'candidate_funded_by_excluded_donors'                  AS signal_id,
        sum_amt                                                AS raw_value,
        5::SMALLINT                                            AS severity,
        'office=' || COALESCE(cand_office, '?')
            || '|state=' || COALESCE(cand_office_st, '?')      AS peer_bucket,
        pctile                                                 AS peer_percentile,
        '/fec/risk/entities/candidate/' || cand_id
            || '?signal=candidate_funded_by_excluded_donors'
            || '&cycle=' || p_cycle                            AS evidence_url
    FROM ranked
    -- Defense-in-depth: only candidates with non-zero received-
    -- money show up. The transaction_amt > 0 filter upstream
    -- already guarantees per_candidate.sum_amt > 0, but we keep
    -- this WHERE clause as a tripwire for any future change to
    -- the filter chain.
    WHERE sum_amt > 0;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_candidate_funded_by_excluded_donors(CHAR) IS
    'Refresh candidate_funded_by_excluded_donors for one FEC cycle. '
    'MUST run after refresh_signal_donor_on_leie for the same cycle '
    '(it reads that signal''s L1 rows). Idempotent on its (cycle, '
    'signal_id) slice. One row per (cycle, cand_id) where the '
    'candidate''s principal/authorized committees received >0 from '
    'LEIE-excluded donors. raw_value = sum of positive contribution '
    'amounts (memo_cd=X excluded). severity=5 (CRITICAL); '
    'peer_percentile is CUME_DIST per (cand_office, cand_office_st) '
    'bucket. Returns the number of rows inserted.';
