-- ============================================================================
-- Migration: 065_fraud_donor_on_sam
--
-- TIER 4 v3 / FRAUD-F2 (donor-side SAM cross-source signal).
-- Joins active raw.fec_contribution donors against active SAM.gov
-- individual exclusions on the canonical "LAST|FIRST" key.
--
-- WHAT THIS SIGNAL ANSWERS
-- ------------------------
-- "Which individuals on the federal SAM exclusion list donate to NJ
-- political campaigns?" Parallel to donor_on_leie (059) but using
-- SAM's BROADER coverage:
--    * LEIE: HHS-OIG healthcare exclusions only.
--    * SAM:  EVERY federal excluding agency -- DOJ, OFAC, GSA, NIH,
--            NSF, DOE, Treasury reciprocity, plus HHS-OIG mirrors.
-- An individual on SAM (whether or not also on LEIE) is barred from
-- federal participation. Their political donations to NJ campaigns
-- merit analyst review for the same reason as donor_on_leie:
--   * the donor has been formally barred from federal programs,
--     yet retains discretionary income and political access;
--   * the donation may signal an attempt to retain political cover
--     during exclusion;
--   * the donor may attempt to co-opt regulatory action through a
--     funded official.
--
-- COMPLEMENTS, DOES NOT DUPLICATE, donor_on_leie
-- ----------------------------------------------
-- A donor on BOTH SAM and LEIE will fire BOTH signals. That is the
-- correct semantic: dual-list inclusion is stronger evidence than
-- single-list (and the diversity bonus in derived.fraud_risk_score
-- amplifies multi-family entities). The (entity_kind, entity_id,
-- signal_id) PK keeps them as separate L1 rows; the L2 pivot
-- aggregates both into one (entity_kind, entity_id) row with both
-- signal columns populated.
--
-- A donor on SAM but not LEIE (e.g., a defense-contractor employee
-- excluded by GSA, with no healthcare relationship) fires ONLY this
-- signal -- which is exactly the gap donor_on_leie cannot cover.
--
-- WHY entity_kind='donor' (already in the whitelist as of migration 059)
-- ----------------------------------------------------------------------
-- 059 added 'donor' to the entity_kind whitelist. We reuse it
-- without further schema change. entity_id = canonical "LAST|FIRST".
--
-- AGGREGATION
-- -----------
-- raw_value = SUM(GREATEST(transaction_amt, 0) * f_leie_age_decay(
--                 sam_active_date)) per matched canonical donor in
-- the cycle, EXCLUDING memo records (memo_cd='X'). Per-contribution
-- decay anchored on the (freshest) SAM exclusion's active_date so a
-- 12-year-old SAM exclusion combined with $1000 of recent donations
-- contributes ~$300 to raw_value.
--
-- Negative-amount inclusion would invert the signal: a "donor" with
-- net negative giving (more refunds than gifts) is not a procurement-
-- influence concern. The sum-positive filter mirrors donor_on_leie.
--
-- BUCKET / PERCENTILE
-- -------------------
-- peer_bucket     = 'kind=donor'
-- peer_percentile = 1 - (n_flagged / n_in_bucket)
-- Bucket population = distinct canonical donor keys in the cycle.
-- Same shape as donor_on_leie so the percentile is comparable
-- across the two signals.
--
-- SAM-side pre-collapse via DISTINCT ON canonical_key ORDER BY
-- sam_active_date DESC picks the freshest SAM exclusion per person,
-- mirroring 058/059's freshest-LEIE selection.
--
-- WHAT'S OUT OF SCOPE FOR V1
-- --------------------------
-- 1. Candidate-side projection (parallel to 060: "which candidates
--    received money from SAM-excluded donors?"). Future migration.
-- 2. Joint LEIE+SAM-exclusion analyst panel. The L2 pivot already
--    aggregates both donor_on_leie and donor_on_sam into one row
--    when both fire on the same canonical_key; the analyst panel
--    can read that row and surface both record hashes.
-- 3. State / DOB filtering. Strict on canonicalization, loose on
--    demographics, same as 058/059.
-- 4. Conduit / earmarked-contribution disambiguation. Inherits the
--    memo_cd='X' filter from the same-shape predecessor 059.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. Seed the new signal config row
-- ----------------------------------------------------------------------------
-- Threshold=$200 mirrors donor_on_leie. FEC itemizes donors at
-- $200+; below that, contributions appear in aggregate rows that
-- canonicalize to NULL, so the threshold is also a natural floor on
-- the FEC side. family='sam_bearing'.
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config (
    signal_id,
    signal_family,
    min_actionable_threshold,
    comment
) VALUES (
    'donor_on_sam',
    'sam_bearing',
    200,
    'Cross-source canonical-name match: a FEC NJ-cycle donor whose '
    'canonical LAST|FIRST key matches an active SAM.gov individual '
    'exclusion. Threshold=$200 mirrors donor_on_leie -- below that, '
    'FEC aggregates the contribution and the canonicalizer drops it. '
    'family=sam_bearing so a same-person LEIE+SAM dual fire earns '
    'the multi-family diversity bonus in fraud_risk_score.'
)
ON CONFLICT (signal_id) DO UPDATE
    SET signal_family            = EXCLUDED.signal_family,
        min_actionable_threshold = EXCLUDED.min_actionable_threshold,
        comment                  = EXCLUDED.comment;


-- ----------------------------------------------------------------------------
-- 2. REFRESHER: derived.refresh_signal_donor_on_sam(cycle)
-- ----------------------------------------------------------------------------
-- Idempotent on its (cycle, signal_id='donor_on_sam') slice.
-- DELETE-then-INSERT pattern matching donor_on_leie (059).
--
-- Per-contribution age-decay weighting is the structural difference
-- from 059: each contribution is multiplied by f_leie_age_decay(
-- sam_active_date) before being summed. Because the freshest SAM
-- exclusion has been pre-selected per canonical_key, all rows for a
-- given donor share the same active_date and the per-contribution
-- decay is mathematically equivalent to a per-donor decay applied
-- post-SUM. We write it per-row anyway for parity with the future
-- multi-source variant where different donors have different ages.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_donor_on_sam(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'donor_on_sam';

    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    WITH donor_canonical AS (
        -- One row per (contribution row, canonical donor key) in the
        -- cycle. The canonicalizer returns NULL for corporate-shaped
        -- names (no comma), so this CTE is automatically person-only.
        -- memo_cd='X' rows are pre-filtered to avoid double-counting.
        SELECT
            derived.f_canonical_lastfirst_from_fec(c.name) AS canonical_key,
            c.transaction_amt
        FROM raw.fec_contribution c
        WHERE c.cycle = p_cycle
          AND c.name IS NOT NULL
          AND derived.f_canonical_lastfirst_from_fec(c.name) IS NOT NULL
          AND (c.memo_cd IS NULL OR c.memo_cd <> 'X')
    ),
    donor_pop AS (
        SELECT COUNT(DISTINCT canonical_key) AS n_in_bucket
        FROM donor_canonical
    ),
    sam_canonical_freshest AS (
        -- One representative SAM row per canonical_key (freshest
        -- active_date wins). Mirror of leie_canonical_freshest in
        -- migration 059, swapped over to v_sam_exclusion_individual_
        -- canonical (added in migration 063).
        SELECT DISTINCT ON (canonical_key)
            canonical_key,
            sam_record_hash,
            sam_active_date
        FROM derived.v_sam_exclusion_individual_canonical
        ORDER BY canonical_key, sam_active_date DESC NULLS LAST
    ),
    matches AS (
        -- One row per matched contribution. SAM pre-collapsed above
        -- so each contribution row appears at most once.
        SELECT
            d.canonical_key,
            s.sam_record_hash,
            s.sam_active_date,
            d.transaction_amt
        FROM donor_canonical d
        JOIN sam_canonical_freshest s USING (canonical_key)
    ),
    aggregated AS (
        SELECT
            canonical_key,
            MIN(sam_record_hash) AS sam_record_hash,
            -- Per-contribution decay applied. All rows for a given
            -- donor share the same sam_active_date (pre-collapse),
            -- so this is equivalent to one decay weight x SUM, but
            -- we write it per-row for symmetry with future multi-
            -- source variants.
            COALESCE(
                SUM(
                    GREATEST(transaction_amt, 0)::NUMERIC
                    * derived.f_leie_age_decay(sam_active_date)
                ),
                0
            )::NUMERIC AS sum_decayed_amt
        FROM matches
        GROUP BY canonical_key
    ),
    aggregated_active AS (
        -- A donor with all-refund (net-zero or net-negative) giving
        -- has sum_decayed_amt = 0 (since GREATEST(amt, 0) clamps
        -- negatives to 0); drop them so the queue stays actionable.
        SELECT * FROM aggregated WHERE sum_decayed_amt > 0
    ),
    flag_count AS (
        SELECT COUNT(*) AS n_flagged FROM aggregated_active
    )
    SELECT
        p_cycle                                            AS cycle,
        'donor'                                            AS entity_kind,
        a.canonical_key                                    AS entity_id,
        'donor_on_sam'                                     AS signal_id,
        a.sum_decayed_amt                                  AS raw_value,
        5::SMALLINT                                        AS severity,
        'kind=donor'                                       AS peer_bucket,
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (
                fc.n_flagged::NUMERIC
                / NULLIF(p.n_in_bucket, 0)::NUMERIC
            )
        )                                                  AS peer_percentile,
        '/fec/risk/entities/donor/'
            || REPLACE(REPLACE(a.canonical_key, '/', '_'), '|', '_')
            || '?signal=donor_on_sam'
            || '&cycle=' || p_cycle
            || '&sam='   || a.sam_record_hash              AS evidence_url
    FROM aggregated_active a
    CROSS JOIN donor_pop p
    CROSS JOIN flag_count fc;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_donor_on_sam(CHAR) IS
    'Refresh donor_on_sam for one FEC cycle. Idempotent on its '
    '(cycle, signal_id) slice. One row per matched canonical '
    '"LAST|FIRST" donor key whose name appears in derived.v_sam_'
    'exclusion_individual_canonical. raw_value = SUM(positive '
    'transaction_amt * f_leie_age_decay(sam_active_date)) across '
    'the donor''s contributions in the cycle, excluding memo_cd=X '
    'records and donors with all-refund (zero-after-decay) giving. '
    'severity=5 (CRITICAL); rate-based peer_percentile within '
    '''kind=donor'' bucket. Parallel to donor_on_leie (059) using '
    'SAM''s broader exclusion coverage; complementary, not '
    'duplicate.';
