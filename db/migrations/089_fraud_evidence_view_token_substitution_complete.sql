-- ============================================================================
-- Migration: 089_fraud_evidence_view_token_substitution_complete
--
-- VISION_2026 Pillar 2 (civic integrity) -- Phase F-UX work item F4
-- patch. Closes a substitution gap discovered during the 088 production
-- deploy: the chained REPLACE in derived.v_entity_fraud_evidence handled
-- {{entity_id}} {{cycle}} {{raw_value}} {{peer_percentile}} but NOT the
-- two additional tokens that seed 018's plain_english_template strings
-- ALSO use:
--
--   * {{entity_kind}}  -- used by `entity_funded_and_excluded` to render
--                        whether the joint LEIE+FAR finding applies to a
--                        committee or a candidate.
--   * {{peer_bucket}}  -- used by `committee_address_clusters` and
--                        `treasurer_concentration` to render the peer
--                        cohort the percentile is computed against
--                        (e.g., "state=NJ peer rank").
--
-- The 088 deploy verification flagged 1,072 treasurer_concentration rows
-- + 530 committee_address_clusters rows leaking literal "{{peer_bucket}}"
-- into rendered_explanation. The other 4,527 observations rendered
-- cleanly because their templates did not use those tokens.
--
-- WHY A NEW MIGRATION (not edit-088-and-rerun)
-- ---------------------------------------------
-- Migration 088 is already recorded in governance.schema_migrations on
-- production with sha256=8b231578... -- editing 088 would either fail the
-- sha-drift check on the next deploy or, worse, silently mask the bug
-- forever if drift detection is bypassed. The platform's verifiable-data
-- contract requires the migration ledger to be append-only.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- A pure CREATE OR REPLACE VIEW for derived.v_entity_fraud_evidence with
-- the same column shape as 088, but with the rendered_explanation chain
-- extended by two REPLACE() steps for {{entity_kind}} and {{peer_bucket}}.
-- All other view columns (is_nj, display_name, citation_*, severity_*,
-- upstream_verify_*) are preserved verbatim.
--
-- WHAT THIS MIGRATION DOES NOT DO
-- -------------------------------
-- * Modify the underlying observation table or the human_explanation /
--   severity_calibration / evidence_url_template reference tables --
--   those are the source of truth for the tokens; this migration only
--   teaches the view to substitute every token they emit.
-- * Add a new column. The fix is a render-pipeline correction, not a
--   schema change.
-- * Touch v_nj_federal_officials. That view is unaffected.
-- ============================================================================

BEGIN;


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
)
SELECT
    o.cycle,
    o.entity_kind,
    o.entity_id,
    o.signal_id,
    o.raw_value,
    o.severity,
    o.peer_bucket,
    o.peer_percentile,
    o.materialized_at,

    CASE o.entity_kind
        WHEN 'candidate' THEN COALESCE(cand.is_nj,  FALSE)
        WHEN 'committee' THEN COALESCE(cmte.is_nj,  FALSE)
        WHEN 'treasurer' THEN COALESCE(treas.is_nj, FALSE)
        WHEN 'address'   THEN (SPLIT_PART(o.entity_id, '|', 3) = 'NJ')
        ELSE FALSE
    END                                                          AS is_nj,

    CASE o.entity_kind
        WHEN 'candidate' THEN cand.cand_name
        WHEN 'committee' THEN cmte.cmte_nm
        WHEN 'treasurer' THEN o.entity_id
        WHEN 'address'   THEN SPLIT_PART(o.entity_id, '|', 1)
                              || COALESCE(', ' || SPLIT_PART(o.entity_id, '|', 2), '')
                              || COALESCE(' '  || SPLIT_PART(o.entity_id, '|', 3), '')
                              || COALESCE(' '  || SPLIT_PART(o.entity_id, '|', 4), '')
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

    -- ------------------------------------------------------------------
    -- Plain-English explanation with COMPLETE token substitution.
    -- The chained REPLACE handles every token currently emitted by
    -- ref.fraud_signal_human_explanation.plain_english_template:
    --     {{entity_id}} {{cycle}} {{raw_value}} {{peer_percentile}}
    --     {{entity_kind}} {{peer_bucket}}
    -- A future signal that introduces a new token MUST extend this
    -- chain AND ship the matching test case in
    -- tests/test_phase_f4_evidence_view_and_nj_officials.py
    -- (TestEvidenceViewAllSignalsRenderClean::
    --  test_no_residue_in_any_template_after_render).
    -- ------------------------------------------------------------------
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
LEFT JOIN ref.fraud_signal_human_explanation        he   ON he.signal_id  = o.signal_id
LEFT JOIN ref.fraud_signal_severity_calibration     sc   ON sc.signal_id  = o.signal_id
LEFT JOIN ref.fraud_signal_evidence_url_template    eut  ON eut.signal_id = o.signal_id;

COMMENT ON VIEW derived.v_entity_fraud_evidence IS
    'Canonical join from fraud_signal_observation -> rendered plain-English '
    '(all tokens substituted: entity_id, cycle, raw_value, peer_percentile, '
    'entity_kind, peer_bucket) + federal-authority citation + severity '
    'precedent + display metadata + NJ-relevance + upstream-verify URL. '
    'One row per fired signal. Stacks on 2.2.0-fraud-evidence-view-v1; '
    'mig 089 patches the substitution chain to cover every token currently '
    'emitted by ref.fraud_signal_human_explanation.plain_english_template.';


COMMIT;
