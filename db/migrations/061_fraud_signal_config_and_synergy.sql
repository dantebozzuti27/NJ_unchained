-- ============================================================================
-- Migration: 061_fraud_signal_config_and_synergy
--
-- Detection-quality slice. Two coupled changes that together
-- substantially improve the L3a analyst queue without altering the
-- substrate-honesty contract:
--
-- (1) PER-SIGNAL min_actionable_threshold
--     Today every L1 match enters L2/L3a regardless of dollar
--     amount. A $25 contribution from an FEC donor whose name
--     happens to canonical-match an LEIE individual produces the
--     same row as a $25,000 contract paid to a confirmed-excluded
--     contractor. The first is noise; the second is a procurement-
--     fraud red alert. Without per-signal floors, the analyst
--     spends their attention budget on noise.
--
--     We apply the floor at L2 (the entity-pivot view), not at L1
--     (the substrate). L1 stays the substrate-honest record of
--     every match the canonicalizer found. L2/L3a is the analyst-
--     facing surface and filters via JOIN against this config.
--
-- (2) MULTI-SIGNAL FAMILY DIVERSITY BONUS
--     The current scoring function rewards an entity firing on N
--     signals at high percentile. But it does NOT distinguish
--     "5 signals from the same family" from "1 signal each from
--     5 distinct families." From a detection standpoint the
--     latter is far more interesting: independent epistemic
--     sources corroborating the same entity is a stronger
--     evidence shape than the same source observed many times.
--
--     We add a diversity term to fraud_risk_score that rewards
--     distinct CONTRIBUTING families (i.e. families with at
--     least one signal above the 0.95 percentile threshold).
--     Single-family entities are unaffected; multi-family
--     entities get a meaningful boost without saturating the
--     score for one-family-many-signals cases.
--
-- WHY A CONFIG TABLE INSTEAD OF HARDCODED CONSTANTS
-- -------------------------------------------------
-- 1. Operator-tunable. As we collect analyst feedback and learn
--    which thresholds actually filter noise, we update one row
--    per signal -- no migration churn.
-- 2. Centralizes the "what kind of signal is this?" classification
--    (the family tag) in one auditable place. Every refresher
--    contributes its own observations; the families are a cross-
--    cutting concern best owned outside the refreshers.
-- 3. Self-documenting. The `comment` column captures rationale
--    for each threshold so a reader six months from now can see
--    why $200 vs $1000 was chosen.
--
-- WHY NOT FILTER AT L1 (the refreshers' INSERT statement)
-- -------------------------------------------------------
-- Substrate honesty. L1 is the canonicalizer's record of every
-- match. If a future analyst needs to know "did anyone with
-- canonical name X ever appear as a $25 donor to a NJ campaign?"
-- the answer is in L1. Filtering at INSERT time loses that signal
-- permanently. Filtering at the L2 pivot lets the analyst queue
-- stay clean while preserving the full match history for ad-hoc
-- investigation.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- TABLE: derived.fraud_signal_config
-- ----------------------------------------------------------------------------
-- One row per known signal_id. The L2 pivot view JOINs against
-- this table to apply per-signal thresholds and to expose the
-- signal_family tag for the diversity bonus.
--
-- A signal_id present in L1 but absent here will SILENTLY DROP
-- from the analyst queue (the JOIN is INNER), so we add an asset
-- check (orchestration layer) that fails fast when an L1 signal
-- has no config row. This forces every new signal author to
-- register their signal here.
-- ----------------------------------------------------------------------------
CREATE TABLE derived.fraud_signal_config (
    signal_id   TEXT NOT NULL PRIMARY KEY
        CHECK (signal_id ~ '^[a-z][a-z0-9_]+$'),

    -- Epistemic source category. Used for the diversity bonus
    -- in derived.fraud_risk_score. The whitelist is small and
    -- stable; new families require a code change in the scoring
    -- function and an explicit decision about whether the new
    -- family is independent enough to count for diversity.
    signal_family TEXT NOT NULL
        CHECK (signal_family IN (
            'leie_bearing',  -- HHS-OIG LEIE exclusion list
            'workforce',     -- federal-contractor employee donations
            'address',       -- residential-address clustering
            'structural'     -- intra-FEC schema anomalies
        )),

    -- Below-threshold matches drop out of L2/L3a. Numeric so the
    -- threshold can be a dollar amount, a count, or a binary 0/1
    -- depending on what the signal's raw_value semantics are.
    -- Defense-in-depth: the constraint blocks negative thresholds
    -- (which would be incoherent given raw_value >= 0 invariant).
    min_actionable_threshold NUMERIC(20, 2) NOT NULL DEFAULT 0
        CHECK (min_actionable_threshold >= 0),

    -- Free-text rationale. Required so future maintainers know
    -- why a given threshold was chosen.
    comment TEXT NOT NULL CHECK (comment <> ''),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE derived.fraud_signal_config IS
    'Per-signal config: family classification (for diversity bonus) '
    'and min_actionable_threshold (filter floor at L2). One row per '
    'signal_id. A signal_id appearing in L1 but absent here will '
    'silently drop from the analyst queue; the orphan_check asset '
    'check fails fast when this happens.';

COMMENT ON COLUMN derived.fraud_signal_config.signal_family IS
    'Epistemic source category. The diversity bonus in '
    'derived.fraud_risk_score rewards entities firing on multiple '
    'distinct families; same-family signals are not double-counted.';

COMMENT ON COLUMN derived.fraud_signal_config.min_actionable_threshold IS
    'L2-level filter floor on signal_observation.raw_value. Matches '
    'with raw_value < threshold drop out of v_entity_fraud_features '
    'and downstream views, but stay in L1 (fraud_signal_observation) '
    'for ad-hoc forensic queries.';


-- ----------------------------------------------------------------------------
-- SEED: defaults for the 14 signal_ids known as of migration 061
-- ----------------------------------------------------------------------------
-- Threshold rationale per signal:
--
-- LEIE-bearing
--   entity_on_leie                              0     binary indicator
--   entity_funded_and_excluded              10000     federal contracts
--                                                     under $10K are
--                                                     supplies / micro-
--                                                     purchases that the
--                                                     FAR exclusion-check
--                                                     duty effectively
--                                                     does not police
--   donor_on_leie                             200     FEC itemization
--                                                     threshold; below
--                                                     this even FEC does
--                                                     not track donor
--                                                     identity rigorously
--   candidate_funded_by_excluded_donors       200     mirror donor floor
--
-- workforce
--   donor_employed_by_nj_contractor          1000     contractor employer
--                                                     name overlap is
--                                                     common; cluster
--                                                     totals below ~$1K
--                                                     dominate the
--                                                     long-tail noise
--   candidate_funded_by_nj_contractor_employees
--                                            1000     mirror cluster floor
--
-- address
--   committee_address_clusters                  0     cluster-size already
--                                                     filtered upstream
--
-- structural (all binary indicators)
--   treasurer_concentration                     0     concentration ratio
--   candidate_no_pcc                            0     binary
--   candidate_broken_pcc                        0     binary
--   candidate_multiple_pccs                     0     binary
--   committee_name_collisions                   0     binary
--   candidate_namesakes                         0     binary
--   treasurer_is_candidate                      0     binary
--
-- These are operator-tunable; UPDATE sets to retune.
-- ----------------------------------------------------------------------------
INSERT INTO derived.fraud_signal_config
    (signal_id, signal_family, min_actionable_threshold, comment)
VALUES
    ('entity_on_leie',
     'leie_bearing', 0,
     'Binary match indicator (raw_value=1). No dollar floor.'),

    ('entity_funded_and_excluded',
     'leie_bearing', 10000,
     'Federal contracts under $10K are micro-purchases / supplies '
     'orders where FAR 9.405 exclusion-check duty is effectively '
     'unenforced. Below-floor matches stay in L1 for ad-hoc query.'),

    ('donor_on_leie',
     'leie_bearing', 200,
     'FEC itemization threshold. Below $200 even FEC does not '
     'rigorously track donor identity, so canonical-name matches '
     'have inflated false-positive risk on un-itemized rows.'),

    ('candidate_funded_by_excluded_donors',
     'leie_bearing', 200,
     'Mirrors donor-side floor. A candidate''s aggregate excluded-'
     'donor receipts below the FEC itemization threshold is '
     'practically un-actionable.'),

    ('donor_employed_by_nj_contractor',
     'workforce', 1000,
     'Contractor employer-name overlap is common (most large '
     'contractors employ thousands of NJ residents). Cluster totals '
     'below ~$1K dominate long-tail noise without identifying '
     'coordinated patterns.'),

    ('candidate_funded_by_nj_contractor_employees',
     'workforce', 1000,
     'Mirrors cluster-side floor. Below $1K aggregate is below the '
     'noise floor of ordinary politically-engaged contractor '
     'employees.'),

    ('committee_address_clusters',
     'address', 0,
     'Address cluster size pre-filtered in the upstream refresher. '
     'No additional dollar floor.'),

    ('treasurer_concentration',
     'structural', 0,
     'Ratio signal (raw_value is a concentration index). Filtering '
     'belongs at the percentile layer, not at a magnitude floor.'),

    ('candidate_no_pcc',
     'structural', 0,
     'Binary indicator (candidate has no principal campaign '
     'committee). No magnitude semantics.'),

    ('candidate_broken_pcc',
     'structural', 0,
     'Binary indicator (PCC linkage is broken). No magnitude.'),

    ('candidate_multiple_pccs',
     'structural', 0,
     'Binary indicator (candidate has 2+ PCCs). No magnitude.'),

    ('committee_name_collisions',
     'structural', 0,
     'Binary indicator. No magnitude.'),

    ('candidate_namesakes',
     'structural', 0,
     'Binary indicator (multiple candidates share a canonical '
     'name within a cycle). No magnitude.'),

    ('treasurer_is_candidate',
     'structural', 0,
     'Binary indicator (treasurer name = candidate name). No '
     'magnitude.');


-- ----------------------------------------------------------------------------
-- Re-create derived.v_entity_fraud_features with threshold + family columns
-- ----------------------------------------------------------------------------
-- CREATE OR REPLACE VIEW in PG cannot CHANGE existing columns or
-- their order; it can only ADD columns at the end. So we DROP and
-- recreate. The downstream view derived.v_entity_fraud_risk
-- references this one, so we DROP it first too.
--
-- New behavior:
--   * INNER JOIN against fraud_signal_config: a signal_id in L1
--     that is missing from config silently drops from L2. The
--     governance asset check `every_signal_id_has_config_row`
--     catches this case and fails the pipeline.
--   * WHERE raw_value >= cfg.min_actionable_threshold: per-signal
--     dollar floor.
--   * New ARRAY_AGG(signal_family) column for the L3a scoring fn.
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS derived.v_entity_fraud_risk;
DROP VIEW IF EXISTS derived.v_entity_fraud_features;

CREATE VIEW derived.v_entity_fraud_features AS
SELECT
    o.cycle,
    o.entity_kind,
    o.entity_id,

    COUNT(*)::INT                                         AS n_signals_fired,
    MAX(o.severity)::SMALLINT                             AS max_severity,
    MAX(o.peer_percentile)                                AS max_peer_percentile,
    AVG(o.peer_percentile)                                AS avg_peer_percentile,

    (ARRAY_AGG(o.peer_bucket
               ORDER BY o.severity DESC,
                        o.peer_percentile DESC,
                        o.signal_id))[1]
        AS primary_peer_bucket,

    ARRAY_AGG(o.signal_id        ORDER BY o.signal_id)    AS signals_fired,
    ARRAY_AGG(o.severity         ORDER BY o.signal_id)    AS severities,
    ARRAY_AGG(o.peer_percentile  ORDER BY o.signal_id)    AS peer_percentiles,
    ARRAY_AGG(o.peer_bucket      ORDER BY o.signal_id)    AS peer_buckets,
    ARRAY_AGG(o.raw_value        ORDER BY o.signal_id)    AS raw_values,
    ARRAY_AGG(o.evidence_url     ORDER BY o.signal_id)    AS evidence_urls,

    ARRAY_AGG(cfg.signal_family  ORDER BY o.signal_id)    AS signal_families,

    MAX(o.materialized_at)                                AS last_observation_at
FROM   derived.fraud_signal_observation o
JOIN   derived.fraud_signal_config      cfg
       ON cfg.signal_id = o.signal_id
WHERE  o.raw_value >= cfg.min_actionable_threshold
GROUP  BY o.cycle, o.entity_kind, o.entity_id;

COMMENT ON VIEW derived.v_entity_fraud_features IS
    'TIER 4 v3 L2: per-entity wide pivot of L1 observations, with '
    'per-signal min_actionable_threshold filter and signal_family '
    'tags joined from derived.fraud_signal_config. One row per '
    '(cycle, entity_kind, entity_id) where at least one signal '
    'cleared its threshold. The L3 scoring function consumes '
    '(severities, peer_percentiles, signal_families) on this view.';


-- ----------------------------------------------------------------------------
-- New 3-arg scoring function: derived.fraud_risk_score(sev, p, fam)
-- ----------------------------------------------------------------------------
-- Adds a multi-family diversity bonus to the existing tail-only
-- additive scoring formula:
--
--   phi(p, sev)         = sev * max(0, p - 0.95)^2         -- unchanged
--   raw_sum             = SUM_s phi(p_s, sev_s)
--   contributing_fams   = DISTINCT family_s WHERE p_s > 0.95
--   diversity_bonus     = beta * max(0, |contributing_fams| - 1)^2
--   raw_sum_with_bonus  = raw_sum + diversity_bonus
--   risk_score          = 100 * (1 - exp(-k * raw_sum_with_bonus))
--   beta = 0.01, k = 50
--
-- WORKED EXAMPLES (sev=5 each, p=0.99 each)
--   1 signal, 1 family    raw_sum=0.008, bonus=0    score~33
--   2 signals, 1 family   raw_sum=0.016, bonus=0    score~55
--   2 signals, 2 families raw_sum=0.016, bonus=0.01 score~73
--   3 signals, 3 families raw_sum=0.024, bonus=0.04 score~96
--
-- The 1-family-many-signals case is unchanged (preserves backward
-- compat with the analyst's mental model). The multi-family case
-- gets meaningfully more weight, surfacing the analyst's most
-- valuable lead -- an entity corroborated by independent epistemic
-- sources.
--
-- We CREATE the new signature alongside the old 2-arg one rather
-- than dropping the old. The 2-arg version stays as a backward-
-- compat surface for any external caller; the L3a view is rewired
-- to call the new 3-arg version below.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.fraud_risk_score(
    severities       SMALLINT[],
    peer_percentiles NUMERIC[],
    signal_families  TEXT[]
)
RETURNS NUMERIC(5, 2)
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    n_sev INT;
    n_pct INT;
    n_fam INT;
    raw_sum NUMERIC := 0;
    n_contributing_families INT := 0;
    diversity_bonus NUMERIC := 0;
    score NUMERIC;
BEGIN
    IF severities IS NULL OR peer_percentiles IS NULL
       OR signal_families IS NULL THEN
        RETURN 0::NUMERIC(5, 2);
    END IF;

    n_sev := COALESCE(array_length(severities,       1), 0);
    n_pct := COALESCE(array_length(peer_percentiles, 1), 0);
    n_fam := COALESCE(array_length(signal_families,  1), 0);

    IF n_sev = 0 AND n_pct = 0 AND n_fam = 0 THEN
        RETURN 0::NUMERIC(5, 2);
    END IF;

    IF n_sev <> n_pct OR n_sev <> n_fam THEN
        RAISE EXCEPTION
            'fraud_risk_score: severities, peer_percentiles, and '
            'signal_families must have equal length; got %, %, %',
            n_sev, n_pct, n_fam;
    END IF;

    -- Tail-only contribution per signal (unchanged from 2-arg fn).
    SELECT COALESCE(SUM(
               sev::NUMERIC * POWER(GREATEST(0::NUMERIC, p - 0.95), 2)
           ), 0)
      INTO raw_sum
      FROM UNNEST(severities, peer_percentiles) AS t(sev, p);

    -- Multi-family diversity bonus. Only count families with at
    -- least one signal above the 0.95 percentile threshold; below-
    -- threshold signals contribute zero to phi anyway, and we do
    -- not want noise families to inflate the bonus.
    SELECT COUNT(DISTINCT fam)
      INTO n_contributing_families
      FROM UNNEST(peer_percentiles, signal_families) AS t(p, fam)
     WHERE p > 0.95;

    diversity_bonus := 0.01::NUMERIC
                       * POWER(GREATEST(0, n_contributing_families - 1), 2);

    score := 100::NUMERIC
             * (1::NUMERIC
                - EXP(-50::NUMERIC * (raw_sum + diversity_bonus)));

    RETURN LEAST(100::NUMERIC,
                 GREATEST(0::NUMERIC, ROUND(score, 2)))::NUMERIC(5, 2);
END;
$$;

COMMENT ON FUNCTION derived.fraud_risk_score(SMALLINT[], NUMERIC[], TEXT[]) IS
    'TIER 4 v3 L3a scoring (3-arg, family-aware). Composite score in '
    '[0, 100] with the same tail-only additive base as the 2-arg '
    'version PLUS a multi-family diversity bonus '
    '(0.01 * (n_contributing_families - 1)^2). Only families with '
    'at least one signal above the 0.95 percentile threshold count '
    'toward the bonus, so noise families do not inflate the score. '
    'Entities firing on multiple distinct epistemic sources score '
    'meaningfully higher than entities firing many times in one '
    'family at the same per-signal magnitudes.';


-- ----------------------------------------------------------------------------
-- Re-create derived.v_entity_fraud_risk on top of the new L2 view
-- ----------------------------------------------------------------------------
CREATE VIEW derived.v_entity_fraud_risk AS
SELECT
    f.cycle,
    f.entity_kind,
    f.entity_id,

    derived.fraud_risk_score(
        f.severities,
        f.peer_percentiles,
        f.signal_families
    ) AS risk_score,

    f.n_signals_fired,
    f.max_severity,
    f.max_peer_percentile,
    f.avg_peer_percentile,
    f.primary_peer_bucket,

    f.signals_fired,
    f.severities,
    f.peer_percentiles,
    f.peer_buckets,
    f.raw_values,
    f.evidence_urls,
    f.signal_families,

    f.last_observation_at
FROM derived.v_entity_fraud_features f;

COMMENT ON VIEW derived.v_entity_fraud_risk IS
    'TIER 4 v3 read surface: per-entity feature vector + risk_score. '
    'Sort DESC by risk_score for the analyst queue; filter by '
    '(cycle, entity_kind) for the /fec/risk/entities API; deep-link '
    'to /fec/risk/entities/{kind}/{id} for the evidence panel using '
    'all parallel arrays. Score formula now includes the multi-'
    'family diversity bonus from migration 061.';


-- ----------------------------------------------------------------------------
-- updated_at maintenance trigger on fraud_signal_config
-- ----------------------------------------------------------------------------
-- Lightweight: stamp updated_at on every UPDATE so operator changes
-- are audit-traceable. INSERTs already set both timestamps via
-- DEFAULT now().
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.fn_fraud_signal_config_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER fraud_signal_config_updated_at
BEFORE UPDATE ON derived.fraud_signal_config
FOR EACH ROW
EXECUTE FUNCTION derived.fn_fraud_signal_config_updated_at();
