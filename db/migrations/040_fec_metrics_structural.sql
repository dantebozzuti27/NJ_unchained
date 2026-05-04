-- ============================================================================
-- Migration: 040_fec_metrics_structural
--
-- TIER 4 v2 fraud-detection layer (Tier A: STRUCTURAL signals).
--
-- Eight derived views over raw.fec_candidate + raw.fec_committee that
-- compute structural anomalies and concentration signals. Structural
-- means: anomalies in the candidate/committee graph that do NOT depend
-- on the multi-GB indiv contributions table. They are computable
-- immediately after a cn/cm load.
--
-- DESIGN CHOICES
-- ---------------
-- 1. Each metric is a VIEW, not a materialized table. The cn/cm tables
--    are tiny (~10K + ~21K rows for a presidential cycle), so the cost
--    of recomputing on every query is < 50ms total. We materialize
--    only when downstream cost demands it; for cn/cm-grain signals,
--    the freshness benefit (no Dagster refresh dependency) outweighs
--    the per-query cost.
--
-- 2. Every view is CYCLE-SCOPED. A row carries `cycle` so the read API
--    can filter to one election cycle without recomputing the full
--    underlying aggregate. Cross-cycle longitudinal metrics live in
--    a separate v3 layer once we have multi-cycle data loaded.
--
-- 3. Every view exposes a `severity_score` column on the SAME numeric
--    scale across signals (count of flagged entities, capped at 1000
--    for sortability). This lets the UI rank signals consistently
--    without per-signal scaling logic.
--
-- 4. Severity-vs-presence: NOT every row in these views is a
--    confirmed problem. They are LEADS that an analyst should
--    investigate. The UI renders thresholds and labels so it is
--    obvious that "treasurer has 50 committees" needs human triage,
--    not auto-action.
--
-- 5. Schema is `derived` (not `public`) -- the platform convention
--    for cooked analytical layers. The serving layer's API is the
--    only consumer; ad-hoc analysts can also read these directly.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- M1. derived.fec_treasurer_concentration
-- ----------------------------------------------------------------------------
-- Treasurers (tres_nm) ranked by the number of distinct committees
-- they manage in a single cycle. A treasurer-as-shell-network has
-- the signature of one person on dozens of committees -- the Senate
-- ethics rules cap formal multi-committee treasurer relationships
-- but enforcement is weak.
--
-- Threshold (informational, NOT enforced): manageable < 5 committees;
-- 5-15 = legitimate professional treasurer firm; > 15 = leads.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.fec_treasurer_concentration AS
SELECT
    cycle,
    -- Canonicalize the treasurer string for stable grouping.
    -- FEC has the same person spelled "DOE, JANE", "DOE, JANE A.",
    -- and "DOE, JANE A" across rows; we strip middle initials and
    -- collapse whitespace for the GROUP BY key (preserved verbatim
    -- in tres_nm_canonical for inspection).
    REGEXP_REPLACE(UPPER(TRIM(tres_nm)), '\s+', ' ', 'g')   AS tres_nm_canonical,
    COUNT(DISTINCT cmte_id)                                  AS n_committees,
    COUNT(DISTINCT cmte_st)                                  AS n_states,
    LEAST(COUNT(DISTINCT cmte_id), 1000)                     AS severity_score,
    ARRAY_AGG(DISTINCT cmte_id ORDER BY cmte_id)             AS committee_ids,
    ARRAY_AGG(DISTINCT cmte_pty_affiliation
              ORDER BY cmte_pty_affiliation)                 AS parties_seen
FROM   raw.fec_committee
WHERE  tres_nm IS NOT NULL AND tres_nm <> ''
GROUP  BY 1, 2
HAVING COUNT(DISTINCT cmte_id) >= 2
ORDER  BY n_committees DESC;

COMMENT ON VIEW derived.fec_treasurer_concentration IS
'TIER 4 v2.A.1: Treasurers managing multiple committees in one cycle. '
'Rows where n_committees >= 15 are likely leads for shell-network triage; '
'2-15 is normal for professional treasurer firms.';


-- ----------------------------------------------------------------------------
-- M2. derived.fec_candidate_no_pcc
-- ----------------------------------------------------------------------------
-- Candidates with no Principal Campaign Committee declared. Per FEC
-- regulations every candidate who raises or spends > $5,000 must
-- designate a PCC within 15 days. A candidate with no PCC is either
-- (a) below the registration threshold (legitimate, ~half of these)
-- or (b) a registration anomaly worth investigating.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.fec_candidate_no_pcc AS
SELECT
    cycle,
    cand_id,
    cand_name,
    cand_pty_affiliation,
    cand_office,
    cand_office_st,
    cand_office_district,
    cand_status,
    cand_ici,
    -- All these candidates have NULL or empty cand_pcc by definition;
    -- the severity is 1 per row (each is a single anomaly).
    1::INT                                                   AS severity_score
FROM   raw.fec_candidate
WHERE  cand_pcc IS NULL OR cand_pcc = '';

COMMENT ON VIEW derived.fec_candidate_no_pcc IS
'TIER 4 v2.A.2: Candidates with no declared Principal Campaign Committee. '
'Most legitimately are sub-$5k candidates; a small fraction are filing '
'anomalies. Cross-reference cand_status to filter active filings.';


-- ----------------------------------------------------------------------------
-- M3. derived.fec_candidate_broken_pcc
-- ----------------------------------------------------------------------------
-- Candidates whose declared cand_pcc does NOT exist as a committee in
-- raw.fec_committee for the same cycle. This is a referential gap
-- that should be near-zero in clean data; non-zero rates are either
-- ingestion bugs (we missed cm rows) or genuine FEC data integrity
-- issues (the committee was de-registered after the candidate
-- declared it).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.fec_candidate_broken_pcc AS
SELECT
    c.cycle,
    c.cand_id,
    c.cand_name,
    c.cand_office_st,
    c.cand_pcc                                               AS missing_cmte_id,
    c.cand_status,
    1::INT                                                   AS severity_score
FROM   raw.fec_candidate c
LEFT   JOIN raw.fec_committee m
    ON m.cmte_id = c.cand_pcc AND m.cycle = c.cycle
WHERE  c.cand_pcc IS NOT NULL
  AND  c.cand_pcc <> ''
  AND  m.cmte_id IS NULL;

COMMENT ON VIEW derived.fec_candidate_broken_pcc IS
'TIER 4 v2.A.3: Candidates whose cand_pcc references a committee not '
'present in raw.fec_committee for the same cycle. Referential anomaly; '
'expected near-zero. Persistent non-zero counts indicate stale FEC data.';


-- ----------------------------------------------------------------------------
-- M4. derived.fec_candidate_multiple_pccs
-- ----------------------------------------------------------------------------
-- Candidates affiliated with MULTIPLE committees designated 'P'
-- (Principal Campaign Committee) in the same cycle. By regulation a
-- candidate has exactly one PCC at a time; multiple P designations
-- usually indicate a successor committee transition that did not
-- get the prior committee re-coded, OR a filing race condition.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.fec_candidate_multiple_pccs AS
SELECT
    cm.cycle,
    cm.cand_id,
    c.cand_name,
    c.cand_office_st,
    c.cand_office,
    COUNT(*)                                                 AS n_pccs,
    LEAST(COUNT(*), 1000)                                    AS severity_score,
    ARRAY_AGG(cm.cmte_id ORDER BY cm.cmte_id)                AS pcc_ids,
    ARRAY_AGG(cm.cmte_nm ORDER BY cm.cmte_id)                AS pcc_names
FROM   raw.fec_committee cm
LEFT   JOIN raw.fec_candidate c
    ON c.cand_id = cm.cand_id AND c.cycle = cm.cycle
WHERE  cm.cmte_dsgn = 'P'
  AND  cm.cand_id IS NOT NULL AND cm.cand_id <> ''
GROUP  BY 1, 2, 3, 4, 5
HAVING COUNT(*) > 1
ORDER  BY n_pccs DESC;

COMMENT ON VIEW derived.fec_candidate_multiple_pccs IS
'TIER 4 v2.A.4: Candidates with > 1 Principal Campaign Committee in one '
'cycle. By regulation should be exactly 1; > 1 indicates successor-committee '
'transitions, filing errors, or race conditions in FEC ingestion.';


-- ----------------------------------------------------------------------------
-- M5. derived.fec_committee_address_clusters
-- ----------------------------------------------------------------------------
-- Multiple committees registered at the SAME street address. A
-- legitimate "Friends of X" + "X Leadership PAC" + "X Victory Fund"
-- might share a campaign HQ address; a shell network has dozens of
-- "PAC" committees registered at one PO box.
--
-- We canonicalize address by upper-casing and stripping internal
-- whitespace runs; this catches "PO BOX 123", "P.O. BOX 123",
-- "P O BOX 123" as the same address.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.fec_committee_address_clusters AS
SELECT
    cycle,
    REGEXP_REPLACE(UPPER(TRIM(cmte_st1)), '\s+', ' ', 'g')   AS address_canonical,
    REGEXP_REPLACE(UPPER(TRIM(cmte_city)), '\s+', ' ', 'g')  AS city_canonical,
    cmte_st                                                  AS state,
    REGEXP_REPLACE(cmte_zip, '\D', '', 'g')                  AS zip_canonical,
    COUNT(DISTINCT cmte_id)                                  AS n_committees,
    LEAST(COUNT(DISTINCT cmte_id), 1000)                     AS severity_score,
    ARRAY_AGG(DISTINCT cmte_id ORDER BY cmte_id)             AS committee_ids,
    ARRAY_AGG(DISTINCT cmte_pty_affiliation
              ORDER BY cmte_pty_affiliation)                 AS parties_seen
FROM   raw.fec_committee
WHERE  cmte_st1 IS NOT NULL AND cmte_st1 <> ''
GROUP  BY 1, 2, 3, 4, 5
HAVING COUNT(DISTINCT cmte_id) >= 3
ORDER  BY n_committees DESC;

COMMENT ON VIEW derived.fec_committee_address_clusters IS
'TIER 4 v2.A.5: Street addresses hosting >=3 distinct committees in one '
'cycle. Many "Friends of X" share campaign HQs (legitimate); shell networks '
'concentrate dozens at single PO boxes.';


-- ----------------------------------------------------------------------------
-- M6. derived.fec_committee_name_collisions
-- ----------------------------------------------------------------------------
-- Different committee IDs registered under the SAME canonical name.
-- Real-world causes: name reuse after a committee terminated; near-
-- duplicate filings ("Friends of Smith" vs "FRIENDS OF SMITH INC").
-- Both are confusion vectors that downstream donor-tracking systems
-- conflate.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.fec_committee_name_collisions AS
SELECT
    cycle,
    REGEXP_REPLACE(UPPER(TRIM(cmte_nm)), '\s+', ' ', 'g')    AS cmte_nm_canonical,
    COUNT(DISTINCT cmte_id)                                  AS n_committee_ids,
    LEAST(COUNT(DISTINCT cmte_id), 1000)                     AS severity_score,
    ARRAY_AGG(DISTINCT cmte_id ORDER BY cmte_id)             AS committee_ids,
    ARRAY_AGG(DISTINCT cmte_st ORDER BY cmte_st)             AS states_seen
FROM   raw.fec_committee
WHERE  cmte_nm IS NOT NULL AND cmte_nm <> ''
GROUP  BY 1, 2
HAVING COUNT(DISTINCT cmte_id) > 1
ORDER  BY n_committee_ids DESC;

COMMENT ON VIEW derived.fec_committee_name_collisions IS
'TIER 4 v2.A.6: Committees with identical canonicalized names but different '
'cmte_ids in one cycle. Indicates name reuse, near-duplicate filings, or '
'cross-state name collisions.';


-- ----------------------------------------------------------------------------
-- M7. derived.fec_candidate_namesakes
-- ----------------------------------------------------------------------------
-- Different candidate IDs filed under the SAME canonical name within
-- the same cycle. Most are coincidence (lots of "JOHN SMITH" run for
-- House); same-state same-office namesakes are the suspicious slice
-- (possible impersonation or duplicate filings).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.fec_candidate_namesakes AS
SELECT
    cycle,
    REGEXP_REPLACE(UPPER(TRIM(cand_name)), '\s+', ' ', 'g')  AS cand_name_canonical,
    cand_office_st,
    cand_office,
    COUNT(DISTINCT cand_id)                                  AS n_cand_ids,
    LEAST(COUNT(DISTINCT cand_id), 1000)                     AS severity_score,
    ARRAY_AGG(DISTINCT cand_id ORDER BY cand_id)             AS candidate_ids,
    ARRAY_AGG(DISTINCT cand_pty_affiliation
              ORDER BY cand_pty_affiliation)                 AS parties_seen
FROM   raw.fec_candidate
WHERE  cand_name IS NOT NULL AND cand_name <> ''
GROUP  BY 1, 2, 3, 4
HAVING COUNT(DISTINCT cand_id) > 1
ORDER  BY n_cand_ids DESC;

COMMENT ON VIEW derived.fec_candidate_namesakes IS
'TIER 4 v2.A.7: Same canonical candidate name with multiple cand_ids in '
'the same cycle/state/office. Same-state same-office namesakes are leads '
'for duplicate-filing or impersonation review.';


-- ----------------------------------------------------------------------------
-- M8. derived.fec_treasurer_is_candidate
-- ----------------------------------------------------------------------------
-- Committees where the treasurer (tres_nm) and the candidate
-- (cand_name) appear to be the same person. By itself not necessarily
-- improper -- many self-funded local candidates serve as their own
-- treasurer -- but it is a signal worth surfacing because (a) it
-- collapses the audit chain, (b) it correlates with self-funded
-- campaigns where the candidate dominates donations.
--
-- Match on canonical name (uppercase, whitespace-collapsed). Both
-- columns use FEC's "LAST, FIRST [MIDDLE]" convention so the format
-- aligns naturally.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.fec_treasurer_is_candidate AS
SELECT
    cm.cycle,
    cm.cmte_id,
    cm.cmte_nm                                               AS committee_name,
    cm.tres_nm                                               AS treasurer_name,
    cm.cmte_dsgn,
    cm.cmte_tp,
    cm.cmte_st,
    c.cand_id,
    c.cand_name,
    c.cand_office,
    c.cand_office_st,
    1::INT                                                   AS severity_score
FROM   raw.fec_committee cm
JOIN   raw.fec_candidate c
    ON c.cand_id = cm.cand_id AND c.cycle = cm.cycle
WHERE  cm.tres_nm IS NOT NULL
  AND  c.cand_name IS NOT NULL
  AND  REGEXP_REPLACE(UPPER(TRIM(cm.tres_nm)), '\s+', ' ', 'g')
     = REGEXP_REPLACE(UPPER(TRIM(c.cand_name)), '\s+', ' ', 'g');

COMMENT ON VIEW derived.fec_treasurer_is_candidate IS
'TIER 4 v2.A.8: Committees where the treasurer name matches the linked '
'candidate name (canonical match). Self-treasurer flag; legitimate for '
'small local campaigns but worth surfacing for audit-chain visibility.';
