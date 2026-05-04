-- ============================================================================
-- Migration: 059_fraud_donor_on_leie
--
-- TIER 4 v3 / FRAUD-F5c (third LEIE-bearing cross-source signal).
-- Joins active raw.fec_contribution donors against active LEIE
-- individual exclusions on the canonical "LAST|FIRST" key.
--
-- WHAT THIS SIGNAL ANSWERS
-- ------------------------
-- "Which individuals on the federal exclusion list (LEIE) donate to
-- NJ political campaigns?" A federally-excluded healthcare provider
-- writing checks to a candidate or PAC is, on its face, suspicious:
--   * the donor has been formally barred from federal participation,
--     yet retains discretionary income and political access;
--   * the donor's donation may signal an attempt to retain political
--     cover during exclusion;
--   * the donor may attempt to co-opt regulatory action through a
--     funded official.
-- These are reportable by themselves; combined with downstream
-- signals (e.g., the donor's exclusion overlapping a vote on
-- healthcare procurement), they sketch a procurement-influence
-- pattern.
--
-- WHY 'donor' IS A NEW entity_kind (not 'donor_cluster' reuse)
-- ------------------------------------------------------------
-- The existing 'donor_cluster' kind groups donors by SHARED EMPLOYER.
-- The donor-on-LEIE entity is a SINGLE PERSON, identified by their
-- canonical name. They are a different shape semantically (an
-- individual identity, not an aggregation) and analytically (the
-- analyst queue surfaces a person's profile, not a workforce).
-- Reusing 'donor_cluster' would conflate the two, so this migration
-- adds 'donor' to the whitelist.
--
-- COMPLEMENTS, DOES NOT DUPLICATE, derived.signal_entity_on_leie
-- --------------------------------------------------------------
-- entity_on_leie matches FEC ENTITIES (candidate cand_name,
-- committee tres_nm) against LEIE individuals. Those entities are
-- the "structural" side of FEC: people running for office or
-- managing committees.
-- donor_on_leie matches FEC DONORS (the third-party contributors
-- whose names appear in raw.fec_contribution.name). That is a
-- vastly larger and demographically different population.
-- Both signals can fire on the same person if e.g., a candidate is
-- ALSO a donor to another candidate AND is on LEIE -- but they
-- represent two different concerns (entity-being-excluded vs
-- donor-influence-from-excluded-source). The L2 pivot keeps them as
-- separate signal columns; the L3a score sums their severities, so
-- the combined effect is correctly amplified, not double-counted.
--
-- WHAT'S OUT OF SCOPE FOR V1
-- --------------------------
-- 1. Candidate-side projection (i.e., "which candidates received
--    money from LEIE-excluded donors?"). That's a follow-up signal
--    in the same shape as 057 (joins through fec_committee.cand_id),
--    deferred so the L1 substrate ships clean first.
-- 2. State / DOB filtering. As with 054, strict on canonicalization,
--    loose on demographics in v1.
-- 3. Aggregate-row donors (FEC's "AGGREGATE CONTRIBUTIONS UNDER
--    $200" placeholders) are NOT excluded explicitly; the
--    canonicalizer drops them via empty-string-after-normalization
--    NULL handling.
-- 4. Conduit / earmarked-contribution disambiguation. A donor name
--    may appear via a conduit (e.g., ActBlue); we count the
--    transaction in raw.fec_contribution at face value. The downstream
--    "money flows from LEIE through conduit to candidate" pattern is
--    a future signal that joins this one with FEC Schedule B-A
--    earmark records.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- Schema change: extend the entity_kind whitelist to include 'donor'
-- ----------------------------------------------------------------------------
-- DROP CONSTRAINT + ADD CONSTRAINT (Postgres single-statement modify
-- not available). Migration 058 already replaced the original
-- inline constraint with an explicitly-named one, so we target it
-- by name.
-- ----------------------------------------------------------------------------
ALTER TABLE derived.fraud_signal_observation
DROP CONSTRAINT IF EXISTS fraud_signal_observation_entity_kind_check;

ALTER TABLE derived.fraud_signal_observation
ADD  CONSTRAINT fraud_signal_observation_entity_kind_check
CHECK (entity_kind IN (
    'committee',
    'candidate',
    'treasurer',
    'address',
    'donor_cluster',
    'contractor',
    'donor'
));

COMMENT ON CONSTRAINT fraud_signal_observation_entity_kind_check
ON derived.fraud_signal_observation IS
    'Whitelist of entity_kind values. Seven kinds as of migration 059: '
    'the five FEC-domain kinds (committee, candidate, treasurer, '
    'address, donor_cluster), plus contractor (federal-award '
    'recipient, currently person-shaped only -- migration 058), plus '
    'donor (single-person FEC contributor identity, distinct from '
    'donor_cluster which is a workforce aggregation -- migration 059).';


-- ----------------------------------------------------------------------------
-- REFRESHER: derived.refresh_signal_donor_on_leie(cycle)
-- ----------------------------------------------------------------------------
-- Idempotent on its (cycle, signal_id='donor_on_leie') slice.
-- DELETE-then-INSERT pattern mirroring the other LEIE-bearing
-- refreshers.
--
-- AGGREGATION
-- -----------
-- raw_value = SUM(GREATEST(transaction_amt, 0)) per matched canonical
-- donor in the cycle, EXCLUDING memo records (memo_cd='X'). The
-- positive-only filter handles refunds/corrections; the memo filter
-- handles FEC's intentional double-counting of conduit / earmarked
-- transactions, which mirrors the rule used in migrations 056/057.
--
-- Negative-amount inclusion would invert the signal: a "donor" with
-- net negative giving (i.e., received more refunds than they gave)
-- is not a procurement-influence concern. The percentile only ranks
-- positive cumulative giving; donors with raw_value=0 after filtering
-- are dropped to keep the queue actionable.
--
-- BUCKET / PERCENTILE
-- -------------------
-- peer_bucket     = 'kind=donor'
-- peer_percentile = 1 - (n_flagged / n_in_bucket)
-- Bucket population = distinct canonical donor keys in the cycle (NOT
-- contributions, NOT individuals on the LEIE side). This is the
-- "could-have-been-flagged" denominator.
--
-- LEIE-side pre-collapse via DISTINCT ON canonical_key ORDER BY
-- excldate DESC picks the freshest LEIE record per person. This
-- mirrors migration 058's pattern and prevents contribution double-
-- counting when one person has multiple LEIE entries.
--
-- entity_id = canonical "LAST|FIRST" key. A donor with multiple
-- name spellings ("DOE, JANE M." vs "DOE, JANE") collapses to one
-- entity row, which is the correct semantic for this signal.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_donor_on_leie(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'donor_on_leie';

    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    WITH donor_canonical AS (
        -- One row per (contribution row, canonical donor key) in the
        -- cycle. The canonicalizer returns NULL for corporate-shaped
        -- names (no comma), so this CTE is automatically person-only.
        -- memo_cd='X' rows are pre-filtered to avoid double-counting.
        -- Negative amounts are deferred to the aggregation step
        -- (positive-only SUM) so we still see a donor's positive
        -- giving even if they had partial refunds.
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
        -- Bucket population for the rate-based percentile.
        SELECT COUNT(DISTINCT canonical_key) AS n_in_bucket
        FROM donor_canonical
    ),
    leie_canonical_freshest AS (
        SELECT DISTINCT ON (canonical_key)
            canonical_key,
            leie_record_hash,
            leie_excldate
        FROM derived.v_leie_individual_canonical
        ORDER BY canonical_key, leie_excldate DESC NULLS LAST
    ),
    matches AS (
        -- One row per matched contribution. M-LEIE pre-collapsed
        -- above so each contribution row appears at most once here.
        SELECT
            d.canonical_key,
            l.leie_record_hash,
            d.transaction_amt
        FROM donor_canonical d
        JOIN leie_canonical_freshest l USING (canonical_key)
    ),
    aggregated AS (
        SELECT
            canonical_key,
            MIN(leie_record_hash)                          AS leie_record_hash,
            COALESCE(SUM(GREATEST(transaction_amt, 0)), 0)::NUMERIC
                                                           AS sum_positive_amt
        FROM matches
        GROUP BY canonical_key
    ),
    aggregated_active AS (
        -- A donor with all-refund (negative-only) giving has
        -- sum_positive_amt = 0 and offers no actionable signal;
        -- drop them so the analyst queue stays actionable.
        SELECT * FROM aggregated WHERE sum_positive_amt > 0
    ),
    flag_count AS (
        SELECT COUNT(*) AS n_flagged FROM aggregated_active
    )
    SELECT
        p_cycle                                            AS cycle,
        'donor'                                            AS entity_kind,
        a.canonical_key                                    AS entity_id,
        'donor_on_leie'                                    AS signal_id,
        a.sum_positive_amt                                 AS raw_value,
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
            || '?signal=donor_on_leie'
            || '&cycle=' || p_cycle
            || '&leie=' || a.leie_record_hash              AS evidence_url
    FROM aggregated_active a
    CROSS JOIN donor_pop p
    CROSS JOIN flag_count fc;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_donor_on_leie(CHAR) IS
    'Refresh donor_on_leie for one FEC cycle. Idempotent on its '
    '(cycle, signal_id) slice. One row per matched canonical '
    '"LAST|FIRST" donor key. raw_value = SUM(positive transaction_amt) '
    'across the donor''s contributions in the cycle, excluding '
    'memo_cd=X records (FEC double-counts) and donors with all-refund '
    '(net-zero) giving. severity=5 (CRITICAL); rate-based '
    'peer_percentile within ''kind=donor'' bucket.';
