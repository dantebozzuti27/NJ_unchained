-- ============================================================================
-- Migration: 051_fraud_signal_observation_adapter
--
-- TIER 4 v3 STEP 2: re-emit each v2.A structural signal (migration 040)
-- as rows in derived.fraud_signal_observation (migration 050). After
-- this migration, calling derived.refresh_all_fraud_signal_observations(
-- '2024') populates L1 with one observation per (entity, signal) for
-- every entity that fired in cycle 2024.
--
-- DESIGN CHOICES (substrate-honesty)
-- ----------------------------------
-- 1. ONE FUNCTION PER SIGNAL, ONE DISPATCHER. Each adapter is a self-
--    contained idempotent INSERT INTO ... SELECT ... that DELETEs its
--    own (cycle, signal_id) slice first, so re-running it for the same
--    cycle is safe. The dispatcher calls all eight in a single
--    transaction so partial-state corruption cannot occur on failure.
--
-- 2. RATE-BASED PERCENTILE FOR BINARY SIGNALS. candidate_no_pcc,
--    candidate_broken_pcc, and treasurer_is_candidate are flag-or-not
--    -- raw_value carries no rank information across flagged entities.
--    For those:
--        peer_percentile = 1 - (n_flagged_in_bucket / n_bucket_population)
--    A flag that 1% of bucket peers also have is at percentile 0.99
--    (rare flags = strong signal); a flag that 50% of peers also have
--    is at 0.5 (common flags = weak signal). The denominator is the
--    bucket population from the underlying raw table, NOT the count of
--    flagged rows -- otherwise rare flags would saturate at 1.0
--    regardless of how rare they actually are.
--
-- 3. CUME_DIST FOR CONTINUOUS SIGNALS. treasurer_concentration's
--    n_committees, address_clusters' n_committees-per-address, etc.
--    use:
--        CUME_DIST() OVER (PARTITION BY peer_bucket ORDER BY raw_value)
--    Returns "fraction of peers at-or-below this value", so 1.0 = the
--    largest in the bucket. PERCENT_RANK would give the smallest rank
--    0.0; CUME_DIST is the right choice for "is this entity in the
--    extreme tail of its peers" semantics.
--
-- 4. PEER BUCKET CHOICES (work_left.txt 2026-05-04 design pin).
--    Single-cycle (2024) data has too few candidates per
--    (office, state, ici) to make ICI bucketing reliable; we use
--    (office, state) for candidate-keyed signals. Add ICI when
--    multi-cycle data lands.
--
--    Per signal:
--      treasurer_concentration       'kind=treasurer'
--      candidate_no_pcc              'office=H|state=NJ' (etc.)
--      candidate_broken_pcc          same shape as no_pcc
--      candidate_multiple_pccs       same shape as no_pcc
--      committee_address_clusters    'state=NJ' (etc.)
--      committee_name_collisions     'state=<cmte_st>'
--      candidate_namesakes           'office=<>|state=<>'
--      treasurer_is_candidate        'office=<linked>|state=<linked>'
--
-- 5. SEVERITY (analyst-curated ordinal in [1, 5]).
--      treasurer_concentration   3   suggestive but common in normal practice
--      candidate_no_pcc          1   most are sub-$5k legitimate filings
--      candidate_broken_pcc      2   data integrity, not necessarily fraud
--      candidate_multiple_pccs   2   usually transition artifact
--      committee_address_clusters 4  genuinely concerning at >= 3 cmtes/addr
--      committee_name_collisions  3  confusion vector for downstream donor tracking
--      candidate_namesakes        3  same-state same-office namesakes are leads
--      treasurer_is_candidate     1  legitimate for small local campaigns
--
-- 6. ENTITY MAPPING. Each signal emits to its NATURAL entity (treasurer
--    for treasurer-concentration, address for address-clusters, the
--    fanned-out committee/candidate ids for collision/namesake groups,
--    etc.). The evidence panel will fan out to related entities at
--    render time (e.g., a committee's panel queries L1 for both its own
--    cmte_id AND its treasurer's tres_nm_canonical), keeping L1 small.
--
-- 7. TREASURER ENTITY ID = canonical name only (limitation, documented).
--    Until ref.fec_treasurer (a future canonical entity table) exists,
--    we identify treasurers by REGEXP_REPLACE(UPPER(TRIM(tres_nm)),
--    '\s+', ' ', 'g'). Cross-state name collisions (a "DOE, JOHN" in
--    NJ vs a "DOE, JOHN" in TX) are conflated. Acceptable for v3 step 2;
--    promote to a true canonical entity table in step 2.5.
--
-- 8. FUNCTIONS ARE plpgsql, NOT SQL FUNCTIONS. plpgsql lets us write
--    DELETE + INSERT in sequence within a single function body. Pure
--    SQL functions can only return a single SELECT.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- A1: treasurer_concentration -> entity_kind='treasurer'
-- ----------------------------------------------------------------------------
-- Continuous signal (raw_value = n_committees managed). Single national
-- bucket: 'kind=treasurer'. CUME_DIST percentile within the bucket.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_treasurer_concentration_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'treasurer_concentration';

    WITH src AS (
        SELECT
            cycle,
            tres_nm_canonical,
            n_committees::NUMERIC AS raw_value
        FROM derived.fec_treasurer_concentration
        WHERE cycle = p_cycle
    ),
    ranked AS (
        SELECT
            cycle,
            tres_nm_canonical,
            raw_value,
            CUME_DIST() OVER (ORDER BY raw_value) AS peer_percentile
        FROM src
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        cycle,
        'treasurer',
        tres_nm_canonical,
        'treasurer_concentration',
        raw_value,
        3::SMALLINT,
        'kind=treasurer',
        peer_percentile,
        '/fec/metrics/treasurer_concentration?cycle=' || cycle
    FROM ranked;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------------
-- A2: candidate_no_pcc -> entity_kind='candidate'
-- ----------------------------------------------------------------------------
-- Binary signal. peer_bucket = 'office=<>|state=<>'.
-- peer_percentile = 1 - (n_flagged_in_bucket / n_bucket_population)
-- where n_bucket_population is the count of candidates in raw.fec_candidate
-- for that (cycle, office, state) -- NOT the count of flagged candidates.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_candidate_no_pcc_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'candidate_no_pcc';

    WITH bucket_pop AS (
        SELECT
            cycle,
            cand_office,
            cand_office_st,
            COUNT(*) AS n_in_bucket
        FROM raw.fec_candidate
        WHERE cycle         = p_cycle
          AND cand_office   IS NOT NULL AND cand_office   <> ''
          AND cand_office_st IS NOT NULL AND cand_office_st <> ''
        GROUP BY cycle, cand_office, cand_office_st
    ),
    flagged AS (
        SELECT
            c.cycle,
            c.cand_id,
            c.cand_office,
            c.cand_office_st
        FROM raw.fec_candidate c
        WHERE c.cycle = p_cycle
          AND (c.cand_pcc IS NULL OR c.cand_pcc = '')
          AND c.cand_office    IS NOT NULL AND c.cand_office    <> ''
          AND c.cand_office_st IS NOT NULL AND c.cand_office_st <> ''
    ),
    n_flagged AS (
        SELECT cycle, cand_office, cand_office_st, COUNT(*) AS n_flagged
        FROM flagged
        GROUP BY cycle, cand_office, cand_office_st
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        f.cycle,
        'candidate',
        f.cand_id,
        'candidate_no_pcc',
        1::NUMERIC,
        1::SMALLINT,
        'office=' || f.cand_office || '|state=' || f.cand_office_st,
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (nf.n_flagged::NUMERIC / NULLIF(bp.n_in_bucket, 0)::NUMERIC)
        ),
        '/fec/metrics/candidate_no_pcc?cycle=' || f.cycle
    FROM flagged f
    JOIN bucket_pop bp
      ON bp.cycle           = f.cycle
     AND bp.cand_office     = f.cand_office
     AND bp.cand_office_st  = f.cand_office_st
    JOIN n_flagged nf
      ON nf.cycle            = f.cycle
     AND nf.cand_office      = f.cand_office
     AND nf.cand_office_st   = f.cand_office_st;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------------
-- A3: candidate_broken_pcc -> entity_kind='candidate'
-- ----------------------------------------------------------------------------
-- Binary signal, same shape as A2.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_candidate_broken_pcc_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'candidate_broken_pcc';

    WITH bucket_pop AS (
        SELECT
            cycle, cand_office, cand_office_st,
            COUNT(*) AS n_in_bucket
        FROM raw.fec_candidate
        WHERE cycle = p_cycle
          AND cand_office    IS NOT NULL AND cand_office    <> ''
          AND cand_office_st IS NOT NULL AND cand_office_st <> ''
        GROUP BY cycle, cand_office, cand_office_st
    ),
    flagged AS (
        SELECT
            v.cycle,
            v.cand_id,
            c.cand_office,
            c.cand_office_st
        FROM derived.fec_candidate_broken_pcc v
        JOIN raw.fec_candidate c
          ON c.cycle   = v.cycle AND c.cand_id = v.cand_id
        WHERE v.cycle = p_cycle
          AND c.cand_office    IS NOT NULL AND c.cand_office    <> ''
          AND c.cand_office_st IS NOT NULL AND c.cand_office_st <> ''
    ),
    n_flagged AS (
        SELECT cycle, cand_office, cand_office_st, COUNT(*) AS n_flagged
        FROM flagged
        GROUP BY cycle, cand_office, cand_office_st
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        f.cycle,
        'candidate',
        f.cand_id,
        'candidate_broken_pcc',
        1::NUMERIC,
        2::SMALLINT,
        'office=' || f.cand_office || '|state=' || f.cand_office_st,
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (nf.n_flagged::NUMERIC / NULLIF(bp.n_in_bucket, 0)::NUMERIC)
        ),
        '/fec/metrics/candidate_broken_pcc?cycle=' || f.cycle
    FROM flagged f
    JOIN bucket_pop bp
      ON bp.cycle = f.cycle AND bp.cand_office = f.cand_office
     AND bp.cand_office_st = f.cand_office_st
    JOIN n_flagged nf
      ON nf.cycle = f.cycle AND nf.cand_office = f.cand_office
     AND nf.cand_office_st = f.cand_office_st;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------------
-- A4: candidate_multiple_pccs -> entity_kind='candidate'
-- ----------------------------------------------------------------------------
-- Continuous signal (raw_value = n_pccs >= 2). Same bucket shape as A2/A3.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_candidate_multiple_pccs_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'candidate_multiple_pccs';

    WITH src AS (
        SELECT
            v.cycle,
            v.cand_id,
            v.n_pccs::NUMERIC AS raw_value,
            v.cand_office,
            v.cand_office_st
        FROM derived.fec_candidate_multiple_pccs v
        WHERE v.cycle = p_cycle
          AND v.cand_office    IS NOT NULL AND v.cand_office    <> ''
          AND v.cand_office_st IS NOT NULL AND v.cand_office_st <> ''
    ),
    ranked AS (
        SELECT
            cycle, cand_id, raw_value,
            cand_office, cand_office_st,
            CUME_DIST() OVER (
                PARTITION BY cycle, cand_office, cand_office_st
                ORDER BY raw_value
            ) AS peer_percentile
        FROM src
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        cycle,
        'candidate',
        cand_id,
        'candidate_multiple_pccs',
        raw_value,
        2::SMALLINT,
        'office=' || cand_office || '|state=' || cand_office_st,
        peer_percentile,
        '/fec/metrics/candidate_multiple_pccs?cycle=' || cycle
    FROM ranked;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------------
-- A5: committee_address_clusters -> entity_kind='address'
-- ----------------------------------------------------------------------------
-- Continuous signal (raw_value = n_committees per canonical address).
-- entity_id = canonical address text concatenated with state for uniqueness.
-- peer_bucket = 'state=<>'.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_committee_address_clusters_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'committee_address_clusters';

    WITH src AS (
        SELECT
            cycle,
            -- entity_id deterministically combines address + state; non-empty
            -- by virtue of the source view's WHERE cmte_st1 <> '' filter.
            address_canonical || '|' || COALESCE(state, '') AS entity_id,
            n_committees::NUMERIC                            AS raw_value,
            COALESCE(state, '')                              AS state
        FROM derived.fec_committee_address_clusters
        WHERE cycle = p_cycle
    ),
    ranked AS (
        SELECT
            cycle, entity_id, raw_value, state,
            CUME_DIST() OVER (
                PARTITION BY cycle, state
                ORDER BY raw_value
            ) AS peer_percentile
        FROM src
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        cycle,
        'address',
        entity_id,
        'committee_address_clusters',
        raw_value,
        4::SMALLINT,
        'state=' || state,
        peer_percentile,
        '/fec/metrics/committee_address_clusters?cycle=' || cycle
    FROM ranked;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------------
-- A6: committee_name_collisions -> entity_kind='committee' (FANNED OUT)
-- ----------------------------------------------------------------------------
-- The source view groups by canonical name and emits one row per group with
-- an array of cmte_ids. We fan out to one observation per cmte_id in the
-- group: every committee in a name-collision is at the same percentile
-- (it's the group raw_value, n_committee_ids, that ranks).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_committee_name_collisions_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'committee_name_collisions';

    WITH groups AS (
        SELECT
            v.cycle,
            v.cmte_nm_canonical,
            v.n_committee_ids::NUMERIC AS raw_value,
            v.committee_ids,
            -- Per-committee state derived from raw; same canonical name
            -- can span states, so a committee's bucket is its own cmte_st,
            -- not a per-group field.
            UNNEST(v.committee_ids)    AS cmte_id
        FROM derived.fec_committee_name_collisions v
        WHERE v.cycle = p_cycle
    ),
    fanned AS (
        SELECT
            g.cycle,
            g.cmte_id,
            g.raw_value,
            COALESCE(c.cmte_st, '') AS state
        FROM groups g
        LEFT JOIN raw.fec_committee c
          ON c.cycle = g.cycle AND c.cmte_id = g.cmte_id
    ),
    ranked AS (
        SELECT
            cycle, cmte_id, raw_value, state,
            CUME_DIST() OVER (
                PARTITION BY cycle, state
                ORDER BY raw_value
            ) AS peer_percentile
        FROM fanned
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        cycle,
        'committee',
        cmte_id,
        'committee_name_collisions',
        raw_value,
        3::SMALLINT,
        'state=' || state,
        peer_percentile,
        '/fec/metrics/committee_name_collisions?cycle=' || cycle
    FROM ranked;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------------
-- A7: candidate_namesakes -> entity_kind='candidate' (FANNED OUT)
-- ----------------------------------------------------------------------------
-- Source view groups by canonical name + state + office; fan out to one
-- observation per cand_id in the group.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_candidate_namesakes_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'candidate_namesakes';

    WITH fanned AS (
        SELECT
            v.cycle,
            UNNEST(v.candidate_ids)  AS cand_id,
            v.n_cand_ids::NUMERIC    AS raw_value,
            v.cand_office_st,
            v.cand_office
        FROM derived.fec_candidate_namesakes v
        WHERE v.cycle = p_cycle
          AND v.cand_office    IS NOT NULL AND v.cand_office    <> ''
          AND v.cand_office_st IS NOT NULL AND v.cand_office_st <> ''
    ),
    ranked AS (
        SELECT
            cycle, cand_id, raw_value, cand_office, cand_office_st,
            CUME_DIST() OVER (
                PARTITION BY cycle, cand_office, cand_office_st
                ORDER BY raw_value
            ) AS peer_percentile
        FROM fanned
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        cycle,
        'candidate',
        cand_id,
        'candidate_namesakes',
        raw_value,
        3::SMALLINT,
        'office=' || cand_office || '|state=' || cand_office_st,
        peer_percentile,
        '/fec/metrics/candidate_namesakes?cycle=' || cycle
    FROM ranked;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------------
-- A8: treasurer_is_candidate -> entity_kind='committee'
-- ----------------------------------------------------------------------------
-- Binary signal. peer_bucket uses the linked candidate's (office, state)
-- since it's the candidate-side reference group that matters. n_flagged is
-- the count of cmte rows where the flag fires within that bucket.
-- bucket_pop is the count of cmtes linked to candidates in that bucket
-- (the natural population of "could-have-been-flagged" entities).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_treasurer_is_candidate_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'treasurer_is_candidate';

    WITH flagged AS (
        SELECT
            v.cycle,
            v.cmte_id,
            v.cand_office,
            v.cand_office_st
        FROM derived.fec_treasurer_is_candidate v
        WHERE v.cycle = p_cycle
          AND v.cand_office    IS NOT NULL AND v.cand_office    <> ''
          AND v.cand_office_st IS NOT NULL AND v.cand_office_st <> ''
    ),
    bucket_pop AS (
        SELECT
            c.cycle,
            cd.cand_office,
            cd.cand_office_st,
            COUNT(*) AS n_in_bucket
        FROM raw.fec_committee c
        JOIN raw.fec_candidate cd
          ON cd.cycle = c.cycle AND cd.cand_id = c.cand_id
        WHERE c.cycle = p_cycle
          AND cd.cand_office    IS NOT NULL AND cd.cand_office    <> ''
          AND cd.cand_office_st IS NOT NULL AND cd.cand_office_st <> ''
        GROUP BY c.cycle, cd.cand_office, cd.cand_office_st
    ),
    n_flagged AS (
        SELECT cycle, cand_office, cand_office_st, COUNT(*) AS n_flagged
        FROM flagged
        GROUP BY cycle, cand_office, cand_office_st
    )
    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    SELECT
        f.cycle,
        'committee',
        f.cmte_id,
        'treasurer_is_candidate',
        1::NUMERIC,
        1::SMALLINT,
        'office=' || f.cand_office || '|state=' || f.cand_office_st,
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (nf.n_flagged::NUMERIC / NULLIF(bp.n_in_bucket, 0)::NUMERIC)
        ),
        '/fec/metrics/treasurer_is_candidate?cycle=' || f.cycle
    FROM flagged f
    JOIN bucket_pop bp
      ON bp.cycle = f.cycle AND bp.cand_office = f.cand_office
     AND bp.cand_office_st = f.cand_office_st
    JOIN n_flagged nf
      ON nf.cycle = f.cycle AND nf.cand_office = f.cand_office
     AND nf.cand_office_st = f.cand_office_st;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------------
-- DISPATCHER
-- ----------------------------------------------------------------------------
-- Calls all eight per-signal refreshers in a single transaction (the caller
-- is responsible for the BEGIN/COMMIT; the function itself runs in the
-- caller's transaction context). Returns total rows inserted across all
-- signals so the operator can sanity-check.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_all_fraud_signal_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_total INT := 0;
    n_each  INT;
BEGIN
    SELECT derived.refresh_treasurer_concentration_observations(p_cycle)    INTO n_each;
    n_total := n_total + n_each;
    SELECT derived.refresh_candidate_no_pcc_observations(p_cycle)           INTO n_each;
    n_total := n_total + n_each;
    SELECT derived.refresh_candidate_broken_pcc_observations(p_cycle)       INTO n_each;
    n_total := n_total + n_each;
    SELECT derived.refresh_candidate_multiple_pccs_observations(p_cycle)    INTO n_each;
    n_total := n_total + n_each;
    SELECT derived.refresh_committee_address_clusters_observations(p_cycle) INTO n_each;
    n_total := n_total + n_each;
    SELECT derived.refresh_committee_name_collisions_observations(p_cycle)  INTO n_each;
    n_total := n_total + n_each;
    SELECT derived.refresh_candidate_namesakes_observations(p_cycle)        INTO n_each;
    n_total := n_total + n_each;
    SELECT derived.refresh_treasurer_is_candidate_observations(p_cycle)     INTO n_each;
    n_total := n_total + n_each;
    RETURN n_total;
END;
$$ LANGUAGE plpgsql;


COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'TIER 4 v3 step 2 dispatcher: re-emits every v2.A structural signal '
'(migration 040) as rows in derived.fraud_signal_observation for the '
'given cycle. Idempotent: each per-signal refresher does DELETE + INSERT '
'within its own slice. Designed to be called by a Dagster asset that '
'depends on raw.fec_candidate + raw.fec_committee.';
