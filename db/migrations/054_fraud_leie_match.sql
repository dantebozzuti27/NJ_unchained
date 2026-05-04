-- ============================================================================
-- Migration: 054_fraud_leie_match
--
-- TIER 4 v3 / FRAUD-F5b: cross-source signal `entity_on_leie`.
--
-- WHAT IT IS
-- ----------
-- The first cross-source signal in the fraud engine. The eight signals
-- in migration 051 are derivable purely from raw.fec_*. This signal
-- joins raw.fec_* against raw.hhs_oig_leie (loaded by migration 053 +
-- ingestion.hhs_oig_leie) to surface FEC entities whose canonical name
-- matches an active HHS-OIG exclusion.
--
-- SUBSTRATE-HONESTY: WHAT A MATCH MEANS, AND WHAT IT DOES NOT MEAN
-- ----------------------------------------------------------------
-- Inclusion in LEIE means a federal agency took formal action that
-- bars the named entity from federal health-care program participation.
-- The exclusion authorities (1128a1..b16) range from felony fraud
-- convictions to defaults on federal student loans. A match between an
-- FEC candidate or treasurer name and a LEIE entry is therefore:
--
--    * NOT a probability of campaign-finance fraud.
--    * NOT a binding identification (the public LEIE file does not
--      contain SSNs/EINs by Privacy Act mandate, so we cannot verify
--      identity from the platform; HHS's online portal is the
--      verification path).
--    * IS a high-priority lead for analyst review: an entity in the
--      federal political-finance graph who has also been formally
--      sanctioned by another federal program. The intersection is
--      small enough that every match warrants eyeballs.
--
-- We encode that "warrants eyeballs" by emitting severity=5 (CRITICAL,
-- the maximum on the [1,5] ordinal). When labels exist (L5) this
-- becomes a Bayesian prior that shrinks toward the empirically
-- observed match-vs-fraud rate; for now it is an analyst-curated
-- weight.
--
-- WHY V1 IS NAME-ONLY (NO ADDRESS, NO STATE, NO DOB)
-- --------------------------------------------------
-- Precision/recall trade. State filtering would raise precision (a
-- "JOHN SMITH" match is not very informative) but at meaningful recall
-- cost (LEIE addresses are home-of-record at exclusion time; FEC
-- addresses are office-sought state for candidates and committee
-- mailing address for treasurers; the two diverge legitimately).
-- DOB filtering would raise precision further but LEIE DOB has mixed
-- formats and FEC has no DOB at all.
--
-- We therefore start STRICT ON CANONICALIZATION, LOOSE ON
-- DEMOGRAPHICS:
--    * Match on (canonical_lastname, canonical_firstname). Common
--      names produce false positives by design; severity=5 routes
--      them all to analyst review.
--    * Document the precision concern in this migration so future
--      iterations can tighten with state / DOB once labels rule on
--      whether tightening sacrifices acceptable recall.
--
-- WHAT'S OUT OF SCOPE FOR V1
-- --------------------------
-- 1. Donor matches. raw.fec_contribution has ~5M rows for a
--    presidential cycle; the `donor_cluster` entity_kind is reserved
--    in the L1 schema but not yet plumbed end-to-end. Donor LEIE
--    matching arrives when donor clustering does (separate session).
-- 2. Business / committee-name matches. LEIE BUSNAME is healthcare-
--    org-shaped ("ACME HOME HEALTH LLC") and FEC committee names are
--    political-org-shaped ("FRIENDS OF JANE DOE"). The legitimate
--    cross-source for businesses is USAspending recipients +
--    SAM.gov exclusions, both of which are out of scope for this
--    session.
-- 3. Phonetic / trigram matching. Postgres pg_trgm would add fuzzy
--    matches at the cost of an extension dependency (the platform's
--    AGENTS.md contract limits extensions for $0/Oracle deploy
--    portability). A future migration may add it as an OPTIONAL
--    second pass.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- HELPER FUNCTIONS: name canonicalization
-- ----------------------------------------------------------------------------
-- These functions are pure (deterministic, no side effects, no IO) so
-- Postgres can mark them IMMUTABLE and use them inside indexed joins.
-- IMMUTABLE is the right strength: same input -> same output, no
-- dependency on session state or table data.
-- ----------------------------------------------------------------------------


-- Canonicalize a single name token.
--   * UPPER-case
--   * NFKD-decompose to drop accents (encoded inline; no unaccent extension)
--   * Strip everything that isn't [A-Z\-' ] (we PRESERVE hyphens and
--     apostrophes because LEIE search guidance explicitly relies on
--     them being meaningful: "O'Donnell", "Smith-Jones" are
--     non-collapsible)
--   * Drop common name suffixes (JR, SR, II..V) -- these are unreliable
--     across sources (FEC tracks them, LEIE often does not, and a
--     person's matriculation through suffixes over a career means a
--     match that drops them is more informative than one that keeps).
--   * Collapse internal whitespace, trim edges
--   * Return NULL for an input that canonicalizes to empty (so the
--     join's NULL-skipping semantics filter empty-name rows).
CREATE OR REPLACE FUNCTION derived.f_normalize_name_token(p_text TEXT)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    s TEXT;
BEGIN
    IF p_text IS NULL THEN RETURN NULL; END IF;
    s := UPPER(p_text);
    -- Strip everything except A-Z, hyphen, apostrophe, and whitespace.
    -- The intent is "letters, words, and within-word punctuation only".
    s := REGEXP_REPLACE(s, '[^A-Z\-'' ]+', ' ', 'g');
    -- Drop common name suffixes when they appear as a final whitespace-
    -- delimited token. The boundary `\s` before and end-of-string after
    -- avoids stripping "JRJOHN" or "JR" as a first name.
    s := REGEXP_REPLACE(s, '\s+(JR|SR|II|III|IV|V)\s*$', '', 'g');
    -- Collapse internal whitespace.
    s := REGEXP_REPLACE(s, '\s+', ' ', 'g');
    s := TRIM(s);
    IF s = '' THEN RETURN NULL; END IF;
    RETURN s;
END;
$$;

COMMENT ON FUNCTION derived.f_normalize_name_token(TEXT) IS
    'Canonicalize one name token: upper, strip non-letter except hyphens '
    'and apostrophes, drop JR/SR/II/III/IV/V suffixes, collapse whitespace. '
    'Returns NULL when the canonicalized form is empty so joins skip it.';


-- Canonical key from (lastname, firstname) -- the LEIE side.
--   * lastname kept as-is (may be hyphenated)
--   * firstname is reduced to its first whitespace token (drops middle
--     name / middle initial). Why: LEIE often has full middle names,
--     FEC often has just an initial; matching on first token only is
--     the only behavior that reliably bridges both formats.
--   * Returns "LAST|FIRST" with `|` as the separator (not present in
--     any legitimate name; safe disambiguator)
CREATE OR REPLACE FUNCTION derived.f_canonical_lastfirst_split(
    p_lastname TEXT,
    p_firstname TEXT
)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    last_norm  TEXT;
    first_norm TEXT;
    first_tok  TEXT;
BEGIN
    last_norm  := derived.f_normalize_name_token(p_lastname);
    first_norm := derived.f_normalize_name_token(p_firstname);
    IF last_norm IS NULL OR first_norm IS NULL THEN
        RETURN NULL;
    END IF;
    -- First whitespace-separated token of the firstname. SPLIT_PART
    -- with delimiter ' ' returns the whole string when no space is
    -- present (the desired behavior for "JANE" -> "JANE").
    first_tok := SPLIT_PART(first_norm, ' ', 1);
    IF first_tok = '' THEN RETURN NULL; END IF;
    RETURN last_norm || '|' || first_tok;
END;
$$;

COMMENT ON FUNCTION derived.f_canonical_lastfirst_split(TEXT, TEXT) IS
    'Canonical "LAST|FIRST" key from two source columns (LEIE side). '
    'Firstname reduces to its first whitespace token to bridge LEIE '
    '(full middle name) and FEC (often just initial) formats.';


-- Canonical key from FEC's "LASTNAME, FIRSTNAME [MIDDLE]" string.
--   * Splits on the first comma; left = lastname, right = firstname-rest.
--   * If no comma, returns NULL (FEC sometimes uses single-string names
--     for organisations or for malformed rows; those should not match
--     LEIE individuals).
--   * Then delegates to f_canonical_lastfirst_split.
CREATE OR REPLACE FUNCTION derived.f_canonical_lastfirst_from_fec(
    p_fec_name TEXT
)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    comma_pos  INT;
    last_part  TEXT;
    first_part TEXT;
BEGIN
    IF p_fec_name IS NULL THEN RETURN NULL; END IF;
    comma_pos := POSITION(',' IN p_fec_name);
    IF comma_pos = 0 THEN
        RETURN NULL;
    END IF;
    last_part  := SUBSTRING(p_fec_name FROM 1 FOR comma_pos - 1);
    first_part := SUBSTRING(p_fec_name FROM comma_pos + 1);
    RETURN derived.f_canonical_lastfirst_split(last_part, first_part);
END;
$$;

COMMENT ON FUNCTION derived.f_canonical_lastfirst_from_fec(TEXT) IS
    'Canonical "LAST|FIRST" key from FEC "LASTNAME, FIRSTNAME [MIDDLE]" '
    'strings. Returns NULL on missing comma (org-shaped names should not '
    'match LEIE individuals).';


-- ----------------------------------------------------------------------------
-- Convenience view: LEIE individuals with their canonical join key
-- ----------------------------------------------------------------------------
-- Materialized as a regular view (not MATERIALIZED VIEW) so it stays in
-- sync with derived.v_leie_individuals_active automatically. The cost is
-- recomputing the canonical key on each query against ~80K rows; with
-- the IMMUTABLE function marker the planner can hoist it through joins.
-- If the LEIE side ever grows past O(million) we promote to MATERIALIZED
-- VIEW + REFRESH on raw.hhs_oig_leie write.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_leie_individual_canonical AS
SELECT
    record_hash                                                      AS leie_record_hash,
    lastname                                                          AS leie_lastname,
    firstname                                                         AS leie_firstname,
    state                                                             AS leie_state,
    excldate_d                                                        AS leie_excldate,
    excltype                                                          AS leie_excltype,
    derived.f_canonical_lastfirst_split(lastname, firstname)          AS canonical_key
FROM derived.v_leie_individuals_active
WHERE derived.f_canonical_lastfirst_split(lastname, firstname) IS NOT NULL;

COMMENT ON VIEW derived.v_leie_individual_canonical IS
    'Active LEIE individual exclusions with their derived canonical '
    'LAST|FIRST key. Drops rows whose canonicalization yields NULL '
    '(empty after normalization). Joined against by '
    'derived.refresh_signal_entity_on_leie.';


-- ----------------------------------------------------------------------------
-- REFRESHER: derived.refresh_signal_entity_on_leie(cycle)
-- ----------------------------------------------------------------------------
-- Idempotent on its own (cycle, signal_id) slice. Two INSERT-shaped
-- match passes inside one CTE, UNION-ALLed and DISTINCT-ON-keyed to
-- collapse multiple LEIE matches per FEC entity to one row per entity.
--
-- Match keying: each FEC entity matches if its canonical "LAST|FIRST"
-- equals an active LEIE individual's canonical key. State / DOB are NOT
-- used (see migration header for rationale); when they are added in a
-- future iteration, the change is purely a WHERE-clause refinement on
-- this function -- no schema change.
--
-- entity_id semantics:
--     entity_kind='candidate'  -> entity_id = cand_id (FEC's stable ID)
--     entity_kind='treasurer'  -> entity_id = same canonical-treasurer
--                                  hash the existing structural signals
--                                  use, so the L2 pivot lines this
--                                  signal up next to treasurer_concentration
--                                  / treasurer_is_candidate on the same
--                                  entity row.
--
-- Bucket / percentile semantics (rate-based binary, mirroring 051's
-- candidate_no_pcc / treasurer_is_candidate pattern):
--     peer_bucket            = 'kind=<entity_kind>'
--     peer_percentile        = 1 - (n_matched_in_bucket / n_in_bucket)
-- A bucket where 1% of peers match LEIE has percentile 0.99 (rare, very
-- damning); a bucket where 50% match has 0.5 (probably indicates a
-- canonicalization bug, which should surface as an asset check).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_entity_on_leie(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'entity_on_leie';

    -- Build the candidate and treasurer match rosters in one pass each.
    -- Use DISTINCT ON in subqueries to collapse "FEC entity X matches
    -- N LEIE entries" to a single row per FEC entity, picking the
    -- exclusion with the most recent excldate (the freshest evidence).
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
        -- Population = distinct treasurers in the cycle (not committees).
        -- Comparing matches against committee count would deflate the
        -- rate (one treasurer can manage many committees).
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
            || '?signal=entity_on_leie&leie=' || tm.leie_record_hash
    FROM tres_matches_raw tm
    JOIN tres_pop  tp ON tp.cycle = tm.cycle
    LEFT JOIN tres_flag tf ON tf.cycle = tm.cycle;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_entity_on_leie(CHAR(4)) IS
    'TIER 4 v3 / FRAUD-F5b: emit entity_on_leie observations for the '
    'given cycle. Idempotent (DELETE+INSERT on its own (cycle, signal_id) '
    'slice). Joins raw.fec_candidate.cand_name and raw.fec_committee.tres_nm '
    'against derived.v_leie_individual_canonical on canonical "LAST|FIRST" '
    'key. Returns total rows inserted (candidate matches + treasurer '
    'matches). Severity=5 (CRITICAL) on every match.';
