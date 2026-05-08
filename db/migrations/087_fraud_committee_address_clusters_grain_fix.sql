-- ============================================================================
-- Migration: 087_fraud_committee_address_clusters_grain_fix
--
-- VISION_2026 Pillar 2 (civic integrity) -- Phase F-1 substrate fix.
--
-- Surfaces a substrate bug that materialized only after the FEC bulk loader
-- (cycle 2024 cn + cm) populated raw.fec_committee with real committee
-- registrations. The bug is a grain mismatch between two objects that both
-- claim to identify "the same physical address":
--
--   * derived.fec_committee_address_clusters (migration 040) groups by the
--     5-tuple (cycle, address_canonical, city_canonical, state, zip_canonical),
--     where zip_canonical preserves whatever digits FEC published -- which
--     means a single physical address that some committees filed with ZIP
--     "33606" and others with ZIP+4 "336062647" appears as TWO distinct
--     view rows, with two different n_committees counts.
--
--   * derived.refresh_committee_address_clusters_observations (migration 051)
--     keys each fraud observation with entity_id = address_canonical || '|' ||
--     state. That's a 2-tuple. So when the source view emits two rows for
--     the same physical address (because of zip+4 noise), the refresher
--     INSERTs two rows that collide on the PK (cycle, entity_kind, entity_id,
--     signal_id), aborting the entire refresh.
--
-- Concrete observed cases (cycle 2024, real FEC data published 2026-05-08):
--
--     address               state  filings as 5-digit zip   filings as zip+4
--     -------------------   -----  ----------------------   -----------------
--     "PO BOX 97275"        NC     49 committees @ 27624     4 committees @ 276247275
--     "PO BOX 2485"         VA     18 committees @ 22152     3 committees @ 221520485
--     "9856 ARCHER LN"      OH      8 committees @ 43017     3 committees @ 430178914
--     "610 S BOULEVARD"     FL      6 committees @ 33606     4 committees @ 336062647
--
-- A separate case ("421 OFFICE PARK DR" in AL) has the same street name
-- straddling a municipal boundary -- city BIRMINGHAM (n=3) vs MOUNTAIN
-- BROOK (n=23). These ARE different physical addresses (the road is
-- shared, the city is not), so they should remain TWO observations.
-- The refresher's old `address|state` entity_id was lossy on city; the
-- fix below preserves city in the entity_id so that the two halves of
-- "421 OFFICE PARK DR" stay distinct.
--
-- THE FIX (two coordinated changes)
-- ---------------------------------
-- 1. derived.fec_committee_address_clusters: normalize zip in the GROUP BY
--    to LEFT(REGEXP_REPLACE(cmte_zip, '\D', '', 'g'), 5). zip+4 noise now
--    collapses at the source: "33606" and "336062647" both group as
--    zip_canonical = '33606' and the cluster size of n_committees correctly
--    counts the union of committees across both filing styles.
--
--    Why LEFT(.., 5) and not the whole digits string:
--      - FEC's bulk schema treats CMTE_ZIP as TEXT with no shape contract
--        (legacy entries can be 5, 9, 10, 11, or junk).
--      - The first 5 digits are the only portion that uniquely identifies
--        a USPS zip code area. The +4 is street-precise but optional and
--        inconsistently filed.
--      - Truncation is deterministic and never loses identifying signal.
--
--    Why we KEEP city_canonical in the GROUP BY:
--      - "421 OFFICE PARK DR" in BIRMINGHAM AL and the same street name in
--        MOUNTAIN BROOK AL are different physical addresses on a
--        municipal-boundary road. Collapsing them would overstate one
--        observation and understate the other.
--      - This preserves the source view's ability to distinguish them
--        and, paired with change #2 below, propagates the distinction
--        into derived.fraud_signal_observation.
--
-- 2. derived.refresh_committee_address_clusters_observations: change
--    entity_id from `address|state` to `address|city|state|zip5`. The
--    entity_id now matches the source view's GROUP BY grain exactly, so
--    every view row has exactly one corresponding observation row -- no
--    PK collisions are possible.
--
-- IDEMPOTENCY
-- -----------
-- The refresher is DELETE WHERE signal_id='committee_address_clusters'
-- + INSERT, scoped by cycle. Re-running the refresher with the new
-- entity_id format will simply REPLACE all prior observations for this
-- signal at this cycle. No rolling migration of historical observation
-- rows is needed; the next refresh run cleans them.
--
-- VERIFIABLE-DATA INVARIANTS PRESERVED
-- ------------------------------------
-- * Provenance: derived.fraud_signal_observation.evidence_url is unchanged
--   (the route /fec/metrics/committee_address_clusters keys on cycle).
-- * formula_version: 2.1.0-fraud-evidence-substrate-v1 (unchanged --
--   this migration is a substrate-correctness fix, not a model change;
--   the calibration semantics of severity=4 and CUME_DIST percentile
--   are identical, and the only change is which (entity_kind, entity_id)
--   tuples are emitted at all).
-- * Severity: still 4::SMALLINT (matches the precedent in
--   ref.fraud_signal_severity_calibration row for committee_address_clusters
--   seeded in 019_fraud_signal_severity_calibration.sql).
--
-- TESTS
-- -----
-- A regression test (test_phase_f1_address_clusters_grain.py) constructs
-- a synthetic raw.fec_committee where the same address appears once with
-- a 5-digit zip and once with a 9-digit zip, runs the refresher, and
-- asserts:
--   (a) the refresher returns exactly 1 observation row, not 0 (PK
--       collision) and not 2 (duplicate rows);
--   (b) raw_value (= n_committees) equals the union of committees across
--       both filing styles.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. Source view: collapse zip+4 to zip5 at the GROUP BY level
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.fec_committee_address_clusters AS
SELECT
    cycle,
    REGEXP_REPLACE(UPPER(TRIM(cmte_st1)),  '\s+', ' ', 'g')          AS address_canonical,
    REGEXP_REPLACE(UPPER(TRIM(cmte_city)), '\s+', ' ', 'g')          AS city_canonical,
    cmte_st                                                          AS state,
    LEFT(REGEXP_REPLACE(cmte_zip, '\D', '', 'g'), 5)                 AS zip_canonical,
    COUNT(DISTINCT cmte_id)                                          AS n_committees,
    LEAST(COUNT(DISTINCT cmte_id), 1000)                             AS severity_score,
    ARRAY_AGG(DISTINCT cmte_id ORDER BY cmte_id)                     AS committee_ids,
    ARRAY_AGG(DISTINCT cmte_pty_affiliation
              ORDER BY cmte_pty_affiliation)                         AS parties_seen
FROM   raw.fec_committee
WHERE  cmte_st1 IS NOT NULL AND cmte_st1 <> ''
GROUP  BY 1, 2, 3, 4, 5
HAVING COUNT(DISTINCT cmte_id) >= 3
ORDER  BY n_committees DESC;

COMMENT ON VIEW derived.fec_committee_address_clusters IS
'TIER 4 v2.A.5 (rev 087): Street addresses hosting >=3 distinct committees '
'in one cycle. Many "Friends of X" share campaign HQs (legitimate); shell '
'networks concentrate dozens at single PO boxes. Migration 087 normalizes '
'zip_canonical to LEFT(digits, 5) so zip+4 noise (e.g. 33606 vs 336062647) '
'collapses to a single physical-address grouping; city is preserved in the '
'group key so address-on-a-municipal-boundary cases (e.g. shared road '
'across two cities) remain distinct observations.';


-- ----------------------------------------------------------------------------
-- 2. Refresher: extend entity_id to match the view's full grain
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
            -- entity_id MUST match the source view's GROUP BY grain
            -- (address, city, state, zip5) so each view row maps to
            -- exactly one observation. Migration 087 fixes the prior
            -- 2-tuple `address|state` keying that collided when zip+4
            -- noise produced two view rows per physical address.
            address_canonical
                || '|' || COALESCE(city_canonical, '')
                || '|' || COALESCE(state, '')
                || '|' || COALESCE(zip_canonical, '')                AS entity_id,
            n_committees::NUMERIC                                    AS raw_value,
            COALESCE(state, '')                                      AS state
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

COMMENT ON FUNCTION derived.refresh_committee_address_clusters_observations(CHAR(4)) IS
'TIER 4 v3 step 2 (rev 087): Refreshes committee_address_clusters '
'observations from derived.fec_committee_address_clusters. entity_id is '
'address|city|state|zip5 to match the source view''s 4-attribute grain '
'(prior `address|state` collided on zip+4 noise -- see migration 087 '
'header for the full failure mode). Idempotent (DELETE + INSERT, scoped '
'by signal_id and cycle).';


-- ----------------------------------------------------------------------------
-- 3. Migration record (idempotent: ON CONFLICT DO NOTHING)
-- ----------------------------------------------------------------------------
-- The deploy script (scripts/_deploy_087_to_neon.py) handles the actual
-- governance.schema_migrations row insert with sha256 + duration_ms; the
-- migration itself is content-only.
