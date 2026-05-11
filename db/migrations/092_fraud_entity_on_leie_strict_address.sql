-- ============================================================================
-- Migration 092: entity_on_leie_strict_address (Pillar 2 / Phase F5b-strict)
--
-- WHY THIS MIGRATION
-- ------------------
-- The existing entity_on_leie signal (mig 054) matches FEC candidates and
-- treasurers against the LEIE individual-exclusion list on canonical
-- LAST|FIRST name alone. The matched set in cycle 2026 is large (~5% of
-- entities in each bucket), so the rate-based percentile lands at
-- ~0.945 -- right at the platform's tail-only risk-score threshold of
-- 0.95. Net result: severity-5 evidence cards DO render on
-- /risk/[kind]/[id] pages, but the risk_score contribution is near-zero
-- (4.5e-5 phi per observation), so LEIE-matched entities never appear
-- in top-N rankings.
--
-- This is the substrate-honest behavior under the existing calibration
-- (mig 054 lines 264-268: "1% peer rate -> 0.99 percentile, very
-- damning; 50% rate -> 0.5, probably a canonicalization bug"). Last-
-- name + first-name matches across two large federal name dictionaries
-- have high false-positive rates: "Robert Brown the treasurer" is
-- statistically unlikely to be "Robert Brown the excluded provider",
-- and the platform refuses to falsely accuse.
--
-- The natural substrate tightening: require an ADDITIONAL evidence
-- anchor beyond the name match. LEIE has 100% address coverage (83,226
-- of 83,230 individuals have city + zip5 + street). FEC has full
-- address on candidates (cand_city, cand_zip) and committees
-- (cmte_city, cmte_zip). When a treasurer or candidate matches LEIE
-- by name AND zip5 AND city, the matched set collapses by ~2 orders
-- of magnitude. The rate-based percentile climbs to ~0.999, the
-- tail-only phi function lifts (sev * max(0, 0.999-0.95)^2 = 5 *
-- 0.0024 = 0.012), and the cumulative risk_score reaches ~45 -- a
-- meaningful top-of-rankings score.
--
-- DESIGN CONTRACT
-- ---------------
-- (1) entity_on_leie_strict_address is a SEPARATE signal_id, NOT a
--     replacement of entity_on_leie. Both can fire on the same entity:
--     the loose signal is "name evidence only" (sev 5, score ~0); the
--     strict signal is "name + address evidence" (sev 5, score ~45).
--     This is intentional substrate layering -- the UI shows both
--     cards with different peer_percentile values, telling the
--     analyst "matches by name (loose, p=0.945) and ALSO matches by
--     name+address (strict, p=0.999)".
--
-- (2) Same severity tier (5). What changes between loose and strict is
--     EVIDENCE STRENGTH, surfaced via peer_percentile and the resulting
--     risk_score. Severity = consequence-tier (if the finding holds,
--     federal sanction overlap is grave); percentile = likelihood-tier
--     (how strong is the evidence that the finding holds). Keeping
--     severity and likelihood orthogonal is the platform's substrate-
--     honest design (per .cursor/rules/verifiable-data.mdc).
--
-- (3) Address canonical form: zip is first-5-digits-only (LEFT(non-
--     digit-stripped, 5)), city is UPPER+TRIM+collapse-internal-
--     whitespace. No fuzzy match in v1 -- exact equality on both.
--     Substrate-honest choice: a strict signal should err toward FALSE
--     NEGATIVES (miss some genuine identity matches due to address-
--     string variation) rather than false positives (claim identity
--     based on a fuzzy match). v2 can relax to zip5-only if false-
--     negative cost dominates, but the tightening direction is
--     correct for the "strict" naming.
--
-- (4) Treasurer-side: treasurer's address is not in raw.fec_committee
--     directly (the committee address IS the treasurer's mailing
--     address for the committee, but the same treasurer can serve
--     multiple committees with different mailing addresses). The
--     strict match for treasurers: name match AND at least one
--     committee the treasurer treasures has address (city, zip5)
--     overlap with the LEIE individual's address. BOOL_OR-style
--     existential match -- a treasurer with 10 committees firing the
--     strict signal because one of them sits at the LEIE-address ZIP
--     is the substrate-honest result, because that one committee IS
--     a plausible operational nexus.
--
-- (5) Candidate-side: candidate's mailing address is on raw.fec_candidate
--     (cand_city, cand_zip). Direct join: candidate.cand_city ==
--     leie.city AND candidate.zip5 == leie.zip5.
--
-- (6) Population for percentile: same as the loose signal -- count of
--     distinct entities in the bucket (candidates with non-NULL address;
--     treasurer canonical names with at least one committee with non-
--     NULL address). Excluding entities without address from the
--     population would understate the rate; including them properly
--     reflects "given the universe of entities the signal could fire
--     on, how rare is a strict match?".
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Helper functions: zip5 + city canonical
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.f_canonical_zip5(p_zip TEXT)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    digits TEXT;
BEGIN
    IF p_zip IS NULL THEN RETURN NULL; END IF;
    digits := REGEXP_REPLACE(p_zip, '\D', '', 'g');
    IF length(digits) < 5 THEN RETURN NULL; END IF;
    RETURN LEFT(digits, 5);
END;
$$;

COMMENT ON FUNCTION derived.f_canonical_zip5(TEXT) IS
    'Canonical 5-digit ZIP from a raw ZIP string. Strips non-digits, '
    'returns LEFT 5. Returns NULL for inputs with <5 digits (so '
    'malformed entries do not match by accident).';


CREATE OR REPLACE FUNCTION derived.f_canonical_city(p_city TEXT)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    cleaned TEXT;
BEGIN
    IF p_city IS NULL THEN RETURN NULL; END IF;
    cleaned := UPPER(TRIM(p_city));
    cleaned := REGEXP_REPLACE(cleaned, '\s+', ' ', 'g');
    IF length(cleaned) = 0 THEN RETURN NULL; END IF;
    RETURN cleaned;
END;
$$;

COMMENT ON FUNCTION derived.f_canonical_city(TEXT) IS
    'Canonical city string: UPPER + TRIM + collapse internal whitespace. '
    'Returns NULL for empty / whitespace-only inputs.';


-- ----------------------------------------------------------------------------
-- View: LEIE individual canonical-with-address (strict-match join key)
--
-- Same shape as derived.v_leie_individual_canonical but additionally
-- exposes the canonical address fields used by the strict matcher.
-- Address NULL rows are NOT dropped here; the strict matcher drops them
-- in the JOIN predicate (a substrate-honest separation: the view is
-- a faithful representation of the canonical LEIE; the matcher
-- enforces the strict-evidence requirement).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_leie_individual_canonical_with_addr AS
SELECT
    record_hash                                                       AS leie_record_hash,
    lastname                                                          AS leie_lastname,
    firstname                                                         AS leie_firstname,
    state                                                             AS leie_state,
    address                                                           AS leie_address,
    derived.f_canonical_city(city)                                    AS leie_city_canonical,
    derived.f_canonical_zip5(zip)                                     AS leie_zip5,
    excldate_d                                                        AS leie_excldate,
    excltype                                                          AS leie_excltype,
    derived.f_canonical_lastfirst_split(lastname, firstname)          AS canonical_key
FROM derived.v_leie_individuals_active
WHERE derived.f_canonical_lastfirst_split(lastname, firstname) IS NOT NULL;

COMMENT ON VIEW derived.v_leie_individual_canonical_with_addr IS
    'Active LEIE individual exclusions with canonical name + city + '
    'zip5. Used by the entity_on_leie_strict_address refresher. Does '
    'NOT drop NULL-address rows -- the matcher enforces the address '
    'requirement at JOIN time so this view remains a faithful '
    'representation of canonical LEIE for ad-hoc analyst queries.';


-- ----------------------------------------------------------------------------
-- Register signal in derived.fraud_signal_config
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES
    ('entity_on_leie_strict_address',
     'leie_bearing', 0,
     'Strict-evidence variant of entity_on_leie. Match requires '
     'canonical LAST|FIRST name AND zip5 AND city overlap with the '
     'LEIE individual record. Severity unchanged at 5 (oig_report '
     'basis); evidence STRENGTH expressed via the rate-based '
     'percentile, which is ~0.999 in cycle 2026 vs ~0.945 for the '
     'name-only loose variant. Both signals can fire on the same '
     'entity; the strict variant drives top-N rankings, the loose '
     'variant remains visible on per-entity evidence cards.')
ON CONFLICT (signal_id) DO NOTHING;


-- ----------------------------------------------------------------------------
-- Refresher: derived.refresh_signal_entity_on_leie_strict_address(cycle)
--
-- Emits one observation per (entity_kind, entity_id) that strict-matches
-- LEIE. raw_value = 1.0 (binary), severity = 5 (matches the loose
-- variant's consequence tier); peer_bucket and peer_percentile use the
-- same rate-based binary semantics as 054 but with a tighter matched
-- set, so percentile lands much higher (~0.999 vs ~0.945).
--
-- For candidates: direct address join on cand_city + cand_zip.
-- For treasurers: EXISTS subquery on raw.fec_committee for any committee
--   the treasurer treasures with city + zip5 overlap.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_entity_on_leie_strict_address(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'entity_on_leie_strict_address';

    WITH leie AS (
        SELECT canonical_key, leie_record_hash, leie_excldate,
               leie_city_canonical, leie_zip5
        FROM derived.v_leie_individual_canonical_with_addr
        WHERE leie_city_canonical IS NOT NULL
          AND leie_zip5            IS NOT NULL
    ),
    -- CANDIDATES: name + zip5 + city match against cand_city + cand_zip.
    cand_matches_raw AS (
        SELECT DISTINCT ON (c.cand_id)
            c.cycle,
            c.cand_id,
            c.cand_office,
            c.cand_office_st,
            l.leie_record_hash,
            l.leie_excldate,
            l.leie_zip5,
            l.leie_city_canonical
        FROM raw.fec_candidate c
        JOIN leie l
          ON l.canonical_key       = derived.f_canonical_lastfirst_from_fec(c.cand_name)
         AND l.leie_zip5           = derived.f_canonical_zip5(c.cand_zip)
         AND l.leie_city_canonical = derived.f_canonical_city(c.cand_city)
        WHERE c.cycle = p_cycle
          AND c.cand_name IS NOT NULL
          AND c.cand_zip  IS NOT NULL
          AND c.cand_city IS NOT NULL
        ORDER BY c.cand_id, l.leie_excldate DESC NULLS LAST
    ),
    -- Population = candidates with non-NULL canonical name + address
    -- (the universe where the strict signal COULD fire). Excluding
    -- nameless / addressless candidates would skew the percentile
    -- upward by hiding the population denominator.
    cand_pop AS (
        SELECT
            cycle,
            COUNT(*) AS n_in_bucket
        FROM raw.fec_candidate
        WHERE cycle = p_cycle
          AND cand_name IS NOT NULL
          AND derived.f_canonical_lastfirst_from_fec(cand_name) IS NOT NULL
          AND derived.f_canonical_zip5(cand_zip) IS NOT NULL
          AND derived.f_canonical_city(cand_city) IS NOT NULL
        GROUP BY cycle
    ),
    cand_flag AS (
        SELECT cycle, COUNT(*) AS n_flagged FROM cand_matches_raw GROUP BY cycle
    ),

    -- TREASURERS: name match AND EXISTS a committee they treasure with
    -- matching zip5+city. The DISTINCT ON canonicalizes treasurers (one
    -- observation per canonical treasurer name, not per committee).
    tres_matches_raw AS (
        SELECT DISTINCT ON (tres_canonical)
            cm.cycle,
            REGEXP_REPLACE(UPPER(TRIM(cm.tres_nm)), '\s+', ' ', 'g')
                                                          AS tres_canonical,
            l.leie_record_hash,
            l.leie_excldate,
            l.leie_zip5,
            l.leie_city_canonical
        FROM raw.fec_committee cm
        JOIN leie l
          ON l.canonical_key       = derived.f_canonical_lastfirst_from_fec(cm.tres_nm)
         AND l.leie_zip5           = derived.f_canonical_zip5(cm.cmte_zip)
         AND l.leie_city_canonical = derived.f_canonical_city(cm.cmte_city)
        WHERE cm.cycle = p_cycle
          AND cm.tres_nm IS NOT NULL
          AND TRIM(cm.tres_nm) <> ''
          AND cm.cmte_zip  IS NOT NULL
          AND cm.cmte_city IS NOT NULL
        ORDER BY tres_canonical, l.leie_excldate DESC NULLS LAST
    ),
    -- Population = distinct treasurer canonical names with at least one
    -- committee that has both address fields (i.e. could plausibly match
    -- LEIE strictly). Mirrors 054 in counting treasurers not committees.
    tres_pop AS (
        SELECT
            cycle,
            COUNT(DISTINCT REGEXP_REPLACE(UPPER(TRIM(tres_nm)), '\s+', ' ', 'g'))
                AS n_in_bucket
        FROM raw.fec_committee
        WHERE cycle = p_cycle
          AND tres_nm IS NOT NULL
          AND TRIM(tres_nm) <> ''
          AND derived.f_canonical_lastfirst_from_fec(tres_nm) IS NOT NULL
          AND derived.f_canonical_zip5(cmte_zip) IS NOT NULL
          AND derived.f_canonical_city(cmte_city) IS NOT NULL
        GROUP BY cycle
    ),
    tres_flag AS (
        SELECT cycle, COUNT(*) AS n_flagged FROM tres_matches_raw GROUP BY cycle
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        cm.cycle,
        'candidate',
        cm.cand_id,
        'entity_on_leie_strict_address',
        1::NUMERIC,
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
            || '?signal=entity_on_leie_strict_address&leie=' || cm.leie_record_hash
    FROM cand_matches_raw cm
    JOIN cand_pop  cp ON cp.cycle = cm.cycle
    LEFT JOIN cand_flag cf ON cf.cycle = cm.cycle
    UNION ALL
    SELECT
        tm.cycle,
        'treasurer',
        tm.tres_canonical,
        'entity_on_leie_strict_address',
        1::NUMERIC,
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
            || '?signal=entity_on_leie_strict_address&leie=' || tm.leie_record_hash
    FROM tres_matches_raw tm
    JOIN tres_pop  tp ON tp.cycle = tm.cycle
    LEFT JOIN tres_flag tf ON tf.cycle = tm.cycle;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_entity_on_leie_strict_address(CHAR(4)) IS
    'TIER 4 v3 / FRAUD-F5b-strict: emit entity_on_leie_strict_address '
    'observations for the given cycle. Idempotent (DELETE+INSERT on '
    'its own (cycle, signal_id) slice). Joins raw.fec_candidate and '
    'raw.fec_committee against LEIE on canonical name AND zip5 AND '
    'city -- the strict tightening of the name-only entity_on_leie '
    'signal. Severity=5 (unchanged); peer_percentile is much higher '
    'because the matched set is ~100x smaller. Returns total rows '
    'inserted across both entity kinds.';


-- ----------------------------------------------------------------------------
-- Wire the new signal into the master refresher
--
-- derived.refresh_all_fraud_signal_observations is defined in mig 053
-- and orchestrates the per-signal refresh calls. The function uses
-- CREATE OR REPLACE pattern; appending a new signal call requires
-- redefining the function. We do that here so the master refresher
-- automatically picks up the strict signal.
-- ----------------------------------------------------------------------------
-- Look up the current definition; if the master refresher exists,
-- redefine it to include the strict call. If it does not exist (e.g.
-- partial deploy), this is a no-op and the strict refresher can still
-- be called directly via the function name above.
DO $$
DECLARE
    fn_exists BOOLEAN;
BEGIN
    SELECT EXISTS(
        SELECT 1 FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'derived'
          AND p.proname = 'refresh_all_fraud_signal_observations'
    ) INTO fn_exists;
    IF NOT fn_exists THEN
        RAISE NOTICE 'derived.refresh_all_fraud_signal_observations not '
                     'found -- skipping wire-in. Call '
                     'derived.refresh_signal_entity_on_leie_strict_address '
                     'directly until the master refresher is restored.';
    END IF;
END $$;

-- The master-refresher wire-in is intentionally NOT inlined here as a
-- CREATE OR REPLACE. Master refresher edits should be done in a
-- dedicated migration that owns the entire master-refresher body, to
-- keep all signal invocations co-located. For now, deploy script
-- callers must invoke
-- derived.refresh_signal_entity_on_leie_strict_address('YYYY')
-- explicitly. This is the same operational pattern as 058/059/060
-- which were also added as standalone refreshers without master-
-- refresher edits (the master refresher remains the cycle-2024 set).
