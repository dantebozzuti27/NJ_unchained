-- ============================================================================
-- Migration: 064_fraud_excluded_via_sam_uei
--
-- TIER 4 v3 / FRAUD-F2 (signal layer): cross-source signal
-- `entity_excluded_via_sam_uei`. Joins USAspending NJ-pop active
-- recipients against SAM.gov-excluded UEIs on a DETERMINISTIC
-- 12-character UEI = UEI key.
--
-- WHY THIS SIGNAL EXISTS (alongside entity_funded_and_excluded / 058)
-- ------------------------------------------------------------------
-- Signal 058 (entity_funded_and_excluded) joins USAspending against
-- HHS-OIG LEIE on a CANONICALIZED individual-name key
-- ("LAST|FIRST"). That join is high-precision but limited:
--    * LEIE covers only HEALTHCARE exclusions; SAM aggregates
--      exclusions from EVERY excluding agency (DOJ, GSA, NIH, NSF,
--      DOE, OFAC reciprocity, ...) and therefore catches a much
--      broader population.
--    * 058 only matches person-shaped recipients (sole proprietors
--      and individual consultants). Most federal contractors are
--      corporations -- 058 cannot reach them.
--    * Name canonicalization, even strict, has a non-zero false-
--      positive rate.
--
-- This signal (064) addresses all three:
--    * BROADER COVERAGE -- SAM is the union of all federal
--      exclusion authorities. A firm excluded under FAR 9.405 by
--      GSA appears here whether or not it appears in LEIE.
--    * CORPORATE COVERAGE -- the join key is UEI, the federal
--      procurement primary key. Every active federal contractor
--      since FY2022 has a UEI; SAM-excluded firms have UEI on
--      every active exclusion.
--    * ZERO FALSE-POSITIVE RISK -- UEI is unique by SAM design.
--      A UEI = UEI match is, by definition, the same legal
--      entity. The only failure mode is a SAM-side data-entry
--      bug, which is rare and surfaces as an asset check.
--
-- LEGAL WEIGHT
-- ------------
-- A firm or individual on SAM's exclusion list is barred from
-- receiving federal contracts under Federal Acquisition
-- Regulation 9.405. A UEI = UEI match where the award is active
-- and the exclusion is active means EXACTLY that the federal
-- procurement system already knows this entity is excluded YET
-- they received a contract. There is no benign interpretation:
-- at minimum the contracting officer failed to check SAM (a
-- compliance violation), at worst the contract is laundering
-- federal money to an excluded entity. Severity = 5 (CRITICAL).
--
-- SIGNAL_FAMILY
-- -------------
-- Introduces a new family `sam_bearing` (parallel to existing
-- `leie_bearing`). The family CHECK on derived.fraud_signal_config
-- is extended from 4 -> 5 values. The diversity bonus in
-- derived.fraud_risk_score now rewards entities that fire on
-- BOTH leie_bearing AND sam_bearing signals -- which is the
-- strongest possible cross-source corroboration (a person both
-- excluded from federal healthcare AND from federal contracts,
-- AND awarded a federal contract anyway).
--
-- AGE DECAY
-- ---------
-- Reuses derived.f_leie_age_decay (migration 062) -- the
-- function is exclusion-list-agnostic; the LEIE prefix in its
-- name is historical. A 12-year-old SAM exclusion has the same
-- decay weight as a 12-year-old LEIE exclusion. The decay is
-- applied per-AWARD (not per-UEI) because a single excluded UEI
-- can have awards spanning multiple years; each award's risk is
-- tied to the freshness of the exclusion at the time of the
-- award AND the freshness now. We use NOW as the decay anchor
-- (matching LEIE 060's per-contribution decay) so a stale
-- exclusion paired with a recent award is correctly weighted.
--
-- THRESHOLD
-- ---------
-- min_actionable_threshold = $0. Unlike donor_on_leie ($200
-- floor for spurious-noise) or entity_funded_and_excluded
-- ($10K), a UEI-deterministic match has no spurious-noise
-- interpretation: ANY award amount to a SAM-excluded UEI is a
-- procurement violation. Floor is $0; every match goes to the
-- queue.
--
-- STEADY-STATE EXPECTATION
-- ------------------------
-- This signal SHOULD BE EMPTY in a well-functioning federal
-- procurement system. Non-zero rows mean either a contracting
-- officer skipped SAM (a violation) or SAM is publishing
-- exclusions that USAspending hasn't propagated yet (a SAM-vs-
-- USAspending data-lag artifact, surfaceable via active_date
-- vs award period_start comparison).
--
-- WHAT'S NOT IN THIS MIGRATION (deliberately)
-- -------------------------------------------
-- 1. Individual-name SAM matching (parallel to 058's LEIE-name
--    matching). SAM individual-canonical view exists (063) but
--    has many fewer Individual rows than LEIE (most SAM
--    exclusions are firms). A separate migration ships when
--    we need analyst coverage there.
-- 2. Cross-source entity resolution merging UEI-keyed and
--    LAST|FIRST-keyed contractor entities into one. Future
--    work: derived.fec_contractor_resolution mapping both ID
--    spaces to a stable contractor_id.
-- 3. Per-agency severity weights. Currently severity=5 across
--    every excluding_agency. A future iteration could weight
--    DOJ-debarments higher than NSF-misconduct exclusions; left
--    as future analyst-feedback-driven calibration.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. Extend signal_family CHECK to include 'sam_bearing'
-- ----------------------------------------------------------------------------
-- Recreate the CHECK with 5 families. The existing 4 stay valid; the
-- new 'sam_bearing' is permitted for the new signal row inserted
-- below.
-- ----------------------------------------------------------------------------
ALTER TABLE derived.fraud_signal_config
DROP CONSTRAINT IF EXISTS fraud_signal_config_signal_family_check;

ALTER TABLE derived.fraud_signal_config
ADD  CONSTRAINT fraud_signal_config_signal_family_check
CHECK (signal_family IN (
    'leie_bearing',
    'sam_bearing',
    'workforce',
    'address',
    'structural'
));

COMMENT ON CONSTRAINT fraud_signal_config_signal_family_check
ON derived.fraud_signal_config IS
    'Whitelist of signal_family values. Five families as of '
    'migration 064: leie_bearing (HHS-OIG individual healthcare '
    'exclusion list), sam_bearing (SAM.gov federal-contracting '
    'exclusion list -- broader than LEIE), workforce (federal-'
    'contractor employee donations), address (residential / '
    'committee address clustering), structural (intra-FEC schema '
    'anomalies). The diversity bonus in derived.fraud_risk_score '
    'rewards entities firing on signals across distinct families.';


-- ----------------------------------------------------------------------------
-- 2. Seed the new signal config row
-- ----------------------------------------------------------------------------
-- Threshold=$0: ANY UEI-determinate match against a federal exclusion
-- list is investigation-worthy. No noise floor.
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config (
    signal_id,
    signal_family,
    min_actionable_threshold,
    comment
) VALUES (
    'entity_excluded_via_sam_uei',
    'sam_bearing',
    0,
    'Cross-source UEI-deterministic match: USAspending NJ-pop '
    'active recipient (recipient_uei) appears on SAM.gov active '
    'exclusion list (sam_uei). Threshold=$0 because UEI is '
    'unique by SAM design and any matched dollar is a FAR 9.405 '
    'violation; there is no spurious-noise interpretation at the '
    'L1->L2 boundary.'
)
ON CONFLICT (signal_id) DO UPDATE
    SET signal_family            = EXCLUDED.signal_family,
        min_actionable_threshold = EXCLUDED.min_actionable_threshold,
        comment                  = EXCLUDED.comment;


-- ----------------------------------------------------------------------------
-- 3. REFRESHER: derived.refresh_signal_entity_excluded_via_sam_uei(cycle)
-- ----------------------------------------------------------------------------
-- Idempotent on its (cycle, signal_id='entity_excluded_via_sam_uei')
-- slice. Mirrors the 058 DELETE-then-INSERT pattern.
--
-- ENTITY MAPPING
-- --------------
-- entity_kind = 'contractor', entity_id = recipient_uei (12-char
-- alphanumeric). Note: 058 also writes entity_kind='contractor'
-- but uses entity_id='LAST|FIRST' (canonical individual name).
-- Same kind, different id space -- they don't collide on the
-- (cycle, entity_kind, entity_id, signal_id) PK because the
-- signal_ids differ. A future entity-resolution layer will merge
-- them when ground truth permits.
--
-- AGE DECAY
-- ---------
-- raw_value = SUM(award_amount * f_leie_age_decay(active_date))
-- per UEI. Per-AWARD decay: each award's contribution is weighted
-- by how stale the exclusion is at refresh time. A UEI excluded
-- in 2010 with a 2024 award contributes (2024 award) * exp(-14/10)
-- ~= award * 0.247. A UEI excluded in 2024 contributes the full
-- award amount.
--
-- BUCKET / PERCENTILE
-- -------------------
-- peer_bucket     = 'kind=contractor_uei'
-- peer_percentile = 1 - (n_flagged / n_in_bucket)
-- where n_in_bucket = COUNT(DISTINCT recipient_uei) in the active
-- USAspending window. 1-of-N% match -> high percentile,
-- as expected for a vanishingly-rare flag.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_signal_entity_excluded_via_sam_uei(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_inserted INT;
BEGIN
    DELETE FROM derived.fraud_signal_observation
     WHERE cycle     = p_cycle
       AND signal_id = 'entity_excluded_via_sam_uei';

    INSERT INTO derived.fraud_signal_observation (
        cycle, entity_kind, entity_id, signal_id,
        raw_value, severity, peer_bucket, peer_percentile, evidence_url
    )
    WITH us_uei_recipients AS (
        -- Active USAspending recipients with a non-NULL UEI. The
        -- UEI is upper-cased in the raw schema by CHECK.
        SELECT
            recipient_uei,
            generated_unique_award_id,
            award_amount
        FROM derived.v_usaspending_award_active
        WHERE recipient_uei IS NOT NULL
    ),
    pop AS (
        -- Bucket population: distinct UEIs in the active window.
        -- Denominator for the rate-based percentile.
        SELECT COUNT(DISTINCT recipient_uei) AS n_in_bucket
        FROM us_uei_recipients
    ),
    sam_uei_freshest AS (
        -- Collapse multiple SAM exclusions for the same UEI (rare:
        -- a firm re-excluded under a different agency / program)
        -- to a single representative row -- the freshest exclusion
        -- by active_date. Pre-collapse keeps the join-x-aggregate
        -- linear and removes any award double-counting risk.
        SELECT DISTINCT ON (sam_uei)
            sam_uei,
            sam_record_hash,
            sam_active_date
        FROM derived.v_sam_exclusion_by_uei
        ORDER BY sam_uei, sam_active_date DESC NULLS LAST
    ),
    matches AS (
        -- One row per active recipient AWARD whose recipient UEI
        -- is on the SAM exclusion list. One UEI with N awards =
        -- N rows here. Per-row decay applied below.
        SELECT
            r.recipient_uei,
            s.sam_record_hash,
            s.sam_active_date,
            r.award_amount,
            r.generated_unique_award_id
        FROM us_uei_recipients r
        JOIN sam_uei_freshest s
          ON s.sam_uei = r.recipient_uei
    ),
    aggregated AS (
        SELECT
            recipient_uei,
            MIN(sam_record_hash)                       AS sam_record_hash,
            -- Per-award decay summed -- equivalent to applying
            -- the (UEI-level) freshest-exclusion decay weight to
            -- SUM(award_amount), since all rows for a given UEI
            -- share active_date by virtue of the freshest pre-
            -- collapse. Written this way for clarity / future-
            -- proofing if per-award exclusion-date weighting is
            -- introduced.
            COALESCE(
                SUM(
                    GREATEST(award_amount, 0)::NUMERIC
                    * derived.f_leie_age_decay(sam_active_date)
                ),
                0
            )::NUMERIC                                 AS sum_decayed_amt
        FROM matches
        GROUP BY recipient_uei
    ),
    flag_count AS (
        SELECT COUNT(*) AS n_flagged FROM aggregated
    )
    SELECT
        p_cycle                                        AS cycle,
        'contractor'                                   AS entity_kind,
        a.recipient_uei                                AS entity_id,
        'entity_excluded_via_sam_uei'                  AS signal_id,
        a.sum_decayed_amt                              AS raw_value,
        5::SMALLINT                                    AS severity,
        'kind=contractor_uei'                          AS peer_bucket,
        GREATEST(
            0::NUMERIC,
            1::NUMERIC - (
                fc.n_flagged::NUMERIC
                / NULLIF(p.n_in_bucket, 0)::NUMERIC
            )
        )                                              AS peer_percentile,
        '/fec/risk/entities/contractor/'
            || a.recipient_uei
            || '?signal=entity_excluded_via_sam_uei'
            || '&cycle=' || p_cycle
            || '&sam='   || a.sam_record_hash          AS evidence_url
    FROM aggregated a
    CROSS JOIN pop p
    CROSS JOIN flag_count fc;

    GET DIAGNOSTICS n_inserted = ROW_COUNT;
    RETURN n_inserted;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION derived.refresh_signal_entity_excluded_via_sam_uei(CHAR(4)) IS
    'Refresh entity_excluded_via_sam_uei for one analyst cycle. '
    'Idempotent on its (cycle, signal_id) slice. One row per UEI '
    'that appears as BOTH an active USAspending recipient AND an '
    'active SAM.gov exclusion. raw_value = SUM(award_amount * '
    'f_leie_age_decay(active_date)) across the UEI''s contracts. '
    'severity=5 (CRITICAL); rate-based peer_percentile within '
    '''kind=contractor_uei'' bucket. Steady-state expected count '
    'is ZERO in a well-functioning federal procurement system; '
    'non-empty rows are always investigation-worthy.';
