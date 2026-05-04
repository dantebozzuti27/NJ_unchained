-- ============================================================================
-- Migration: 066_fraud_candidate_funded_by_sam_excluded_donors
--
-- TIER 4 v3 / FRAUD-F2 candidate-side projection of donor_on_sam
-- (signal_id 'candidate_funded_by_sam_excluded_donors'). EIGHTH cross-
-- source signal in the fraud engine; parallel to 060
-- (candidate_funded_by_excluded_donors) which projects donor_on_leie.
--
-- WHAT THIS SIGNAL ANSWERS
-- ------------------------
-- "Which candidates received money from individuals on SAM.gov's
-- federal exclusion list?" The candidate-side projection of
-- donor_on_sam (migration 065): it rolls a SAM-flagged donor's
-- contributions through fec_committee.cand_id to fec_candidate,
-- surfacing candidates whose campaigns are funded by people barred
-- from federal participation (DOJ, OFAC, GSA, NIH/NSF, DOE, plus
-- HHS-OIG mirror -- the full federal exclusion population, not just
-- healthcare).
--
-- COMPLEMENTS, DOES NOT DUPLICATE, candidate_funded_by_excluded_donors
-- --------------------------------------------------------------------
-- 060 fires on candidates funded by LEIE-excluded donors (HHS-OIG
-- healthcare exclusions only).
-- 066 fires on candidates funded by SAM-excluded donors (every
-- federal excluding agency).
-- A donor on BOTH lists fires both donor_on_leie and donor_on_sam,
-- and that means the candidate they funded fires both
-- candidate_funded_by_excluded_donors and
-- candidate_funded_by_sam_excluded_donors. The L2 pivot aggregates
-- both signals into the candidate's row, signal_families[] contains
-- both leie_bearing and sam_bearing, and fraud_risk_score grants
-- the multi-family diversity bonus -- the candidate-side mirror of
-- the dual-fire amplification we already get for the donor.
--
-- SEVERITY 5 (CRITICAL)
-- ---------------------
-- Mirrors 060. A SAM-excluded donor making campaign contributions
-- carries the same procurement-influence concern as a LEIE-excluded
-- one: the donor has been formally barred from federal participation,
-- yet retains discretionary income and political access; the
-- candidate accepting that money is a direct procurement-influence
-- target. SAM's broader scope (DOJ, OFAC, etc.) makes this if
-- anything more salient than the LEIE-only counterpart, not less.
--
-- ORDERING CONTRACT
-- -----------------
-- This signal MUST run after derived.refresh_signal_donor_on_sam
-- for the same cycle. It reads the (cycle, signal_id='donor_on_sam')
-- slice of derived.fraud_signal_observation as input. The Dagster
-- asset's dep edge enforces the order; this header documents the
-- contract for readers running the SQL directly.
--
-- AGE-DECAY DESIGN (mirrors 062's per-contribution decay rewrite)
-- ---------------------------------------------------------------
-- Each contribution decays by its donor's freshest SAM active_date,
-- not the candidate's aggregate (different donors have different
-- exclusion ages). matched_donors_with_active_date enriches the L1
-- matched set with each donor's freshest sam_active_date; the
-- per_candidate CTE applies SUM(transaction_amt * f_leie_age_decay(
-- sam_active_date)) per candidate. The function name is
-- f_leie_age_decay for historical reasons -- it's exclusion-list-
-- agnostic and consumes any DATE.
--
-- WHAT'S OUT OF SCOPE FOR V1
-- --------------------------
-- 1. Joint Fundraising Committee / Leadership PAC unrolling --
--    requires raw.fec_ccl ingestion. Same gap as 057/060. JFCs and
--    Leadership PACs have fec_committee.cand_id IS NULL and
--    therefore drop out here.
-- 2. Independent expenditures (Super PAC spending FOR a candidate).
--    Surfacing the candidate requires an FEC F-5/F-7 expenditure
--    ingester (raw table currently absent).
-- 3. Time-of-contribution proximity. We aggregate the entire cycle.
-- 4. Multi-cycle aggregation. A "candidate persistently funded by
--    SAM-excluded donors across N cycles" pattern is a downstream
--    view consuming this signal.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. Seed the new signal config row
-- ----------------------------------------------------------------------------
-- threshold=$200 mirrors candidate_funded_by_excluded_donors. A
-- candidate funded by exactly one SAM-excluded donor at the FEC
-- itemization minimum ($200) is exactly at the floor of analyst-
-- worthy: any lower is a single sub-itemization donation and the
-- canonicalizer drops it anyway. family='sam_bearing'.
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config (
    signal_id,
    signal_family,
    min_actionable_threshold,
    comment
) VALUES (
    'candidate_funded_by_sam_excluded_donors',
    'sam_bearing',
    200,
    'Candidate-side projection of donor_on_sam (065). raw_value = '
    'SUM(transaction_amt * f_leie_age_decay(sam_active_date)) per '
    '(cycle, cand_id) where the donor canonical key matches an '
    'active SAM individual exclusion. threshold=$200 mirrors the '
    'LEIE-counterpart 060: a single sub-itemization donor would '
    'have been canonicalized to NULL and never reach this signal. '
    'family=sam_bearing so a candidate fired by both this and '
    'candidate_funded_by_excluded_donors earns the multi-family '
    'diversity bonus in fraud_risk_score.'
)
ON CONFLICT (signal_id) DO UPDATE
    SET signal_family            = EXCLUDED.signal_family,
        min_actionable_threshold = EXCLUDED.min_actionable_threshold,
        comment                  = EXCLUDED.comment;


-- ----------------------------------------------------------------------------
-- 2. REFRESHER:
-- derived.refresh_signal_candidate_funded_by_sam_excluded_donors(cycle)
-- ----------------------------------------------------------------------------
-- Mirrors derived.refresh_signal_candidate_funded_by_excluded_donors
-- (062's post-decay rewrite of 060) byte-for-byte except for:
--   * matched_donors CTE reads signal_id='donor_on_sam' instead
--     of 'donor_on_leie'.
--   * The freshest-exclusion CTE reads
--     derived.v_sam_exclusion_individual_canonical (sam_active_date)
--     instead of v_leie_individual_canonical (leie_excldate).
--   * signal_id and evidence_url slug are renamed.
--
-- Severity is the same (5/CRITICAL). Bucket is the same (office x
-- state). CUME_DIST() per-bucket percentile is the same. Same
-- substrate-honesty principle: keys off the L1 row written by 065,
-- not by recomputing the canonicalizer against SAM.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_candidate_funded_by_sam_excluded_donors(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'candidate_funded_by_sam_excluded_donors';

    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    WITH matched_donors AS (
        SELECT entity_id AS canonical_donor
        FROM derived.fraud_signal_observation
        WHERE cycle = p_cycle
          AND signal_id = 'donor_on_sam'
    ),
    sam_canonical_freshest AS (
        -- Mirror of 065: one freshest sam_active_date per canonical
        -- donor key. DISTINCT ON + ORDER BY canonical_key,
        -- sam_active_date DESC NULLS LAST gives the same per-donor
        -- representative SAM record that 065 used to compute the
        -- donor-side raw_value, so the candidate-side decay weight
        -- aligns exactly with the donor-side weight.
        SELECT DISTINCT ON (canonical_key)
            canonical_key,
            sam_active_date
        FROM derived.v_sam_exclusion_individual_canonical
        ORDER BY canonical_key, sam_active_date DESC NULLS LAST
    ),
    matched_donors_with_active_date AS (
        -- LEFT JOIN: a donor in 065's L1 must have a SAM record
        -- (the match was the reason they got into L1), so a NULL
        -- sam_active_date here would be a 065 / SAM-canonical
        -- drift. f_leie_age_decay(NULL) = 1.0 (no decay), so the
        -- contribution is conservatively included at full weight;
        -- the donor_on_sam asset check catches mass drift
        -- separately.
        SELECT
            m.canonical_donor,
            s.sam_active_date
        FROM matched_donors m
        LEFT JOIN sam_canonical_freshest s
          ON s.canonical_key = m.canonical_donor
    ),
    flagged_contributions AS (
        -- Per-contribution rows where the donor's canonical name
        -- matches a SAM-flagged individual. memo_cd != 'X' (FEC
        -- double-count exclusion) + transaction_amt > 0 (positive
        -- only). Mirror of 060's filter set so candidate-side
        -- accounting matches donor-side accounting (a discrepancy
        -- between the two would confuse the analyst).
        SELECT
            c.cycle,
            c.cmte_id,
            c.transaction_amt,
            c.name,
            md.sam_active_date
        FROM raw.fec_contribution c
        JOIN matched_donors_with_active_date md
          ON derived.f_canonical_lastfirst_from_fec(c.name)
             = md.canonical_donor
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
        -- out here. Same substrate-honest definition as 060.
        SELECT
            cand.cycle,
            cand.cand_id,
            cand.cand_office,
            cand.cand_office_st,
            -- Each contribution decays by its donor's freshest
            -- sam_active_date. SUM(decayed) is the candidate-level
            -- "currently-actionable" total receipt from SAM-
            -- excluded donors.
            SUM(
                fc.transaction_amt
                * derived.f_leie_age_decay(fc.sam_active_date)
            )::NUMERIC                                AS sum_amt,
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
        'candidate_funded_by_sam_excluded_donors'              AS signal_id,
        sum_amt                                                AS raw_value,
        5::SMALLINT                                            AS severity,
        'office=' || COALESCE(cand_office, '?')
            || '|state=' || COALESCE(cand_office_st, '?')      AS peer_bucket,
        pctile                                                 AS peer_percentile,
        '/fec/risk/entities/candidate/' || cand_id
            || '?signal=candidate_funded_by_sam_excluded_donors'
            || '&cycle=' || p_cycle                            AS evidence_url
    FROM ranked
    -- Tripwire: only candidates with non-zero received money show
    -- up. The transaction_amt > 0 + decay > 0 filters upstream
    -- already guarantee per_candidate.sum_amt > 0; the WHERE here
    -- defends against any future filter-chain change.
    WHERE sum_amt > 0;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_candidate_funded_by_sam_excluded_donors(CHAR) IS
    'Refresh candidate_funded_by_sam_excluded_donors for one FEC '
    'cycle. MUST run after refresh_signal_donor_on_sam for the '
    'same cycle (it reads that signal''s L1 rows). Idempotent on '
    'its (cycle, signal_id) slice. One row per (cycle, cand_id) '
    'where the candidate''s principal/authorized committees '
    'received >0 (after decay) from SAM-excluded individual donors. '
    'raw_value = sum of (positive contribution amount * '
    'f_leie_age_decay(sam_active_date)). severity=5 (CRITICAL); '
    'peer_percentile is CUME_DIST per (cand_office, cand_office_st) '
    'bucket. Returns the number of rows inserted.';
