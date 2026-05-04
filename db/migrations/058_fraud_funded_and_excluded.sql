-- ============================================================================
-- Migration: 058_fraud_funded_and_excluded
--
-- TIER 4 v3 / FRAUD-F1 + F5 intersection: cross-source signal
-- `entity_funded_and_excluded`. Joins USAspending NJ-pop active
-- recipients against HHS-OIG LEIE individual exclusions on the
-- canonical "LAST|FIRST" key.
--
-- WHY THIS IS THE HIGHEST-PRIORITY SIGNAL ON THE PLATFORM
-- -------------------------------------------------------
-- The federal exclusion list (LEIE) bars an individual from
-- participating in federal health-care programs. A federal contract
-- is, by definition, federal participation. A name on both lists is
-- a procurement-fraud red alert: at minimum a contracting officer
-- failed to check the exclusion list (a violation of the FAR
-- exclusion requirements), and at worst the contractor is laundering
-- federal money through a shell to bypass exclusion. Severity is
-- fixed at 5 (CRITICAL); every match warrants immediate analyst
-- review.
--
-- Steady-state expectation: this signal SHOULD BE EMPTY in a
-- well-functioning federal procurement system. The asset check on
-- this signal does NOT fire on zero matches when both raw tables
-- are non-empty (unlike the FEC-x-LEIE signal where zero matches
-- indicates a canonicalizer regression). Zero is the EXPECTED
-- result; the platform's purpose is to detect deviations.
--
-- WHAT'S IN SCOPE FOR V1
-- ----------------------
-- Individual-vs-individual matching only:
--    * USAspending recipient_name parses as "LAST, FIRST" via
--      derived.v_usaspending_award_active.recipient_canonical_individual
--      (NULL for corporate recipients; that's most of them).
--    * LEIE individual side via
--      derived.v_leie_individual_canonical.canonical_key.
-- Most federal contractors are corporations -- this signal will fire
-- only on the (rarer) sole-proprietor contracts and individual
-- consulting awards. That's correct: it's the population where
-- person-name matching is the right join key.
--
-- WHAT'S OUT OF SCOPE FOR V1
-- --------------------------
-- 1. Business-side matching (LEIE busname x USAspending corporate
--    recipient_name). LEIE businesses are healthcare-org-shaped
--    ("ACME HOME HEALTH LLC"); USAspending NJ recipients are
--    defense/services-corp-shaped ("LOCKHEED MARTIN CORP"). A
--    business-canonical join across them has high false-positive
--    risk (any "John Doe Consulting LLC" appearing on both sides
--    might or might not be the same entity) and lower expected hit
--    rate. The natural fix is canonicalizing on UEI when SAM.gov
--    is ingested (FRAUD-F2) -- UEI is the federal canonical entity
--    identifier and binds a contractor to its corporate identity
--    deterministically.
-- 2. Address / DOB filtering. As with entity_on_leie, we start
--    strict on canonicalization, loose on demographics. A future
--    iteration tightens once L5 labels exist.
-- 3. Authorized-representative matching. LEIE doesn't expose a
--    "principal of business" field; USAspending doesn't expose
--    contractor officers. Cross-checking corporate principals
--    against LEIE requires SAM.gov SAM.gov POC fields, deferred
--    to FRAUD-F2.
-- 4. Multi-source authority list combining (LEIE + SAM.gov + GSA
--    debarments). One source at a time; SAM.gov is the next slice.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- Schema change: extend the entity_kind whitelist to include 'contractor'
-- ----------------------------------------------------------------------------
-- The existing five kinds ('committee', 'candidate', 'treasurer',
-- 'address', 'donor_cluster') were FEC-domain entities. With cross-
-- source signals against USAspending we need a sixth kind:
-- 'contractor' is a federal-award recipient (person, in v1).
--
-- Adding it requires DROP CONSTRAINT + ADD CONSTRAINT (Postgres
-- doesn't have a single-statement "modify CHECK"). We name the new
-- constraint explicitly so future migrations can target it
-- deterministically.
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
    'contractor'
));

COMMENT ON CONSTRAINT fraud_signal_observation_entity_kind_check
ON derived.fraud_signal_observation IS
    'Whitelist of entity_kind values. Six kinds as of migration 058: '
    'the five FEC-domain kinds (committee, candidate, treasurer, '
    'address, donor_cluster) plus contractor (federal-award recipient, '
    'currently person-shaped only -- corporate-side matching is '
    'deferred to FRAUD-F2 SAM.gov UEI canonicalization).';


-- ----------------------------------------------------------------------------
-- REFRESHER: derived.refresh_signal_entity_funded_and_excluded(cycle)
-- ----------------------------------------------------------------------------
-- Idempotent on its (cycle, signal_id='entity_funded_and_excluded')
-- slice. DELETE-then-INSERT pattern mirroring the entity_on_leie and
-- donor-side signal refreshers.
--
-- BUCKET / PERCENTILE
-- -------------------
-- Mirrors entity_on_leie's rate-based percentile pattern:
--    peer_bucket     = 'kind=contractor'
--    peer_percentile = 1 - (n_flagged / n_in_bucket)
-- Bucket population = active USAspending recipients with a parseable
-- canonical individual key (i.e. "LAST, FIRST"-shaped recipient
-- names). 1-of-1000 match -> percentile 0.999 (vanishingly rare,
-- maximally damning).
--
-- entity_id = canonical "LAST|FIRST" key. Multiple awards to the
-- same matched person collapse to ONE signal row (DISTINCT ON), with
-- raw_value = SUM(award_amount) across all their awards in the
-- active window.
--
-- CYCLE SEMANTICS
-- ---------------
-- USAspending data is not FEC-cycle-bound. The cycle parameter is
-- the analyst-session label: writing this signal under cycle='2024'
-- means "show this row in the 2024 fraud queue." This mirrors how
-- entity_on_leie binds treasurer matches to a cycle without the
-- treasurer's exclusion having any FEC-cycle relationship.
-- ----------------------------------------------------------------------------
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
    WITH     us_individual_recipients AS (
        -- Active USAspending recipients whose name parses as a
        -- person ("LAST, FIRST" shape). The canonicalizer (defined
        -- in migration 054 / 055) returns NULL for corporate-shaped
        -- names, so this CTE is automatically person-only.
        SELECT
            recipient_canonical_individual          AS canonical_individual,
            generated_unique_award_id,
            award_amount
        FROM derived.v_usaspending_award_active
        WHERE recipient_canonical_individual IS NOT NULL
    ),
    pop AS (
        -- Bucket population for the rate-based percentile. The
        -- denominator is "distinct individual recipients in the
        -- active window," not "total awards" -- a single person
        -- with 100 awards counts once.
        SELECT COUNT(DISTINCT canonical_individual) AS n_in_bucket
        FROM us_individual_recipients
    ),
    leie_canonical_freshest AS (
        -- Collapse multiple LEIE records for the same canonical key
        -- (rare: a person re-excluded under a different authority)
        -- to a single representative row -- the freshest exclusion.
        -- This pre-collapse keeps the join-x-aggregate path
        -- linear and removes any award double-counting risk.
        SELECT DISTINCT ON (canonical_key)
            canonical_key,
            leie_record_hash,
            leie_excldate
        FROM derived.v_leie_individual_canonical
        ORDER BY canonical_key, leie_excldate DESC NULLS LAST
    ),
    matches AS (
        -- One row per active recipient AWARD whose recipient is on
        -- the LEIE individual list. One person with N awards = N
        -- rows here. The aggregate below collapses to one row per
        -- person and SUMs across their awards.
        SELECT
            r.canonical_individual,
            l.leie_record_hash,
            r.award_amount,
            r.generated_unique_award_id
        FROM us_individual_recipients r
        JOIN leie_canonical_freshest l
          ON l.canonical_key = r.canonical_individual
    ),
    aggregated AS (
        SELECT
            canonical_individual,
            MIN(leie_record_hash)                      AS leie_record_hash,
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
        a.sum_positive_amt                             AS raw_value,
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
            -- Path-safe escape for the canonical-key separator and
            -- any (theoretical) slash. The canonical key is
            -- LAST|FIRST; '|' is path-legal per RFC 3986 but ugly
            -- in URLs, so we collapse it to '_' for analyst clarity.
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

COMMENT ON FUNCTION derived.refresh_signal_entity_funded_and_excluded(CHAR) IS
    'Refresh entity_funded_and_excluded for one analyst cycle. '
    'Idempotent on its (cycle, signal_id) slice. One row per matched '
    'canonical "LAST|FIRST" key that appears as BOTH an active '
    'USAspending recipient AND an active LEIE individual. '
    'raw_value = SUM(award_amount) across the person''s contracts. '
    'severity=5 (CRITICAL); rate-based peer_percentile within '
    '''kind=contractor'' bucket. Steady-state expected count is ZERO '
    'in a well-functioning procurement system; non-empty rows are '
    'always investigation-worthy.';
