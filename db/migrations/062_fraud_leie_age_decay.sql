-- ============================================================================
-- Migration: 062_fraud_leie_age_decay
--
-- Detection-quality slice B (paired with 061's threshold + diversity work).
--
-- LEIE exclusions decay in detection priority over time. A 2003 mandatory
-- exclusion is far less likely to indicate active fraud activity than a
-- 2025 exclusion -- not because the underlying sanction is less severe
-- (mandatory 1128(a)(1) exclusions can be permanent) but because the
-- forensic-relevance of the exclusion to a CURRENT cycle's signal is
-- proportional to how recent the regulator's enforcement attention was.
-- A 22-year-old exclusion is often a procedural artifact (excluded
-- person retired, deceased, immobile in active fraud); a 2-year-old
-- exclusion is a live lead.
--
-- DESIGN
-- ------
-- Apply an exponential time-decay weight to LEIE-bearing signals'
-- raw_value at L1 write time:
--
--     decay(age_years) = exp(-age_years / tau),  tau = 10
--
-- Examples (tau = 10):
--     fresh                   1.000
--     1 year old              0.905
--     5 years old             0.607
--     10 years old            0.368
--     15 years old            0.223
--     20 years old            0.135
--
-- Half-life is ~6.93 years. We chose tau=10 by inspection of LEIE
-- enforcement vintage distribution: ~half of HHS-OIG exclusions are
-- within the last 10 years, so a half-life around 7 years preserves
-- the bulk of the active-enforcement era at >50% weight while
-- aggressively decaying pre-2010 entries.
--
-- WHY EXPONENTIAL (not linear, not piecewise)
-- ------------------------------------------
-- 1. Single tunable parameter (tau). Linear has two (slope + floor);
--    piecewise has many. Operator can move tau between 5 (fast decay)
--    and 20 (slow decay) without rewriting the SQL.
-- 2. Asymptotes to zero but never reaches it. A 50-year-old exclusion
--    still contributes a small but nonzero weight, which is the right
--    semantics: ancient exclusions are not fully forgotten, just
--    heavily discounted.
-- 3. Smooth and monotone. A piecewise step function would create
--    discontinuities at the cliffs (an entity flipping from 0.5 to
--    0.3 weight on its 11th anniversary feels arbitrary).
-- 4. Bayesian intuition. A first-order kinetics decay matches the
--    "memoryless" prior on enforcement-relevance: P(still relevant
--    after t years) = exp(-t/tau).
--
-- WHY APPLY AT L1 (not L2 / not L3a)
-- ----------------------------------
-- L1 is the per-(entity, signal) observation. raw_value is the
-- magnitude of the observed phenomenon. For LEIE-bearing signals
-- the observed phenomenon IS the LEIE-related dollar / count, and
-- "current LEIE-related magnitude" is the right semantic (not
-- "literal historical sum"). The substrate-honest LITERAL sum
-- remains in the upstream raw.fec_contribution / raw.usaspending
-- tables. The L1 row is already an interpretation (severity +
-- bucket + percentile are computed); the decayed amount is one
-- more analyst-actionable interpretation.
--
-- L2 is per-entity aggregation, which has no excldate context (an
-- entity can fire on multiple signals; a single decay-at-L2 would
-- be ambiguous about which excldate to use). L3a's score function
-- composes percentile + severity + family; injecting decay there
-- breaks the function's purity (it would need to read the source
-- table). L1 is the only place the decay can live coherently.
--
-- INTERACTION WITH MIGRATION 061's min_actionable_threshold
-- ---------------------------------------------------------
-- A decayed raw_value is what the L2 INNER JOIN compares against
-- fraud_signal_config.min_actionable_threshold. A 12-year-old
-- $20K contract decays to $20K * exp(-1.2) = $6,024, which is
-- below the $10K floor for entity_funded_and_excluded and drops
-- out of the analyst queue. That is the design intent: combining
-- a magnitude floor with a recency decay means the queue surfaces
-- "currently-actionable" matches.
--
-- SCOPE
-- -----
-- Migration 062 covers all four LEIE-bearing signals:
--   054 entity_on_leie                       (binary -> decayed binary)
--   058 entity_funded_and_excluded            ($ -> decayed $)
--   059 donor_on_leie                         ($ -> decayed $)
--   060 candidate_funded_by_excluded_donors   ($ -> per-donor-decayed $)
--
-- The two non-LEIE-bearing signals (056 donor_employed_by_nj_contractor,
-- 057 candidate_funded_by_nj_contractor_employees) are unaffected:
-- their substrate is USAspending awards / FEC employer text, neither
-- of which has an exclusion-age dimension.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- derived.f_leie_age_decay(p_excldate)
-- ----------------------------------------------------------------------------
-- Returns the time-decay weight for a LEIE excldate. STABLE (uses
-- CURRENT_DATE which is fixed within a transaction; not IMMUTABLE
-- because the value drifts as wall-clock time passes across runs).
--
-- INPUT
--   p_excldate  DATE   the exclusion effective date.
--                      NULL is treated as full-weight (1.0): we have
--                      no age information, so the conservative choice
--                      is "do not decay" rather than "max decay."
--                      Future excldate (data error) is clamped to
--                      age = 0 (full weight); we surface the data-
--                      error condition via an asset check on
--                      raw.hhs_oig_leie, not by silently discounting
--                      the row.
--
-- OUTPUT
--   NUMERIC in (0, 1].
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.f_leie_age_decay(
    p_excldate DATE
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT EXP(
        -GREATEST(
            0::NUMERIC,
            (CURRENT_DATE - COALESCE(p_excldate, CURRENT_DATE))::NUMERIC
            / 365.25::NUMERIC
        ) / 10::NUMERIC
    );
$$;

COMMENT ON FUNCTION derived.f_leie_age_decay(DATE) IS
    'Exponential time-decay weight for LEIE exclusion age. Returns '
    'exp(-years_since_excldate / 10), in (0, 1]. NULL excldate -> 1.0 '
    '(no decay; we cannot date-discount what we cannot date). Future '
    'excldate -> 1.0 (clamped age=0). STABLE: uses CURRENT_DATE.';


-- ============================================================================
-- 054 -> derived.refresh_signal_entity_on_leie (with LEIE-age decay)
-- ============================================================================
-- The pre-decay raw_value was 1::NUMERIC (a binary indicator). After
-- decay it's f_leie_age_decay(leie_excldate) in (0, 1].
--
-- The peer_percentile is rate-based (1 - n_flagged / n_in_bucket) and
-- is NOT affected by the decay -- being on LEIE is rare regardless of
-- when. The decay only modulates the magnitude (raw_value) which feeds
-- the L2 threshold filter and the L3a evidence panel.
--
-- min_actionable_threshold for entity_on_leie is 0 (binary signal),
-- so even fully-decayed (~0.05) matches still pass the L2 filter.
-- The threshold floor was chosen explicitly with this case in mind:
-- being on LEIE at all, even from 30 years ago, is an actionable
-- piece of evidence; the magnitude column communicates "how
-- recently was this person sanctioned" rather than "should we
-- look at them at all".
-- ============================================================================
CREATE OR REPLACE FUNCTION derived.refresh_signal_entity_on_leie(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'entity_on_leie';

    WITH leie AS (
        SELECT canonical_key, leie_record_hash, leie_excldate
        FROM derived.v_leie_individual_canonical
    ),
    cand_matches_raw AS (
        SELECT DISTINCT ON (c.cand_id)
            c.cycle,
            c.cand_id,
            c.cand_office,
            c.cand_office_st,
            l.leie_record_hash,
            l.leie_excldate
        FROM raw.fec_candidate c
        JOIN leie l
          ON l.canonical_key = derived.f_canonical_lastfirst_from_fec(c.cand_name)
        WHERE c.cycle = p_cycle
          AND c.cand_name IS NOT NULL
        ORDER BY c.cand_id, l.leie_excldate DESC NULLS LAST
    ),
    cand_pop AS (
        SELECT
            cycle,
            COUNT(*) AS n_in_bucket
        FROM raw.fec_candidate
        WHERE cycle = p_cycle
        GROUP BY cycle
    ),
    cand_flag AS (
        SELECT cycle, COUNT(*) AS n_flagged FROM cand_matches_raw GROUP BY cycle
    ),
    tres_matches_raw AS (
        SELECT DISTINCT ON (tres_canonical)
            cm.cycle,
            REGEXP_REPLACE(UPPER(TRIM(cm.tres_nm)), '\s+', ' ', 'g')
                                                          AS tres_canonical,
            l.leie_record_hash,
            l.leie_excldate
        FROM raw.fec_committee cm
        JOIN leie l
          ON l.canonical_key = derived.f_canonical_lastfirst_from_fec(cm.tres_nm)
        WHERE cm.cycle = p_cycle
          AND cm.tres_nm IS NOT NULL
          AND TRIM(cm.tres_nm) <> ''
        ORDER BY tres_canonical, l.leie_excldate DESC NULLS LAST
    ),
    tres_pop AS (
        SELECT
            cycle,
            COUNT(DISTINCT REGEXP_REPLACE(UPPER(TRIM(tres_nm)), '\s+', ' ', 'g'))
                AS n_in_bucket
        FROM raw.fec_committee
        WHERE cycle = p_cycle
          AND tres_nm IS NOT NULL
          AND TRIM(tres_nm) <> ''
        GROUP BY cycle
    ),
    tres_flag AS (
        SELECT cycle, COUNT(*) AS n_flagged FROM tres_matches_raw GROUP BY cycle
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    -- Candidates
    SELECT
        cm.cycle,
        'candidate',
        cm.cand_id,
        'entity_on_leie',
        derived.f_leie_age_decay(cm.leie_excldate)::NUMERIC,
        5::SMALLINT,
        'kind=candidate',
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (
                cf.n_flagged::NUMERIC
                / NULLIF(cp.n_in_bucket, 0)::NUMERIC
            )
        ),
        '/fec/risk/entities/candidate/' || cm.cand_id
            || '?signal=entity_on_leie&leie=' || cm.leie_record_hash
    FROM cand_matches_raw cm
    JOIN cand_pop  cp ON cp.cycle = cm.cycle
    LEFT JOIN cand_flag cf ON cf.cycle = cm.cycle
    UNION ALL
    -- Treasurers
    SELECT
        tm.cycle,
        'treasurer',
        tm.tres_canonical,
        'entity_on_leie',
        derived.f_leie_age_decay(tm.leie_excldate)::NUMERIC,
        5::SMALLINT,
        'kind=treasurer',
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (
                tf.n_flagged::NUMERIC
                / NULLIF(tp.n_in_bucket, 0)::NUMERIC
            )
        ),
        '/fec/risk/entities/treasurer/'
            || REPLACE(tm.tres_canonical, '/', '_')
            || '?signal=entity_on_leie&leie=' || tm.leie_record_hash
    FROM tres_matches_raw tm
    JOIN tres_pop  tp ON tp.cycle = tm.cycle
    LEFT JOIN tres_flag tf ON tf.cycle = tm.cycle;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
-- 058 -> derived.refresh_signal_entity_funded_and_excluded (with decay)
-- ============================================================================
-- Each matched recipient has one freshest leie_excldate (DISTINCT-ON
-- in leie_canonical_freshest CTE). The whole award SUM is multiplied
-- by the decay weight at the per-recipient aggregation level. This
-- is mathematically equivalent to multiplying each award row before
-- the SUM (since they share one excldate per recipient) and is
-- cheaper -- one decay call per recipient, not one per award.
-- ============================================================================
CREATE OR REPLACE FUNCTION derived.refresh_signal_entity_funded_and_excluded(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'entity_funded_and_excluded';

    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    WITH us_individual_recipients AS (
        SELECT
            recipient_canonical_individual          AS canonical_individual,
            generated_unique_award_id,
            award_amount
        FROM derived.v_usaspending_award_active
        WHERE recipient_canonical_individual IS NOT NULL
    ),
    pop AS (
        SELECT COUNT(DISTINCT canonical_individual) AS n_in_bucket
        FROM us_individual_recipients
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
        SELECT
            r.canonical_individual,
            l.leie_record_hash,
            l.leie_excldate,
            r.award_amount,
            r.generated_unique_award_id
        FROM us_individual_recipients r
        JOIN leie_canonical_freshest l
          ON l.canonical_key = r.canonical_individual
    ),
    aggregated AS (
        -- One row per matched person. The SUM is over their awards;
        -- the decay weight is the freshest exclusion's age, applied
        -- once at the per-person level.
        SELECT
            canonical_individual,
            MIN(leie_record_hash)                      AS leie_record_hash,
            MIN(leie_excldate)                         AS leie_excldate,
            COALESCE(SUM(GREATEST(award_amount, 0)), 0)::NUMERIC
                                                       AS sum_positive_amt
        FROM matches
        GROUP BY canonical_individual
    ),
    flag_count AS (
        SELECT COUNT(*) AS n_flagged FROM aggregated
    )
    SELECT
        p_cycle                                        AS cycle,
        'contractor'                                   AS entity_kind,
        a.canonical_individual                         AS entity_id,
        'entity_funded_and_excluded'                   AS signal_id,
        (a.sum_positive_amt
            * derived.f_leie_age_decay(a.leie_excldate))::NUMERIC
                                                       AS raw_value,
        5::SMALLINT                                    AS severity,
        'kind=contractor'                              AS peer_bucket,
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (
                fc.n_flagged::NUMERIC
                / NULLIF(p.n_in_bucket, 0)::NUMERIC
            )
        )                                              AS peer_percentile,
        '/fec/risk/entities/contractor/'
            || REPLACE(REPLACE(a.canonical_individual, '/', '_'),
                       '|', '_')
            || '?signal=entity_funded_and_excluded'
            || '&cycle=' || p_cycle
            || '&leie=' || a.leie_record_hash          AS evidence_url
    FROM aggregated a
    CROSS JOIN pop p
    CROSS JOIN flag_count fc;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
-- 059 -> derived.refresh_signal_donor_on_leie (with LEIE-age decay)
-- ============================================================================
-- Mirrors the 058 pattern: one freshest excldate per donor; decay
-- applied to the per-donor SUM at the aggregation level.
-- ============================================================================
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
    leie_canonical_freshest AS (
        SELECT DISTINCT ON (canonical_key)
            canonical_key,
            leie_record_hash,
            leie_excldate
        FROM derived.v_leie_individual_canonical
        ORDER BY canonical_key, leie_excldate DESC NULLS LAST
    ),
    matches AS (
        SELECT
            d.canonical_key,
            l.leie_record_hash,
            l.leie_excldate,
            d.transaction_amt
        FROM donor_canonical d
        JOIN leie_canonical_freshest l USING (canonical_key)
    ),
    aggregated AS (
        SELECT
            canonical_key,
            MIN(leie_record_hash)                          AS leie_record_hash,
            MIN(leie_excldate)                             AS leie_excldate,
            COALESCE(SUM(GREATEST(transaction_amt, 0)), 0)::NUMERIC
                                                           AS sum_positive_amt
        FROM matches
        GROUP BY canonical_key
    ),
    aggregated_active AS (
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
        (a.sum_positive_amt
            * derived.f_leie_age_decay(a.leie_excldate))::NUMERIC
                                                           AS raw_value,
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


-- ============================================================================
-- 060 -> derived.refresh_signal_candidate_funded_by_excluded_donors (with decay)
-- ============================================================================
-- The candidate-side projection is structurally different: each
-- contribution to the candidate is from a different donor (with a
-- different excldate), so the decay must be applied PER-CONTRIBUTION
-- before the candidate-level SUM. This requires re-joining LEIE
-- inside 060 to get each donor's freshest excldate.
--
-- The added matched_donors_with_excldate CTE replaces the previous
-- matched_donors CTE: it pulls the L1 matched-donor set (substrate-
-- honesty: 060 is keyed off 059's L1 to avoid drift) and enriches
-- each one with the donor's freshest LEIE excldate. The
-- flagged_contributions CTE then carries excldate forward, and
-- per_candidate sums (transaction_amt * decay) per candidate.
-- ============================================================================
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
        SELECT entity_id AS canonical_donor
        FROM derived.fraud_signal_observation
        WHERE cycle = p_cycle
          AND signal_id = 'donor_on_leie'
    ),
    leie_canonical_freshest AS (
        -- Mirror 059: one freshest excldate per canonical donor.
        SELECT DISTINCT ON (canonical_key)
            canonical_key,
            leie_excldate
        FROM derived.v_leie_individual_canonical
        ORDER BY canonical_key, leie_excldate DESC NULLS LAST
    ),
    matched_donors_with_excldate AS (
        -- Enrich the L1 matched-donor set with each donor's freshest
        -- LEIE excldate so we can apply per-contribution decay below.
        -- LEFT JOIN: a donor in 059's L1 must have a LEIE record (the
        -- match was the reason they got into L1), so a NULL excldate
        -- here would be a 059 / LEIE-canonical drift. We pass the
        -- NULL through and f_leie_age_decay treats it as no-decay
        -- (1.0); an asset check on the mass match rate catches
        -- this regression separately.
        SELECT
            m.canonical_donor,
            l.leie_excldate
        FROM matched_donors m
        LEFT JOIN leie_canonical_freshest l
          ON l.canonical_key = m.canonical_donor
    ),
    flagged_contributions AS (
        SELECT
            c.cycle,
            c.cmte_id,
            c.transaction_amt,
            c.name,
            md.leie_excldate
        FROM raw.fec_contribution c
        JOIN matched_donors_with_excldate md
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
        SELECT
            cand.cycle,
            cand.cand_id,
            cand.cand_office,
            cand.cand_office_st,
            -- Each contribution decays by its donor's freshest
            -- excldate. SUM(decayed) is the candidate-level
            -- "currently-actionable" total receipt from LEIE-
            -- excluded donors.
            SUM(
                fc.transaction_amt
                * derived.f_leie_age_decay(fc.leie_excldate)
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
    WHERE sum_amt > 0;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;
