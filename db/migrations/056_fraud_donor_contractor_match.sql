-- ============================================================================
-- Migration: 056_fraud_donor_contractor_match
--
-- TIER 4 v3 / FRAUD-F1 (signal layer): cross-source signal
-- `donor_employed_by_nj_contractor`. Joins FEC donor `employer` text
-- against USAspending NJ-pop contractor `recipient_name` on a
-- canonical employer-name key.
--
-- WHY THIS SIGNAL MATTERS
-- -----------------------
-- This is the canonical pay-to-play surface in federal politics:
-- employees of a federal contractor donate to a candidate or PAC, and
-- the contractor wins federal awards from agencies overseen by that
-- candidate. Each individual donation is legal (FEC limits apply); the
-- emergent pattern -- "all 30 senior people at TETRA TECH gave the
-- max to Senator X, whose subcommittee reviews Navy contracts" -- is
-- the substance of public-corruption analysis. The platform does not
-- assert wrongdoing; it surfaces the pattern for analyst review.
--
-- HONEST FRAMING (substrate-honesty)
-- ----------------------------------
-- A match between FEC `employer` and USAspending `recipient_name`
-- means a self-reported employer string at FEC textually equals (after
-- canonicalization) a contractor name. It does not mean:
--    * the donor is currently employed by that contractor (employer
--      is reported once per donation; turnover is invisible),
--    * the donor is involved in the procurement decision (most
--      donors at a contractor are not),
--    * the contractor benefits from the donation (campaign-finance
--      law forbids quid-pro-quo; presence of a donor cluster is not
--      evidence of a bribe).
-- Severity is set to 3 (HIGH but not CRITICAL) to reflect both the
-- signal's substantive importance AND its high false-positive rate.
--
-- WHY ONE SIGNAL ROW PER (CYCLE, EMPLOYER) -- THE `donor_cluster`
-- ENTITY KIND
-- ----------------------------------------------------------------
-- The natural unit of analysis is the *cluster* of donors who all
-- listed the same employer, not a single donor. A cluster of 50
-- donors who collectively gave $400K from one employer is the
-- pattern; a single donor's $25 contribution is noise. The
-- `donor_cluster` entity_kind was reserved in migration 050 exactly
-- for this purpose. The L4 evidence panel will drill from a
-- donor_cluster row to the underlying donations.
--
-- WHY entity_id = canonical employer name (not UEI)
-- -------------------------------------------------
-- We do not have UEIs in FEC data; the FEC `employer` field is
-- self-reported free text. The canonical name is the most stable
-- key that joins the two sides. A future refinement could link
-- canonical-employer to USAspending UEI via a many-to-one mapping
-- learned from co-occurrence in same-address SAM.gov registrations,
-- but that's L5+ territory.
--
-- BUCKET / PERCENTILE SEMANTICS
-- -----------------------------
-- The peer bucket is "all matched donor_clusters in the cycle." Only
-- clusters that match a NJ contractor appear in the table at all
-- (substrate-honesty: absent rows = signal did not fire). Within the
-- matched set, percentile is CUME_DIST() of `raw_value` (sum of
-- positive donation amounts). Rationale:
--    * CUME_DIST returns 1.0 for the top cluster and 1/N for the
--      bottom cluster. PERCENT_RANK would return 0.0 for the bottom
--      and 1.0 for the top, but for a single-row bucket (matched
--      cluster=1) PERCENT_RANK is 0 (degenerate). CUME_DIST is 1.
--    * raw_value = SUM(GREATEST(transaction_amt, 0)) over the
--      cluster's contributions for the cycle, with memo_cd='X' rows
--      filtered (those are sub-line itemizations that double-count
--      their parents per FEC bulk-data convention).
--
-- WHAT'S OUT OF SCOPE FOR V1
-- --------------------------
-- 1. Multi-cycle aggregation. We compute one signal per cycle. A
--    "donor cluster active across 4 cycles" pattern is a separate
--    derived view that consumes this signal.
-- 2. Recipient-side analysis (which CANDIDATE got the money from
--    these donors). The candidate-side projection is a separate
--    signal `candidate_funded_by_nj_contractor_employees` and ships
--    in a future session; it depends on this signal's row set.
-- 3. UEI-based join. FEC has no UEIs; the canonical-name join is
--    the available primitive.
-- 4. Stop-list of common employers (RETIRED, SELF, etc.). The
--    canonicalizer leaves these intact; they will not match any real
--    contractor name in USAspending so they self-filter. We document
--    the failure mode and rely on USAspending-side filtering.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- HELPER: canonicalize a free-text employer / recipient string
-- ----------------------------------------------------------------------------
-- Mirrors `ingestion._base.canonical_employer_name` (Python). Steps:
--    1. Lowercase
--    2. Strip business-entity suffixes (LLC, INC, CORP, ...)
--    3. Collapse non-alphanumeric runs to single space
--    4. Trim
--
-- We intentionally do NOT NFKD-decompose for accent stripping; that
-- would require the `unaccent` extension, which we avoid for $0/Oracle
-- portability per AGENTS.md. Federal contractor and FEC donor employer
-- names are virtually all ASCII.
--
-- IMMUTABLE so the planner can hoist it through joins and use it in
-- expression indexes if we ever need to materialize the canonicalized
-- form. STRICT so NULL input yields NULL output without invoking the
-- function body.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.f_canonical_employer_name(p_text TEXT)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
DECLARE
    s TEXT;
BEGIN
    -- 1. Lowercase.
    s := LOWER(p_text);

    -- 2. Strip business-entity suffixes (mirrors Python _SUFFIX_PATTERNS).
    --    We process them with \b boundaries and an optional trailing dot
    --    so 'LLC', 'L.L.C.', 'L.L.C', and 'LLC.' all collapse to ''.
    --    Order matters less than completeness; any suffix that survives
    --    canonicalization is a false-equivalence between '<name>' and
    --    '<name> <suffix>'. Tested in tests/test_fraud_donor_contractor_match.
    s := REGEXP_REPLACE(
        s,
        '\m(l\.?l\.?c\.?'
        '|l\.?l\.?p\.?'
        '|p\.?l\.?l\.?c\.?'
        '|l\.?p\.?'
        '|p\.?c\.?'
        '|p\.?a\.?'
        '|inc(orporated)?\.?'
        '|corp(oration)?\.?'
        '|co(mpany)?\.?'
        '|ltd\.?'
        '|limited'
        '|holdings?'
        '|group'
        '|the)\M\.?',
        ' ',
        'gi'
    );

    -- 3. Collapse non-alphanumeric runs to a single space.
    s := REGEXP_REPLACE(s, '[^a-z0-9]+', ' ', 'g');

    -- 4. Trim leading/trailing whitespace and collapse internal spaces.
    --    The previous regex already collapses runs; this trims edges.
    s := BTRIM(s);

    -- Empty string is meaningful (caller can filter); we do NOT
    -- convert to NULL here.
    RETURN s;
END;
$$;

COMMENT ON FUNCTION derived.f_canonical_employer_name(TEXT) IS
    'Canonical form of a corporation / employer name suitable for '
    'exact-match grouping. Mirrors ingestion._base.canonical_employer_name '
    '(Python). Returns lowercase, suffix-stripped, alphanumeric-only '
    'form; empty string for inputs that canonicalize to nothing. NULL '
    'in -> NULL out (STRICT). IMMUTABLE so the planner can hoist it.';


-- ----------------------------------------------------------------------------
-- REFRESHER: derived.refresh_signal_donor_employed_by_nj_contractor(cycle)
-- ----------------------------------------------------------------------------
-- Idempotent on its own (cycle, signal_id) slice. Single-pass match
-- between FEC contributions and USAspending NJ-pop active recipients,
-- aggregated to one row per (cycle, canonical_employer).
--
-- DESIGN NOTE: we read from raw.fec_contribution directly (~5M rows
-- per presidential cycle) rather than the public.v_fec_contribution
-- view, because the view's transaction_dt parsing is irrelevant for
-- this aggregation and adds per-row CASE evaluation. We DO replicate
-- the view's `is_memo` filter (memo_cd != 'X') because itemized
-- sub-lines double-count their parent transactions and would inflate
-- raw_value.
--
-- USAspending side reads from derived.v_usaspending_award_active so
-- only currently-active rows (within the 35-day freshness window)
-- contribute to the contractor-name set. A contractor that aged out
-- of the active window has its donor_cluster signal naturally fall
-- off in the next refresh.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_donor_employed_by_nj_contractor(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'donor_employed_by_nj_contractor';

    -- The CTE chain:
    --   us_employers       -- distinct canonical employer names from
    --                         active USAspending NJ recipients
    --   fec_clusters       -- per-employer aggregation of FEC donations
    --                         in p_cycle (raw_value, donor count)
    --   matches            -- inner join us_employers x fec_clusters on
    --                         canonical_employer
    --   ranked             -- CUME_DIST percentile within matched set
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    WITH us_employers AS (
        SELECT DISTINCT
            derived.f_canonical_employer_name(recipient_name) AS canonical_employer
        FROM derived.v_usaspending_award_active
        WHERE recipient_name IS NOT NULL
          AND derived.f_canonical_employer_name(recipient_name) <> ''
    ),
    fec_clusters AS (
        SELECT
            derived.f_canonical_employer_name(c.employer) AS canonical_employer,
            SUM(GREATEST(c.transaction_amt, 0))           AS sum_positive_amt,
            COUNT(*)                                      AS n_donations,
            COUNT(DISTINCT c.name)                        AS n_unique_donors
        FROM raw.fec_contribution c
        WHERE c.cycle = p_cycle
          AND c.employer IS NOT NULL
          AND derived.f_canonical_employer_name(c.employer) <> ''
          AND (c.memo_cd IS NULL OR c.memo_cd <> 'X')
        GROUP BY derived.f_canonical_employer_name(c.employer)
    ),
    matches AS (
        SELECT
            fc.canonical_employer,
            fc.sum_positive_amt,
            fc.n_donations,
            fc.n_unique_donors
        FROM fec_clusters fc
        JOIN us_employers ue
          ON ue.canonical_employer = fc.canonical_employer
        -- Floor: a cluster with 0 positive donations (only refunds)
        -- adds no useful signal. The `0` floor also avoids a
        -- degenerate raw_value=0 row whose percentile is meaningless.
        WHERE fc.sum_positive_amt > 0
    ),
    ranked AS (
        SELECT
            canonical_employer,
            sum_positive_amt,
            n_donations,
            n_unique_donors,
            -- CUME_DIST returns count_at_or_below / total. For a single
            -- matched cluster this gives 1.0 ("you ARE the bucket"),
            -- which is directionally correct.
            CUME_DIST() OVER (ORDER BY sum_positive_amt) AS pctile
        FROM matches
    )
    SELECT
        p_cycle                                       AS cycle,
        'donor_cluster'                               AS entity_kind,
        canonical_employer                            AS entity_id,
        'donor_employed_by_nj_contractor'             AS signal_id,
        sum_positive_amt                              AS raw_value,
        3::SMALLINT                                   AS severity,
        'kind=donor_cluster'                          AS peer_bucket,
        pctile                                        AS peer_percentile,
        '/fec/risk/entities/donor_cluster/'
            || REPLACE(canonical_employer, '/', '_')
            || '?signal=donor_employed_by_nj_contractor'
            || '&cycle=' || p_cycle                   AS evidence_url
    FROM ranked;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_donor_employed_by_nj_contractor(CHAR) IS
    'Refresh the donor_employed_by_nj_contractor signal for one FEC '
    'cycle. Idempotent on its (cycle, signal_id) slice. One row per '
    '(cycle, canonical_employer) where the employer matches an active '
    'USAspending NJ-pop contractor. raw_value is SUM of positive '
    'transaction amounts (memo_cd=X excluded). peer_percentile is '
    'CUME_DIST within the matched-clusters set. Returns the number of '
    'rows inserted.';
