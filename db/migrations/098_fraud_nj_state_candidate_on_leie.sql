-- =============================================================================
-- Migration 098: nj_state_candidate_on_leie cross-source fraud signal
--
-- VISION_2026 Pillar 2 (civic integrity) -- Phase F8.5-cross-source.
-- Closes the architectural gap mig 093 explicitly punted on: until today
-- the substrate-honest NJ-state-candidate roster (10 publicly-announced
-- 2025 NJ gubernatorial primary candidates seeded by 022 + 093) lived
-- in `ref.nj_state_candidate` but produced ZERO fraud_signal_observation
-- rows because every existing signal consumed FEC-bulk substrate
-- (raw.fec_*) and the NJ-state roster is not in FEC. The mig 093 header
-- documented this gap (lines 45-52, "Wire nj_state_candidate into
-- derived.fraud_signal_observation as a new entity_kind ... would create
-- dead enum values -- worse than waiting").
--
-- The wait is over for the LEIE cross-source signal specifically.
-- entity_on_leie (mig 054) is name-only -- it does NOT need FEC donor
-- or committee substrate. It only needs the candidate's name. The NJ-
-- state roster has full_name. So this signal CAN fire against NJ state
-- candidates today, with zero new ingester dependency, and the
-- substrate-honest scope is bounded: name-only LEIE matches.
--
-- WHY THIS IS WORTH SHIPPING NOW (and not waiting for ELEC ingest)
-- ----------------------------------------------------------------
-- The 10 announced 2025 NJ gubernatorial primary candidates include
-- multiple incumbent federal officials (Sherrill, Gottheimer, Kim) and
-- multiple long-tenured local officials (Fulop, Sweeney, Spiller). Any
-- one of them sharing a canonical LAST|FIRST with an active LEIE
-- exclusion is a high-priority analyst lead -- whether or not the
-- platform has loaded their state-level donor graph. The same precision-
-- vs-recall trade documented in mig 054 applies (common-name false
-- positives by design; severity=5 routes them to analyst review). That
-- trade is acceptable because severity-5 cards were always meant to be
-- analyst-reviewed, not auto-actioned.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- 1. CHECK constraint widening: derived.fraud_signal_observation
--    .entity_kind gains 'nj_state_candidate' as an allowed value.
--    Drop+recreate is the only path -- ALTER TABLE ... ADD CHECK with
--    a different definition is rejected by Postgres if the constraint
--    name already exists.
--
-- 2. Canonicalizer derived.f_canonical_lastfirst_from_first_last(TEXT):
--    parses "First [Middle] Last" / "First Last-Hyphenated" shape and
--    delegates to the existing f_canonical_lastfirst_split. Mirrors
--    f_canonical_lastfirst_from_fec semantically -- SAME canonical key
--    output for the SAME identity, just different input parsing.
--    IMMUTABLE PARALLEL SAFE for indexable joins.
--
-- 3. Refresher derived.refresh_signal_nj_state_candidate_on_leie(CHAR(4)):
--    idempotent DELETE+INSERT slice. Reads ref.nj_state_candidate
--    filtered by election_year = CAST(p_cycle AS INT). Joins
--    derived.v_leie_individual_canonical on the canonical key. Emits
--    one observation per (entity_kind='nj_state_candidate', entity_id,
--    signal_id='nj_state_candidate_on_leie') with severity=5
--    (consequence-tier identical to entity_on_leie -- federal
--    enforcement overlap is the same evidence shape).
--
--    Population for percentile = COUNT of rows in ref.nj_state_candidate
--    for the same election_year that have a non-NULL canonical key
--    (substrate-honest: candidates whose name fails canonicalization
--    are not in the universe the signal could fire on, so they should
--    not inflate the denominator). When the population is empty, the
--    function emits 0 observations cleanly (no division-by-zero --
--    NULLIF + GREATEST handle it the same way 054 does).
--
-- 4. Master refresher wiring: derived.refresh_all_fraud_signal_observations
--    gains a new SELECT call into the new refresher in the LEIE-bearing
--    tier. Substrate-honest no-op when the cycle is FEC-shaped (2024,
--    2026) -- the new refresher's election_year filter returns 0 rows.
--
-- 5. fraud_signal_config seeding: registers signal_id with family
--    'leie_bearing' and threshold 0 (binary signal; matches 054).
--
-- 6. derived.v_entity_fraud_evidence widened to recognize the new
--    entity_kind. Adds a `nj_state_meta` CTE LEFT JOIN that resolves
--    display_name (full_name) and is_nj=TRUE (every nj_state_candidate
--    is by definition NJ).
--
-- WHAT GOES IN THE COMPANION SEED 022_nj_state_candidate_on_leie_seed
-- -------------------------------------------------------------------
-- (Three reference rows -- mirroring seed 021's pattern):
-- * ref.fraud_signal_human_explanation: rule_text, citation_authority,
--   plain-English template
-- * ref.fraud_signal_severity_calibration: severity_level=5,
--   calibration_basis='oig_report'
-- * ref.fraud_signal_evidence_url_template: same OIG.gov LEIE search
--   landing page as the loose entity_on_leie variant (POST-shaped
--   search form, no deep-link viable)
--
-- WHAT THIS MIGRATION DELIBERATELY DOES NOT DO
-- --------------------------------------------
-- * Add a strict-address variant. NJ-state candidates do not have
--   addresses in ref.nj_state_candidate -- the roster carries
--   announcement-citation provenance, not residential filings. The
--   strict variant arrives if/when ELEC ingest provides committee
--   addresses for these candidates.
-- * Add a Dagster sensor that calls the master refresher with
--   cycle='2025'. The bi-weekly schedule (mig 097) only triggers
--   cycle='2026'. NJ-state cycle is operator-driven for now -- a
--   separate annual schedule is a future work item, gated on whether
--   NJ ELEC results / refilings produce intra-cycle changes worth
--   tracking automatically.
-- * Touch the Next.js UI. lib/queries.ts + lib/types.ts +
--   app/risk/[kind]/[id]/page.tsx are updated in companion code
--   commits, not in this migration -- schema changes ship indepen-
--   dently of presentation code.
--
-- IDEMPOTENT VIA CREATE OR REPLACE FUNCTION + ON CONFLICT clauses +
-- governance.schema_migrations sha256 ledger. Safe to re-run.
-- =============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.7.1-fraud-nj-state-candidate-on-leie-v1',
    'Pillar 2 (civic integrity) Phase F8.5-cross-source. Adds the '
    'nj_state_candidate_on_leie cross-source signal -- name-only LEIE '
    'match for the 10 publicly-announced 2025 NJ gubernatorial primary '
    'candidates (and forward-compat for any NJ-state-candidate row in '
    'ref.nj_state_candidate that has a non-NULL canonical key). Severity '
    '5 (consequence-tier identical to entity_on_leie); rate-based '
    'percentile within the small NJ-state-candidate bucket so even one '
    'match yields percentile 0.9 against a 10-candidate population. '
    'Closes the F8.5 substrate-honesty gap (mig 093 lines 45-52) where '
    'NJ-state candidates lived in ref.nj_state_candidate but produced '
    'ZERO fraud_signal_observation rows because every prior signal '
    'consumed FEC-bulk substrate. This is the FIRST signal that fires '
    'against entity_kind=nj_state_candidate.',
    '2026-05-12'::DATE,
    'Stacks on 2.7.0-fraud-signal-drift-baseline-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 1. CHECK constraint widening on derived.fraud_signal_observation.entity_kind
--
-- The current constraint allows {committee, candidate, treasurer, address,
-- donor_cluster, contractor, donor}. We add 'nj_state_candidate' so the
-- new refresher can INSERT without a constraint violation. ALTER TABLE
-- DROP CONSTRAINT + ADD CONSTRAINT is the canonical Postgres path -- a
-- single ALTER ... USING (...) is not supported for CHECK constraints.
--
-- The DROP+ADD is atomic inside the BEGIN/COMMIT, so no observation
-- row can be inserted with the old (narrower) constraint and then
-- become orphaned by a stricter rebuild.
-- ----------------------------------------------------------------------------
ALTER TABLE derived.fraud_signal_observation
    DROP CONSTRAINT IF EXISTS fraud_signal_observation_entity_kind_check;

ALTER TABLE derived.fraud_signal_observation
    ADD CONSTRAINT fraud_signal_observation_entity_kind_check
    CHECK (entity_kind = ANY (ARRAY[
        'committee'::TEXT,
        'candidate'::TEXT,
        'treasurer'::TEXT,
        'address'::TEXT,
        'donor_cluster'::TEXT,
        'contractor'::TEXT,
        'donor'::TEXT,
        'nj_state_candidate'::TEXT
    ]));

COMMENT ON CONSTRAINT fraud_signal_observation_entity_kind_check
    ON derived.fraud_signal_observation IS
    'Whitelist of entity_kind values. nj_state_candidate added by '
    'mig 098 to support cross-source LEIE signal against the NJ-state '
    'roster (ref.nj_state_candidate). Future kinds (donor_cluster, '
    'contractor) remain reserved but not yet plumbed to a refresher.';


-- ----------------------------------------------------------------------------
-- 2. derived.f_canonical_lastfirst_from_first_last(TEXT)
--
-- Canonical "LAST|FIRST" key from a "First [Middle] Last" shaped name.
-- Parses by whitespace: last token = lastname, first token = firstname,
-- middle tokens dropped. Suffix stripping (JR/SR/II/III/IV/V) is
-- delegated to f_normalize_name_token which already handles trailing
-- suffixes; we strip suffixes BEFORE splitting so "Bill Pascrell Jr"
-- canonicalizes as ("Pascrell", "Bill") not ("Jr", "Bill").
--
-- Edge cases (substrate-honest):
--   * Single-token names ("Beyonce", "Sting") -> NULL. The signal
--     cannot match an active LEIE individual exclusion (which has
--     last + first by Privacy Act mandate) without both halves of
--     the name. Returning NULL drops the row from the join cleanly.
--   * Compound surnames with particles ("de la Vega", "van Drew",
--     "von Bismarck") -> takes the last token only ("Vega", "Drew",
--     "Bismarck"). This is a known false-negative shape (a particle-
--     dropped surname will not match the LEIE if LEIE preserved the
--     particle). Acceptable for v1: substrate-honest false negatives
--     beat false positives at severity=5. v2 can add a particle-
--     aware split if labels prove the recall cost is meaningful.
--   * Hyphenated last names ("Smith-Jones") -> preserved by
--     f_normalize_name_token's regex (hyphens are kept).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.f_canonical_lastfirst_from_first_last(
    p_full_name TEXT
)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    cleaned TEXT;
    n_toks  INT;
    last_t  TEXT;
    first_t TEXT;
BEGIN
    IF p_full_name IS NULL THEN RETURN NULL; END IF;
    -- Normalize first (handles upper, suffix-strip, punctuation, ws-collapse).
    -- f_normalize_name_token returns NULL on empty / pure-punctuation input.
    cleaned := derived.f_normalize_name_token(p_full_name);
    IF cleaned IS NULL THEN RETURN NULL; END IF;

    -- Split on whitespace.
    n_toks := array_length(string_to_array(cleaned, ' '), 1);
    IF n_toks IS NULL OR n_toks < 2 THEN
        -- Single-token names cannot produce a (last, first) pair.
        -- Return NULL so the join skips it (substrate-honest).
        RETURN NULL;
    END IF;

    first_t := SPLIT_PART(cleaned, ' ', 1);
    last_t  := SPLIT_PART(cleaned, ' ', n_toks);

    -- Defensive: if either half is empty after split (shouldn't happen
    -- post-canonicalization but guards against future regex changes),
    -- return NULL.
    IF first_t = '' OR last_t = '' THEN
        RETURN NULL;
    END IF;

    -- Delegate to the existing canonical key builder so the output
    -- format is BIT-IDENTICAL to f_canonical_lastfirst_split (LEIE
    -- side) and f_canonical_lastfirst_from_fec (FEC side). One canonical
    -- key shape across all three input families.
    RETURN derived.f_canonical_lastfirst_split(last_t, first_t);
END;
$$;

COMMENT ON FUNCTION derived.f_canonical_lastfirst_from_first_last(TEXT) IS
    'Canonical "LAST|FIRST" key from a "First [Middle] Last" shaped '
    'name (used by ref.nj_state_candidate.full_name). Last token = '
    'lastname, first token = firstname, middle tokens dropped. '
    'Suffix-strip via f_normalize_name_token. Returns NULL for single-'
    'token names (cannot produce a last,first pair). Output format '
    'identical to f_canonical_lastfirst_split / _from_fec so all '
    'three input families share one canonical key shape. Mig 098.';


-- ----------------------------------------------------------------------------
-- 3. derived.refresh_signal_nj_state_candidate_on_leie(p_cycle CHAR(4))
--
-- Idempotent DELETE+INSERT slice on (cycle, signal_id). Filters
-- ref.nj_state_candidate WHERE election_year = CAST(p_cycle AS INT) so
-- a master-refresher call with cycle='2024' or '2026' is a clean no-op
-- (no NJ state cycle ever falls on an even year given NJ holds odd-year
-- gubernatorial elections).
--
-- Severity = 5 (CRITICAL) -- consequence-tier identical to the loose
-- entity_on_leie signal (mig 054). Evidence STRENGTH is captured by
-- peer_percentile, which lands very high for this signal because the
-- bucket size is small (~10-50 candidates per election year vs ~17K
-- federal candidates) -- one match against a 10-candidate roster
-- yields percentile 0.9, against a 50-candidate roster yields 0.98.
--
-- Returns INT (rows inserted). 0 when:
--   * The cycle has no rows in ref.nj_state_candidate (e.g. cycle 2024
--     today, before the NJ ELEC ingester or another curated year lands)
--   * The LEIE substrate is empty (no v_leie_individual_canonical rows)
--   * No name match between the two
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_nj_state_candidate_on_leie(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
    p_year     INT;
BEGIN
    -- Always clear the slice first (idempotency).
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'nj_state_candidate_on_leie';

    -- Cycle must look like a 4-digit year. The CHECK on the column
    -- already guarantees the regex shape; this CAST guards against a
    -- leading-zero anomaly that would silently coerce.
    BEGIN
        p_year := CAST(p_cycle AS INT);
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN 0;
    END;

    WITH leie AS (
        SELECT canonical_key, leie_record_hash, leie_excldate
        FROM derived.v_leie_individual_canonical
    ),
    -- Match each NJ state candidate against the LEIE on canonical key.
    -- DISTINCT ON collapses multiple LEIE matches per candidate to one
    -- observation per candidate, picking the freshest LEIE record by
    -- excldate (mirrors 054's preference for the most recent evidence).
    nj_matches_raw AS (
        SELECT DISTINCT ON (c.candidate_id)
            c.candidate_id,
            c.election_year,
            c.full_name,
            l.leie_record_hash,
            l.leie_excldate
        FROM ref.nj_state_candidate c
        JOIN leie l
          ON l.canonical_key
             = derived.f_canonical_lastfirst_from_first_last(c.full_name)
        WHERE c.election_year = p_year
          AND c.full_name IS NOT NULL
        ORDER BY c.candidate_id, l.leie_excldate DESC NULLS LAST
    ),
    -- Population: NJ state candidates in this election year whose
    -- canonical key is non-NULL (i.e. the signal could plausibly fire
    -- on them). Substrate-honest -- candidates whose name fails the
    -- canonicalizer (single-token, all-punctuation, etc.) are not in
    -- the universe and excluding them does not deflate the rate.
    nj_pop AS (
        SELECT COUNT(*) AS n_in_bucket
        FROM ref.nj_state_candidate
        WHERE election_year = p_year
          AND derived.f_canonical_lastfirst_from_first_last(full_name)
              IS NOT NULL
    ),
    nj_flag AS (
        SELECT COUNT(*) AS n_flagged FROM nj_matches_raw
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        p_cycle,
        'nj_state_candidate',
        m.candidate_id,
        'nj_state_candidate_on_leie',
        1::NUMERIC,
        5::SMALLINT,
        'kind=nj_state_candidate',
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (
                f.n_flagged::NUMERIC
                / NULLIF(p.n_in_bucket, 0)::NUMERIC
            )
        ),
        '/fec/risk/entities/nj_state_candidate/' || m.candidate_id
            || '?signal=nj_state_candidate_on_leie&leie='
            || m.leie_record_hash
    FROM nj_matches_raw m
    CROSS JOIN nj_pop  p
    CROSS JOIN nj_flag f;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_nj_state_candidate_on_leie(CHAR(4)) IS
    'Pillar 2 / Phase F8.5-cross-source: emit nj_state_candidate_on_leie '
    'observations for the given cycle. Reads ref.nj_state_candidate '
    'WHERE election_year = CAST(p_cycle AS INT); joins LEIE individuals '
    'on canonical LAST|FIRST. Severity=5 (CRITICAL); rate-based per-'
    'centile within the (small) nj_state_candidate bucket. Idempotent '
    '(DELETE+INSERT on its (cycle, signal_id) slice). Returns total '
    'rows inserted. Mig 098.';


-- ----------------------------------------------------------------------------
-- 4. fraud_signal_config row registration
--
-- family='leie_bearing' (matches the 4 existing LEIE-bearing signals).
-- threshold=0 (binary signal; raw_value is always 1). The min_actionable
-- threshold floor in v_entity_fraud_features (mig 061) does NOT drop a
-- value-of-1 row at threshold-0, so every match is actionable.
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES (
    'nj_state_candidate_on_leie',
    'leie_bearing',
    0,
    'Cross-source name-only LEIE match against ref.nj_state_candidate '
    '(NJ statewide / state-legislative roster). Mirrors entity_on_leie '
    'in shape (binary, severity=5, rate-based percentile) but emits '
    'entity_kind=nj_state_candidate so the analyst queue surfaces NJ-'
    'state matches alongside FEC ones. Bucket population is small '
    '(~10-50 candidates per election year), so matches yield very '
    'high percentile -- a single match against a 10-candidate roster '
    'lands at percentile 0.9.'
)
ON CONFLICT (signal_id) DO NOTHING;


-- ----------------------------------------------------------------------------
-- 5. Master refresher: derived.refresh_all_fraud_signal_observations
--
-- Wire the new refresher into the LEIE-bearing tier. Call is a no-op
-- when p_cycle is FEC-shaped (2024, 2026) because election_year filter
-- returns 0 rows. Substrate-honest single source of truth: the master
-- now invokes 19 of 19 seeded signal refreshers.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_all_fraud_signal_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_total INT := 0;
    n_each  INT;
BEGIN
    -- ----------------------------------------------------------------
    -- TIER 1: FEC-bulk-only structural signals (8)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_treasurer_concentration_observations(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_no_pcc_observations(p_cycle)           INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_broken_pcc_observations(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_multiple_pccs_observations(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_committee_address_clusters_observations(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_committee_name_collisions_observations(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_namesakes_observations(p_cycle)        INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_treasurer_is_candidate_observations(p_cycle)     INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 2: LEIE-bearing signals (NOW 5 -- adds nj_state_candidate_on_leie)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_entity_on_leie(p_cycle)                   INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_entity_on_leie_strict_address(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_donor_on_leie(p_cycle)                    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_candidate_funded_by_excluded_donors(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- New as of mig 098: NJ-state-candidate cross-source LEIE match.
    SELECT derived.refresh_signal_nj_state_candidate_on_leie(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 3: SAM-bearing signals (3)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_entity_excluded_via_sam_uei(p_cycle)      INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_donor_on_sam(p_cycle)                     INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_candidate_funded_by_sam_excluded_donors(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 4: USAspending-bearing signals (3)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_entity_funded_and_excluded(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_candidate_funded_by_nj_contractor_employees(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_donor_employed_by_nj_contractor(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'Master fraud-signal refresher. Invokes all 19 seeded signal refreshers '
'in substrate-dependency tier order (FEC -> LEIE -> SAM -> USAspending) '
'for the given cycle. Each per-signal refresher is an idempotent '
'DELETE+INSERT slice that returns INT (rows inserted). The master '
'returns SUM. Refreshers against empty raw substrate (SAM, USAspending '
'today) safely return 0. Refreshers whose substrate is keyed on a '
'different cycle (nj_state_candidate_on_leie filters election_year) '
'safely return 0 when called for an FEC even-year cycle. Mig 098 raises '
'the count from 18 to 19 by adding nj_state_candidate_on_leie.';


-- ----------------------------------------------------------------------------
-- 6. derived.v_entity_fraud_evidence widening
--
-- Adds a `nj_state_meta` CTE LEFT JOIN so the evidence-card view can
-- resolve display_name (full_name) for the new entity_kind. is_nj is
-- TRUE by definition (every nj_state_candidate is by definition a NJ
-- candidate).
--
-- The view's column order MUST be preserved (Postgres rejects column
-- reorderings in CREATE OR REPLACE VIEW). We add the JOIN + CASE
-- branches; column list is unchanged.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_entity_fraud_evidence AS
WITH cand_meta AS (
    SELECT
        cycle,
        cand_id,
        cand_name,
        cand_office,
        cand_office_st,
        cand_office_district,
        cand_pty_affiliation,
        cand_ici,
        cand_status,
        cand_election_yr,
        (cand_office_st = 'NJ')                                  AS is_nj
    FROM raw.fec_candidate
),
cmte_meta AS (
    SELECT
        cycle,
        cmte_id,
        cmte_nm,
        cmte_st,
        cmte_city,
        cmte_zip,
        tres_nm,
        cand_id                                                  AS pcc_cand_id,
        (cmte_st = 'NJ')                                         AS is_nj
    FROM raw.fec_committee
),
treas_meta AS (
    SELECT
        cycle,
        UPPER(TRIM(tres_nm))                                     AS treasurer_id,
        BOOL_OR(cmte_st = 'NJ')                                  AS is_nj,
        COUNT(DISTINCT cmte_id)                                  AS n_committees_treasured,
        COUNT(DISTINCT cmte_id) FILTER (WHERE cmte_st = 'NJ')    AS n_nj_committees_treasured
    FROM raw.fec_committee
    WHERE tres_nm IS NOT NULL AND tres_nm <> ''
    GROUP BY 1, 2
),
nj_state_meta AS (
    -- Substrate-honest: NJ-state candidates are always NJ-relevant
    -- (the table is by definition NJ-only); display_name = full_name.
    -- The party + office_label are forward-compat extras the
    -- evidence-card UI may eventually surface (currently unused;
    -- adding columns here would require a synchronized type-update
    -- in lib/types.ts so we keep the view minimal).
    SELECT
        candidate_id                                              AS nj_candidate_id,
        full_name                                                 AS nj_full_name,
        TRUE                                                      AS is_nj
    FROM ref.nj_state_candidate
)
SELECT
    o.cycle,
    o.entity_kind,
    o.entity_id,
    o.signal_id,
    o.raw_value,
    COALESCE(sc.severity_level, o.severity)                      AS severity,
    o.peer_bucket,
    o.peer_percentile,
    o.materialized_at,

    CASE o.entity_kind
        WHEN 'candidate'          THEN COALESCE(cand.is_nj,  FALSE)
        WHEN 'committee'          THEN COALESCE(cmte.is_nj,  FALSE)
        WHEN 'treasurer'          THEN COALESCE(treas.is_nj, FALSE)
        WHEN 'address'            THEN (SPLIT_PART(o.entity_id, '|', 3) = 'NJ')
        WHEN 'nj_state_candidate' THEN COALESCE(nj.is_nj, TRUE)
        ELSE FALSE
    END                                                          AS is_nj,

    CASE o.entity_kind
        WHEN 'candidate'          THEN cand.cand_name
        WHEN 'committee'          THEN cmte.cmte_nm
        WHEN 'treasurer'          THEN o.entity_id
        WHEN 'address'            THEN SPLIT_PART(o.entity_id, '|', 1)
                                       || COALESCE(', ' || SPLIT_PART(o.entity_id, '|', 2), '')
                                       || COALESCE(' '  || SPLIT_PART(o.entity_id, '|', 3), '')
                                       || COALESCE(' '  || SPLIT_PART(o.entity_id, '|', 4), '')
        WHEN 'nj_state_candidate' THEN nj.nj_full_name
        ELSE o.entity_id
    END                                                          AS display_name,

    cand.cand_office                                             AS office_code,
    cand.cand_office_st                                          AS office_state,
    cand.cand_office_district                                    AS office_district,
    cand.cand_pty_affiliation                                    AS office_party,
    cand.cand_ici                                                AS office_incumbent_status,
    cand.cand_election_yr                                        AS office_election_year,

    treas.n_committees_treasured                                 AS treasurer_n_committees,
    treas.n_nj_committees_treasured                              AS treasurer_n_nj_committees,

    cmte.cmte_st                                                 AS committee_state,
    cmte.cmte_city                                               AS committee_city,
    cmte.tres_nm                                                 AS committee_treasurer_name,
    cmte.pcc_cand_id                                             AS committee_pcc_candidate_id,

    he.rule_text                                                 AS rule_text,
    he.citation_authority                                        AS citation_authority,
    he.citation_section                                          AS citation_section,
    he.citation_url                                              AS citation_url,

    REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
        COALESCE(he.plain_english_template, ''),
        '{{entity_id}}',       o.entity_id),
        '{{cycle}}',           o.cycle),
        '{{raw_value}}',       COALESCE(o.raw_value::TEXT, '')),
        '{{peer_percentile}}', COALESCE(ROUND(o.peer_percentile * 100, 1)::TEXT, '')),
        '{{entity_kind}}',     COALESCE(o.entity_kind, '')),
        '{{peer_bucket}}',     COALESCE(o.peer_bucket, ''))
                                                                 AS rendered_explanation,

    sc.calibration_basis                                         AS severity_basis,
    sc.precedent_url                                             AS severity_precedent_url,
    sc.precedent_summary                                         AS severity_precedent_summary,

    REPLACE(REPLACE(
        COALESCE(eut.url_template, o.evidence_url),
        '{{entity_id}}', o.entity_id),
        '{{cycle}}',     o.cycle)
                                                                 AS upstream_verify_url,
    eut.button_label                                             AS upstream_verify_label,
    eut.upstream_source                                          AS upstream_source,

    he.formula_version                                           AS formula_version
FROM   derived.fraud_signal_observation        o
LEFT JOIN cand_meta                            cand
       ON o.entity_kind = 'candidate'
      AND cand.cycle    = o.cycle
      AND cand.cand_id  = o.entity_id
LEFT JOIN cmte_meta                            cmte
       ON o.entity_kind = 'committee'
      AND cmte.cycle    = o.cycle
      AND cmte.cmte_id  = o.entity_id
LEFT JOIN treas_meta                           treas
       ON o.entity_kind     = 'treasurer'
      AND treas.cycle       = o.cycle
      AND treas.treasurer_id = UPPER(TRIM(o.entity_id))
LEFT JOIN nj_state_meta                        nj
       ON o.entity_kind     = 'nj_state_candidate'
      AND nj.nj_candidate_id = o.entity_id
LEFT JOIN ref.fraud_signal_human_explanation        he   ON he.signal_id  = o.signal_id
LEFT JOIN ref.fraud_signal_severity_calibration     sc   ON sc.signal_id  = o.signal_id
LEFT JOIN ref.fraud_signal_evidence_url_template    eut  ON eut.signal_id = o.signal_id;

COMMENT ON VIEW derived.v_entity_fraud_evidence IS
    'Canonical join from fraud_signal_observation -> rendered plain-English '
    'explanation + federal-authority citation + severity precedent + display '
    'metadata + NJ-relevance + upstream-verify URL. One row per fired signal. '
    'Mig 098 widens the entity_kind handling to include nj_state_candidate '
    '(LEFT JOIN ref.nj_state_candidate via nj_state_meta CTE). Stacks on '
    '2.7.0-fraud-signal-drift-baseline-v1; mig 098 adds the F8.5-cross-'
    'source LEIE substrate.';


COMMIT;
