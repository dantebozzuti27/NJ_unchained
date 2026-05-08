-- ============================================================================
-- Migration: 088_fraud_evidence_view_and_nj_officials
--
-- VISION_2026 Pillar 2 (civic integrity) -- Phase F-UX, work items F4 + F6/F7
-- substrate. Closes the gap between (a) the L1 observations table that holds
-- the anomaly evidence and (b) the UI cards that need rendered plain-English
-- explanations + federal-authority citations + upstream-verify URLs.
--
-- WHY THIS MIGRATION EXISTS
-- -------------------------
-- F2/F3/F5 (mig 086 + seeds 018/019) shipped the citation + severity-precedent
-- substrate. F1 (FEC bulk loader) populated raw.fec_* and materialized 6,129
-- structural observations across 8 active signals. The /risk pages today read
-- from derived.v_entity_fraud_risk only -- they get scores and signal IDs but
-- nothing humans can read. The user-facing problem statement, verbatim:
--
--     "the risk page is still broken. why do i see candidates in texas? why
--      doesn't the risk page actually have a description of the issues? it's
--      impossible to read and we limited this to new jersey. we also should
--      surface more powerful politicians like federal congressman senators
--      and governors in NJ"
--
-- This migration ships THREE substrate surfaces that close that gap:
--
--   1. ref.fraud_signal_evidence_url_template -- per-signal_id template for
--      the upstream-verify URL the UI exposes as the "verify on FEC.gov"
--      button. Today fraud_signal_observation.evidence_url stores a relative
--      INTERNAL route (/fec/metrics/...) -- useful for platform navigation
--      but NOT for the substrate-honesty contract. The UI must give analysts
--      a link to the raw federal-source row that triggered the signal so they
--      can independently verify. That URL is templated per signal_id with
--      {{entity_id}} and {{cycle}} substituted at view-render time.
--
--   2. derived.v_entity_fraud_evidence -- the canonical JOIN from a single
--      firing observation -> the signal's federal-authority citation +
--      severity precedent + entity display name + office context (for
--      candidate-kind entities) + NJ relevance flag + rendered plain-English
--      explanation (with template tokens substituted) + upstream-verify URL.
--      One row per (cycle, entity_kind, entity_id, signal_id) -- the natural
--      key of fraud_signal_observation. The UI's per-entity drill-down page
--      reads ALL of its evidence cards from this view; nothing else.
--
--   3. derived.v_nj_federal_officials -- the curated card-grid roster for
--      the /risk overview page's "Section 1: NJ federal officials". Filters
--      raw.fec_candidate to incumbents (cand_ici='I' AND cand_status='C')
--      with cand_office_st='NJ' AND cand_office IN ('S','H'). Joins to
--      v_entity_fraud_risk so each official carries their score (will be
--      0 for the 14 sitting NJ federal incumbents in the May-2026 substrate;
--      a score > 0 should be loud and visible if it ever fires). Includes
--      n_signals=0 -> green-check rendering and n_signals>0 -> red-badge.
--
-- WHAT THIS MIGRATION DELIBERATELY DOES NOT DO
-- --------------------------------------------
--   * Add NJ-state or municipal politicians (governor, state legislature,
--     mayors). Those live at NJ ELEC (Election Law Enforcement Commission),
--     NOT FEC bulk -- the FEC's authority stops at federal seats. An ELEC
--     ingester is its own work-item (parallel to F1's FEC ingester) and is
--     scoped in work_left.txt under "F8.5: NJ ELEC state-and-municipal
--     campaign-finance ingester". Until that ships, the NJ-officials roster
--     is FEDERAL ONLY -- the UI must say so (substrate-honesty G2: scope
--     boundary explicit).
--   * Rewire the refresher to consume severity_level from the calibration
--     table. That's still the F4-extension work; severity is hardcoded in
--     the per-signal refresher. The calibration table DOCUMENTS the
--     existing severity, the F4-extension migration will INVERT the
--     dependency.
--   * Materialize v_entity_fraud_evidence. With ~6K observations today
--     and a query plan of (4 left joins on 17-row reference tables plus
--     a 9.8K-row fec_candidate scan + 21K-row fec_committee scan) the view
--     reads in <100ms uncached. Materialize when L1 reaches ~50K rows.
--   * Build NJ-only view of v_entity_fraud_evidence. The is_nj column is
--     a column ON the canonical view; UI filters with WHERE is_nj. A
--     separate view would risk drift between two surfaces of the same
--     truth.
--
-- DESIGN DECISIONS
-- ----------------
-- * Plain-English template substitution happens at the SQL layer, not in
--   TypeScript or React. Reasoning: the template tokens ({{entity_id}},
--   {{cycle}}, {{raw_value}}) live in the same row as the data they
--   reference; render-at-query-time guarantees a single source of truth.
--   Client-side substitution would mean two render paths (test-suite SQL
--   tests would render differently from live UI). The substitution is a
--   chained REPLACE() -- cheap on a 17-row template table.
--
-- * The is_nj column is a CASE expression over entity_kind, not a function
--   call. Reasoning: a STABLE plpgsql function executing per-row over
--   ~6K rows would do ~6K EXISTS subqueries against raw.fec_committee.
--   The CASE expression with LEFT JOINs to raw.fec_candidate and
--   raw.fec_committee runs as a hash join (~100ms uncached). The address
--   case is pure string parsing on entity_id (SPLIT_PART(entity_id, '|', 3)
--   gives the state token from the canonical 4-tuple address|city|state|zip5).
--
-- * v_nj_federal_officials filter is cand_ici='I' AND cand_status='C',
--   NOT cand_election_yr=2024. Reasoning: Booker's cycle-2024 record has
--   cand_election_yr=2026 because he runs in the 2026 cycle (Senate
--   staggered terms). Including cand_election_yr=2024 would drop Booker
--   and yield only 13 officials. The cycle-2024 file holds the snapshot
--   of who was incumbent during cycle 2024 regardless of when their
--   re-election year falls. This catches Bob Menendez Sr. (lost primary
--   2024, succeeded by Andy Kim) which is the SUBSTRATE-HONEST view --
--   FEC bulk reflects who FILED for cycle 2024, not who currently holds
--   the seat. To get "who currently holds the seat as of May 2026" the
--   platform would ingest cycle 2026 -- a future work item.
--
-- * Treasurer NJ-relevance is BOOL_OR(cmte_st='NJ') over committees this
--   treasurer treasures. A treasurer who serves both NJ and PA committees
--   is is_nj=TRUE. This is correct: treasurer concentration as a fraud
--   signal cares whether the treasurer has any NJ exposure, not whether
--   they exclusively work NJ. The F8 LEIE/SAM ingesters preserve the same
--   semantics.
--
-- * Address NJ-relevance reads from entity_id directly (no join needed)
--   because address entity_ids are constructed by the
--   committee_address_clusters refresher as the canonical 4-tuple
--   address|city|state|zip5 (mig 087). SPLIT_PART(entity_id, '|', 3)
--   yields the state. Robust to NULL city or NULL zip5.
--
-- * The v_entity_fraud_evidence query plan is 4 left joins:
--     observation -> human_explanation (PK signal_id, 17 rows)
--                 -> severity_calibration (PK signal_id, 17 rows)
--                 -> evidence_url_template (PK signal_id, 17 rows)
--                 -> fec_candidate (PK cycle+cand_id, 9.8K rows for cycle 2024)
--                 -> fec_committee (PK cycle+cmte_id, 21K rows for cycle 2024)
--                 -> treasurer_nj CTE (aggregate, 4.5K rows for cycle 2024)
--   All joined on hashable PK columns. ANALYZE-driven plan = hash join.
--
-- * Raw URL templates use {{entity_id}} and {{cycle}}, NOT $1 / $2. Reason:
--   the substitution is at query-time over view rows, not at prepare-time
--   over query parameters. {{...}} is the same convention as the
--   plain_english_template column in the human-explanation table -- ONE
--   token convention, not two.
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration. Stacks on 2.1.0-fraud-evidence-substrate-v1.
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.2.0-fraud-evidence-view-v1',
    'Pillar 2 (civic integrity) Phase F-UX work items F4 + F6/F7 substrate: '
    'derived.v_entity_fraud_evidence (canonical join from observation -> '
    'rendered plain-English + federal authority citation + severity precedent '
    '+ upstream-verify URL + NJ-relevance flag + office context); '
    'derived.v_nj_federal_officials (curated NJ federal incumbent roster for '
    'the /risk overview Section 1 card grid); '
    'ref.fraud_signal_evidence_url_template (per-signal_id upstream-verify '
    'URL templates with {{entity_id}}, {{cycle}} substitution). '
    'Spec .cursor/rules/verifiable-data.mdc rules 1, 2, 3, 4, 5 -- delivers UX '
    'guarantees G1, G2, G3, G6 from the F-UX plan in work_left.txt.',
    '2026-05-08'::DATE,
    'Stacks on 2.1.0-fraud-evidence-substrate-v1. The is_nj column on '
    'v_entity_fraud_evidence is computed via CASE-over-LEFT-JOIN, not a '
    'plpgsql function; the latter would do ~6K row-by-row EXISTS subqueries.'
)
ON CONFLICT (formula_version) DO NOTHING;


-- ----------------------------------------------------------------------------
-- ref.fraud_signal_evidence_url_template
--
-- One row per signal_id. The UI's "verify on FEC.gov" / "verify on OIG.gov"
-- / "verify on SAM.gov" button reads url_template, substitutes {{entity_id}}
-- and {{cycle}}, and links out. Without this row the signal cannot be
-- substrate-honest: there is no analyst pathway to the raw federal record
-- that triggered the firing.
--
-- The template is signal-specific (not entity-kind-specific) because (a)
-- entity_kind and signal_id are functionally dependent in the current
-- 17-signal taxonomy and (b) some signals on the same entity_kind point
-- to different upstream surfaces (e.g., candidate_namesakes points to the
-- FEC candidate-search results page filtered by name, while
-- candidate_no_pcc points to the candidate detail page).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.fraud_signal_evidence_url_template (
    signal_id              TEXT          NOT NULL PRIMARY KEY
        REFERENCES derived.fraud_signal_config(signal_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    url_template           TEXT          NOT NULL,
    button_label           TEXT          NOT NULL,
    upstream_source        TEXT          NOT NULL,

    formula_version        TEXT          NOT NULL
        REFERENCES ref.formula_version(formula_version),
    effective_date         DATE          NOT NULL DEFAULT CURRENT_DATE,
    created_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fraud_signal_evidence_url_template_url_chk
        CHECK (url_template LIKE 'https://%' AND length(url_template) >= 15),

    CONSTRAINT fraud_signal_evidence_url_template_button_chk
        CHECK (length(button_label) BETWEEN 4 AND 60),

    CONSTRAINT fraud_signal_evidence_url_template_upstream_chk
        CHECK (upstream_source IN (
            'FEC.gov',
            'OIG.gov',
            'SAM.gov',
            'USAspending.gov',
            'platform-internal'
        ))
);

CREATE INDEX IF NOT EXISTS idx_fraud_signal_evidence_url_template_upstream
    ON ref.fraud_signal_evidence_url_template (upstream_source);

COMMENT ON TABLE ref.fraud_signal_evidence_url_template IS
    'Per-signal_id template for the upstream-verify URL. Substitutes '
    '{{entity_id}} and {{cycle}} at view-render time. Read exclusively by '
    'derived.v_entity_fraud_evidence; never by the UI directly.';

COMMENT ON COLUMN ref.fraud_signal_evidence_url_template.url_template IS
    'HTTPS URL template with {{entity_id}} and {{cycle}} placeholders. Both '
    'are optional -- some signals (e.g., committee_address_clusters) have '
    'composite entity_ids that cannot be embedded directly; those templates '
    'point to a generic FEC search page. The substitution is a chained '
    'REPLACE() in v_entity_fraud_evidence; tokens that do not appear in the '
    'template are silently no-ops.';

COMMENT ON COLUMN ref.fraud_signal_evidence_url_template.button_label IS
    'UI button label, e.g., "Verify on FEC.gov" or "View OIG LEIE listing".';

COMMENT ON COLUMN ref.fraud_signal_evidence_url_template.upstream_source IS
    'Upstream authority hosting the verifiable record. Drives the icon/badge '
    'rendered alongside the button.';


CREATE OR REPLACE FUNCTION ref._fraud_signal_evidence_url_template_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fraud_signal_evidence_url_template_updated_at
    ON ref.fraud_signal_evidence_url_template;

CREATE TRIGGER fraud_signal_evidence_url_template_updated_at
BEFORE UPDATE ON ref.fraud_signal_evidence_url_template
FOR EACH ROW
EXECUTE FUNCTION ref._fraud_signal_evidence_url_template_set_updated_at();


-- ----------------------------------------------------------------------------
-- derived.v_entity_fraud_evidence
--
-- Canonical join from observation -> rendered plain-English + citation +
-- severity precedent + display metadata + NJ relevance + upstream-verify URL.
-- One row per fired signal observation. Read by the per-entity detail page
-- (one entity, multiple rows = the evidence cards) AND by the overview page
-- (filter is_nj=TRUE, distinct-on entity_id, ordered by severity desc).
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
    -- Treasurer entity_id is the canonical name (mixed-case in raw, but the
    -- refresher emits whatever case fec_committee.tres_nm carries -- we match
    -- on UPPER(TRIM(...)) here for robustness against case drift).
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

    -- ------------------------------------------------------------------
    -- NJ relevance (column-not-function for performance)
    -- ------------------------------------------------------------------
    CASE o.entity_kind
        WHEN 'candidate' THEN COALESCE(cand.is_nj,  FALSE)
        WHEN 'committee' THEN COALESCE(cmte.is_nj,  FALSE)
        WHEN 'treasurer' THEN COALESCE(treas.is_nj, FALSE)
        WHEN 'address'   THEN (SPLIT_PART(o.entity_id, '|', 3) = 'NJ')
        ELSE FALSE
    END                                                          AS is_nj,

    -- ------------------------------------------------------------------
    -- Display name (entity-kind specific)
    -- ------------------------------------------------------------------
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

    -- ------------------------------------------------------------------
    -- Office context (NULL for non-candidate entities)
    -- ------------------------------------------------------------------
    cand.cand_office                                             AS office_code,
    cand.cand_office_st                                          AS office_state,
    cand.cand_office_district                                    AS office_district,
    cand.cand_pty_affiliation                                    AS office_party,
    cand.cand_ici                                                AS office_incumbent_status,
    cand.cand_election_yr                                        AS office_election_year,

    -- Treasurer-specific context (NULL for non-treasurer entities)
    treas.n_committees_treasured                                 AS treasurer_n_committees,
    treas.n_nj_committees_treasured                              AS treasurer_n_nj_committees,

    -- Committee-specific context (NULL for non-committee entities)
    cmte.cmte_st                                                 AS committee_state,
    cmte.cmte_city                                               AS committee_city,
    cmte.tres_nm                                                 AS committee_treasurer_name,
    cmte.pcc_cand_id                                             AS committee_pcc_candidate_id,

    -- ------------------------------------------------------------------
    -- Federal-authority citation (from ref.fraud_signal_human_explanation)
    -- ------------------------------------------------------------------
    he.rule_text                                                 AS rule_text,
    he.citation_authority                                        AS citation_authority,
    he.citation_section                                          AS citation_section,
    he.citation_url                                              AS citation_url,

    -- ------------------------------------------------------------------
    -- Plain-English explanation with template token substitution
    --
    -- The chained REPLACE handles the four canonical tokens. Tokens that
    -- do not appear in a template are silently no-ops; tokens that appear
    -- but are not in this list will pass through verbatim and will trip
    -- the "no-residue" assertion in the v_entity_fraud_evidence test.
    -- ------------------------------------------------------------------
    REPLACE(REPLACE(REPLACE(REPLACE(
        COALESCE(he.plain_english_template, ''),
        '{{entity_id}}',       o.entity_id),
        '{{cycle}}',           o.cycle),
        '{{raw_value}}',       COALESCE(o.raw_value::TEXT, '')),
        '{{peer_percentile}}', COALESCE(ROUND(o.peer_percentile * 100, 1)::TEXT, ''))
                                                                 AS rendered_explanation,

    -- ------------------------------------------------------------------
    -- Severity precedent (from ref.fraud_signal_severity_calibration)
    -- ------------------------------------------------------------------
    sc.calibration_basis                                         AS severity_basis,
    sc.precedent_url                                             AS severity_precedent_url,
    sc.precedent_summary                                         AS severity_precedent_summary,

    -- ------------------------------------------------------------------
    -- Upstream-verify URL with {{entity_id}}, {{cycle}} substitution
    -- ------------------------------------------------------------------
    REPLACE(REPLACE(
        COALESCE(eut.url_template, o.evidence_url),
        '{{entity_id}}', o.entity_id),
        '{{cycle}}',     o.cycle)
                                                                 AS upstream_verify_url,
    eut.button_label                                             AS upstream_verify_label,
    eut.upstream_source                                          AS upstream_source,

    -- ------------------------------------------------------------------
    -- Provenance: which formula_version produced this row
    -- ------------------------------------------------------------------
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
    'Canonical join from fraud_signal_observation -> rendered plain-English + '
    'federal-authority citation + severity precedent + display metadata + '
    'NJ-relevance + upstream-verify URL. One row per fired signal. The UI '
    'detail page reads ALL of its evidence cards from this view. The overview '
    'page filters is_nj=TRUE and aggregates per entity. Stacks on '
    '2.1.0-fraud-evidence-substrate-v1.';


-- ----------------------------------------------------------------------------
-- derived.v_nj_federal_officials
--
-- Curated card-grid roster for the /risk overview Section 1: "NJ federal
-- officials." Filters raw.fec_candidate to incumbents who filed for the
-- specified cycle. Joins to v_entity_fraud_risk so each official carries
-- their score (0 = no fraud signals firing, > 0 = at least one signal
-- fires; the UI uses risk_score=0 to render a green check, > 0 to render
-- a red badge with signal count).
--
-- Ordering: Senate first, then House by district. The UI honors view
-- ordering (no client-side resort).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_nj_federal_officials AS
SELECT
    c.cycle,
    c.cand_id                                                AS entity_id,
    c.cand_name                                              AS official_name,
    c.cand_office                                            AS office_code,
    c.cand_office_district                                   AS office_district,
    c.cand_pty_affiliation                                   AS office_party,
    c.cand_ici                                               AS incumbent_status,
    c.cand_election_yr                                       AS election_year,

    CASE c.cand_office
        WHEN 'S' THEN 'U.S. Senator'
        WHEN 'H' THEN 'U.S. Representative'
        WHEN 'P' THEN 'U.S. President'
        ELSE c.cand_office
    END                                                      AS office_label,

    -- Score from v_entity_fraud_risk; defaults to 0 when no signals fire.
    COALESCE(r.risk_score,        0)::NUMERIC                AS risk_score,
    COALESCE(r.n_signals_fired,   0)                         AS n_signals_fired,
    COALESCE(r.signals_fired,     ARRAY[]::TEXT[])           AS signals_fired,
    COALESCE(r.max_severity,      0)                         AS max_severity,
    r.last_observation_at                                    AS last_observation_at
FROM       raw.fec_candidate c
LEFT JOIN  derived.v_entity_fraud_risk r
       ON  r.entity_kind = 'candidate'
       AND r.cycle       = c.cycle
       AND r.entity_id   = c.cand_id
WHERE      c.cand_office_st = 'NJ'
   AND     c.cand_office IN ('S', 'H')
   AND     c.cand_ici          = 'I'
   AND     c.cand_status       = 'C'
ORDER BY
   -- Senators first (S sorts after H lexicographically, so DESC).
   c.cand_office DESC,
   c.cand_office_district NULLS FIRST;

COMMENT ON VIEW derived.v_nj_federal_officials IS
    'Curated NJ federal incumbent roster for /risk overview Section 1. '
    'Filters raw.fec_candidate to (cand_office_st=NJ, cand_office IN (S,H), '
    'cand_ici=I, cand_status=C). Joins to v_entity_fraud_risk for score + '
    'signal count. Substrate-honest scope: federal seats only -- NJ governor '
    'and state legislature live at NJ ELEC, scoped to the F8.5 ingester.';


COMMIT;
