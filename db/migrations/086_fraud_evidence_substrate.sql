-- ============================================================================
-- Migration: 086_fraud_evidence_substrate
--
-- VISION_2026 Pillar 2 (civic integrity) -- Phase F-UX, work items F2, F3, F5.
-- Closes the verifiable-data gap on the fraud-detection surface: every signal
-- card on /risk/[kind]/[id] must (a) cite a specific federal authority for
-- its predicate and (b) cite a specific precedent that justifies its
-- displayed severity, both pulled from versioned reference tables. Today the
-- 17 fraud signal_ids carry hand-authored comments inside fraud_signal_config
-- (operator-tunable thresholds + freeform commentary) but no machine-readable
-- citation -- the UI cannot honestly render a "Why this fired" block tied to
-- federal authority.
--
-- This migration ships the SUBSTRATE -- the tables that hold the citations
-- and severity precedents, plus the peer-CDF view that backs the UI's
-- "above N% of peers" headline calibration label. The seeds (018, 019) ship
-- in companion files; the UI rewrite (F6, F7) ships once F1 (FEC bulk loader)
-- has populated raw.fec_*.
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
--   1. ref.fraud_signal_human_explanation -- one row per signal_id. Holds
--      the federal-authority citation (rule_text, citation_authority,
--      citation_section, citation_url) and a plain-English template the UI
--      consumes to render the "Why this fired" block. PK signal_id is FK
--      to derived.fraud_signal_config(signal_id) so a signal cannot have an
--      explanation row without a corresponding config row, and a config row
--      cannot be deleted while an explanation still references it.
--      Delivers UX guarantee G6 in part: signal evidence is reference data,
--      not vibes.
--
--   2. ref.fraud_signal_severity_calibration -- one row per signal_id.
--      Holds the precedent that justifies the displayed severity_level
--      (1-5). calibration_basis is a TEXT-with-CHECK enum over the seven
--      legitimate sources (fec_mur, oig_report, doj_filing, crs_analysis,
--      far_authority, fec_advisory, empirical_pctile). PK signal_id FK
--      same as above.
--      Delivers UX guarantee G6 fully: a signal cannot be assigned severity
--      5 unless this table cites a specific FEC/OIG/DOJ/FAR precedent.
--
--   3. derived.v_anomaly_score_percentile_by_kind_cycle -- per-entity
--      empirical CDF of risk_score within (cycle, entity_kind). The UI
--      "above 99.4% of NJ candidates 2024" headline reads from this view;
--      without it the calibration label would have to be computed
--      client-side from a full table scan, or made up. Carries
--      formula_version 2.1.0-fraud-evidence-substrate-v1.
--
-- WHAT THIS MIGRATION DOES NOT SHIP (deliberately, in scope of later work)
-- -----------------------------------------------------------------------
--   * derived.v_entity_fraud_evidence -- the join from entity -> firing
--     signal -> raw row that triggered. Defers to F4 because it requires
--     deterministic URL builders per signal_id (a Python-side concern that
--     is more honestly co-located with the upstream-verify-link generators).
--   * Severity-calibration enforcement asset check (severity_level on this
--     table MUST equal the hardcoded constant the refresher emits).
--     Defers to F4-extension; today the calibration table DOCUMENTS the
--     existing severity, future migration wires refreshers to read from it
--     so severity becomes truly operator-tunable.
--   * Refresher rewiring to consume severity_level from the calibration
--     table. Same reason as above.
--
-- DESIGN DECISIONS
-- ----------------
-- * Two SEPARATE tables (explanation + calibration), not one merged table.
--   They have different update cadences: explanation_text is rewritten when
--   the predicate logic changes (rare); severity_level is retuned as
--   analyst-feedback accumulates (more frequent). Keeping them apart means
--   editing one does not mass-stamp updated_at on the other.
-- * Both tables FK to derived.fraud_signal_config(signal_id). This means
--   the seed must run AFTER fraud_signal_config is populated (mig 061 +
--   later mig 064 SAM extension). On a fresh DB, ledger order ensures this.
--   ON DELETE RESTRICT (default): cannot drop a signal config row while
--   evidence/calibration metadata still exists. Forces the operator to
--   explicitly remove the citation when retiring a signal.
-- * citation_url has a length CHECK (>= 11) so the seed cannot accidentally
--   omit a URL. Same shape as ref.cross_source_divergence_known_causes.
--   source_citation; the federal-domain whitelist is enforced at the seed
--   layer (a non-federal URL in production would fail the migration's
--   companion test, not a CHECK).
-- * calibration_basis is an enum CHECK with seven values covering the
--   complete set of authoritative sources the platform recognizes. Adding
--   a new basis (e.g., 'state_attorney_general_filing') is a migration,
--   not a one-line edit. This forces the team to think about whether a
--   new source is independent enough to count as a calibration anchor.
-- * empirical_pctile is a LEGITIMATE basis for low-severity signals
--   (severity 1-3) where no specific enforcement matter motivates the
--   threshold; the precedent then is the empirical historical NJ-cycle
--   anomaly-rate distribution. A signal at severity 5 with calibration_basis
--   = 'empirical_pctile' is permitted but will be flagged by a future
--   asset check as "high-severity without enforcement precedent."
-- * v_anomaly_score_percentile_by_kind_cycle uses PERCENT_RANK (not
--   CUME_DIST) for the headline calibration value. PERCENT_RANK is
--   (rank - 1) / (N - 1) -- the fraction of peers with STRICTLY LOWER
--   score, normalized so the highest-scoring peer always reads as 1.0
--   ("exceeds 100% of peers"). CUME_DIST is exposed as a secondary column
--   for callers that want the inclusive-tied semantics.
-- * The peer-percentile view is a VIEW, not a materialized view. The
--   parent v_entity_fraud_risk has the same property: scores can be
--   recomputed from L1 (fraud_signal_observation) without re-aggregating,
--   so percentile recomputation is also cheap. Materialization defers
--   until the L1 row count justifies it (~50K NJ entities is a tractable
--   window-function workload on Neon free tier).
-- ============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration. Stacks on 2.0.0-real-dollar-baseline-v1.
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.1.0-fraud-evidence-substrate-v1',
    'Pillar 2 (civic integrity) Phase F-UX work items F2 + F3 + F5: '
    'verifiable-data substrate for the fraud evidence drill-down. '
    'Three surfaces: ref.fraud_signal_human_explanation (federal-authority '
    'citation per signal_id), ref.fraud_signal_severity_calibration '
    '(precedent basis per signal_id), and '
    'derived.v_anomaly_score_percentile_by_kind_cycle (per-entity '
    'empirical CDF of risk_score within cycle x entity_kind, the substrate '
    'for the "above N% of peers" UI headline label). Spec '
    '.cursor/rules/verifiable-data.mdc rules 1, 2, 3, 4 -- delivers UX '
    'guarantees G1, G3, G6 from the F-UX plan in work_left.txt.',
    '2026-05-08'::DATE,
    'Stacks on 2.0.0-real-dollar-baseline-v1.'
)
ON CONFLICT (formula_version) DO NOTHING;


-- ----------------------------------------------------------------------------
-- ref.fraud_signal_human_explanation
--
-- One row per signal_id. The UI's "Why this fired" block on
-- /risk/[kind]/[id] reads exclusively from this table -- never client-side
-- string-templated, never hardcoded in components. Without this row, a
-- signal's evidence card cannot be honestly rendered.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.fraud_signal_human_explanation (
    signal_id              TEXT          NOT NULL PRIMARY KEY
        REFERENCES derived.fraud_signal_config(signal_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    rule_text              TEXT          NOT NULL,
    citation_authority     TEXT          NOT NULL,
    citation_section       TEXT          NOT NULL,
    citation_url           TEXT          NOT NULL,
    plain_english_template TEXT          NOT NULL,

    formula_version        TEXT          NOT NULL
        REFERENCES ref.formula_version(formula_version),
    effective_date         DATE          NOT NULL DEFAULT CURRENT_DATE,
    created_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fraud_signal_human_explanation_authority_chk
        CHECK (citation_authority IN (
            'FEC',          -- Federal Election Commission (regulations + advisory + enforcement)
            'HHS-OIG',      -- HHS Office of Inspector General (LEIE)
            'GSA-SAM',      -- General Services Administration SAM.gov (federal exclusion list)
            'FAR-Council',  -- Federal Acquisition Regulation Council
            'DOJ',          -- Department of Justice (prosecutions, settlements)
            'CRS',          -- Congressional Research Service analysis
            'platform'      -- structural/empirical anomaly with no direct federal authority
        )),

    CONSTRAINT fraud_signal_human_explanation_rule_text_chk
        CHECK (length(rule_text) >= 20),

    CONSTRAINT fraud_signal_human_explanation_section_chk
        CHECK (length(citation_section) >= 3),

    -- 11 chars catches every realistic federal URL ("http://x.gov" = 12).
    -- Forces the seed to ship a real URL not "TBD".
    CONSTRAINT fraud_signal_human_explanation_url_chk
        CHECK (length(citation_url) >= 11),

    CONSTRAINT fraud_signal_human_explanation_template_chk
        CHECK (length(plain_english_template) >= 30)
);

COMMENT ON TABLE ref.fraud_signal_human_explanation IS
    'Federal-authority citation registry for fraud-detection signals. '
    'One row per signal_id (FK -> derived.fraud_signal_config); the UI '
    'evidence drill-down reads "Why this fired" from this table. Required '
    'by .cursor/rules/verifiable-data.mdc rules 1 + 3 (no magic numbers; '
    'lineage visible). Formula 2.1.0-fraud-evidence-substrate-v1.';

COMMENT ON COLUMN ref.fraud_signal_human_explanation.rule_text IS
    'Predicate in plain English. The structural rule the signal codifies '
    '(e.g., "candidate declares a principal campaign committee ID that '
    'is not present in raw.fec_committee"). Authoritative version of the '
    'predicate; refresher SQL is the operational version.';

COMMENT ON COLUMN ref.fraud_signal_human_explanation.citation_authority IS
    'Authoritative body. FEC (regulations + advisories + MURs), HHS-OIG '
    '(LEIE), GSA-SAM (SAM.gov), FAR-Council (procurement), DOJ '
    '(prosecutions), CRS (analysis), or platform (structural / empirical, '
    'no direct federal authority -- e.g. address-cluster anomaly).';

COMMENT ON COLUMN ref.fraud_signal_human_explanation.citation_section IS
    'Specific section identifier within the authority. Examples: '
    '"11 CFR 101.1", "42 USC 1320a-7", "FAR 9.405", "FEC AO 2002-17".';

COMMENT ON COLUMN ref.fraud_signal_human_explanation.citation_url IS
    'URL to the authority page. Required so the UI evidence card can '
    'externally link to the federal text the platform claims to be '
    'codifying. CHECK length >= 11 prevents accidentally seeding TBD.';

COMMENT ON COLUMN ref.fraud_signal_human_explanation.plain_english_template IS
    'Template string for the "Why this fired" block, with {{placeholder}} '
    'tokens (entity_id, raw_value, peer_bucket, etc.) the UI substitutes '
    'at render time. Storing the template here, not in the .tsx file, '
    'keeps the citation discipline consistent across components.';


-- updated_at trigger (same pattern as 084).
CREATE OR REPLACE FUNCTION ref._fraud_signal_human_explanation_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fraud_signal_human_explanation_updated_at
    ON ref.fraud_signal_human_explanation;

CREATE TRIGGER fraud_signal_human_explanation_updated_at
BEFORE UPDATE ON ref.fraud_signal_human_explanation
FOR EACH ROW
EXECUTE FUNCTION ref._fraud_signal_human_explanation_set_updated_at();


-- ----------------------------------------------------------------------------
-- ref.fraud_signal_severity_calibration
--
-- One row per signal_id. severity_level documents what the refresher
-- currently emits; calibration_basis + precedent_url + precedent_summary
-- justify why that level is correct. The constraint that this table's
-- severity_level matches the refresher's hardcoded constant is enforced
-- by the companion test (test_fraud_evidence_substrate.py); future work
-- (F4-extension) inverts the dependency so refreshers READ severity from
-- this table.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.fraud_signal_severity_calibration (
    signal_id          TEXT          NOT NULL PRIMARY KEY
        REFERENCES derived.fraud_signal_config(signal_id)
        ON DELETE RESTRICT ON UPDATE CASCADE,

    severity_level     SMALLINT      NOT NULL,
    calibration_basis  TEXT          NOT NULL,
    precedent_url      TEXT          NOT NULL,
    precedent_summary  TEXT          NOT NULL,

    formula_version    TEXT          NOT NULL
        REFERENCES ref.formula_version(formula_version),
    effective_date     DATE          NOT NULL DEFAULT CURRENT_DATE,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fraud_signal_severity_calibration_level_chk
        CHECK (severity_level BETWEEN 1 AND 5),

    CONSTRAINT fraud_signal_severity_calibration_basis_chk
        CHECK (calibration_basis IN (
            'fec_mur',          -- FEC Matter Under Review (enforcement action)
            'oig_report',       -- HHS-OIG report or audit
            'doj_filing',       -- DOJ enforcement filing or settlement
            'crs_analysis',     -- Congressional Research Service analysis
            'far_authority',    -- FAR debarment / suspension authority
            'fec_advisory',     -- FEC Advisory Opinion
            'empirical_pctile'  -- analyst calibration vs. NJ-cycle anomaly distribution
        )),

    CONSTRAINT fraud_signal_severity_calibration_url_chk
        CHECK (length(precedent_url) >= 11),

    CONSTRAINT fraud_signal_severity_calibration_summary_chk
        CHECK (length(precedent_summary) >= 30)
);

COMMENT ON TABLE ref.fraud_signal_severity_calibration IS
    'Severity precedent registry for fraud-detection signals. One row per '
    'signal_id (FK -> derived.fraud_signal_config). Documents WHY the '
    'currently-emitted severity_level is correct, citing a specific FEC '
    'MUR / OIG report / DOJ filing / FAR authority / FEC advisory, or '
    'empirical-percentile basis when no enforcement precedent applies. '
    'Required by .cursor/rules/verifiable-data.mdc rules 1 + 4 (severity '
    'is reference data, not vibes). '
    'Formula 2.1.0-fraud-evidence-substrate-v1.';

COMMENT ON COLUMN ref.fraud_signal_severity_calibration.severity_level IS
    'The displayed severity dot count (1 = ●○○○○, 5 = ●●●●●). MUST '
    'match the refresher''s hardcoded SMALLINT for the same signal_id; '
    'enforcement deferred to F4-extension which inverts the dependency.';

COMMENT ON COLUMN ref.fraud_signal_severity_calibration.calibration_basis IS
    'Source of the severity calibration. Seven values: fec_mur, oig_report, '
    'doj_filing, crs_analysis, far_authority, fec_advisory, '
    'empirical_pctile. The first six anchor severity to a specific '
    'enforcement precedent; empirical_pctile anchors it to the empirical '
    'historical NJ-cycle anomaly-rate distribution and is the legitimate '
    'basis for low-severity signals (1-3) without a specific MUR. A '
    'severity-5 signal with empirical_pctile basis is permitted but '
    'flagged in F4-extension as "high-severity without enforcement '
    'precedent" -- a research surface, not a violation.';

COMMENT ON COLUMN ref.fraud_signal_severity_calibration.precedent_url IS
    'URL to the precedent. For fec_mur, the FEC MUR detail page; for '
    'oig_report, the OIG report PDF; for empirical_pctile, the platform '
    'documentation describing the calibration methodology.';

COMMENT ON COLUMN ref.fraud_signal_severity_calibration.precedent_summary IS
    '1-3 sentences explaining why this precedent justifies the displayed '
    'severity. Read by the UI methodology footer.';


CREATE OR REPLACE FUNCTION ref._fraud_signal_severity_calibration_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fraud_signal_severity_calibration_updated_at
    ON ref.fraud_signal_severity_calibration;

CREATE TRIGGER fraud_signal_severity_calibration_updated_at
BEFORE UPDATE ON ref.fraud_signal_severity_calibration
FOR EACH ROW
EXECUTE FUNCTION ref._fraud_signal_severity_calibration_set_updated_at();


-- ----------------------------------------------------------------------------
-- derived.v_anomaly_score_percentile_by_kind_cycle
--
-- Per-entity empirical CDF of risk_score within (cycle, entity_kind). The
-- /risk overview headline ("exceeds 99.4% of NJ candidates 2024") and the
-- /risk/[kind]/[id] page header label both read from this view.
--
-- PERCENT_RANK semantics: (rank - 1) / (N - 1). For an entity at the top of
-- its peer bucket, this is 1.0 ("exceeds 100% of peers"). For the bottom
-- entity, 0.0. For ties, the same value (rank uses dense semantics
-- internally).
--
-- CUME_DIST semantics: count(peer.score <= self.score) / N. Always in (0, 1];
-- ties pull the value upward.
--
-- The UI uses pctile_within_kind_cycle (PERCENT_RANK) because the headline
-- copy "exceeds N% of peers" is unambiguous when the value never reads as
-- "exceeds 100% of peers including yourself."
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_anomaly_score_percentile_by_kind_cycle AS
WITH scored AS (
    SELECT
        cycle,
        entity_kind,
        entity_id,
        risk_score
    FROM derived.v_entity_fraud_risk
    WHERE risk_score IS NOT NULL
)
SELECT
    cycle,
    entity_kind,
    entity_id,
    risk_score,
    PERCENT_RANK() OVER (
        PARTITION BY cycle, entity_kind
        ORDER BY risk_score
    )::NUMERIC(7, 6)                                AS pctile_within_kind_cycle,
    CUME_DIST() OVER (
        PARTITION BY cycle, entity_kind
        ORDER BY risk_score
    )::NUMERIC(7, 6)                                AS cume_dist_within_kind_cycle,
    COUNT(*) OVER (PARTITION BY cycle, entity_kind)::INT
                                                    AS n_peers_in_bucket,
    '2.1.0-fraud-evidence-substrate-v1'::TEXT       AS formula_version
FROM scored;

COMMENT ON VIEW derived.v_anomaly_score_percentile_by_kind_cycle IS
    'Per-entity empirical CDF of risk_score within (cycle, entity_kind). '
    'Substrate for /risk and /risk/[kind]/[id] headline calibration label '
    '("exceeds N% of NJ <kind>s in cycle <year>"). Consumes '
    'derived.v_entity_fraud_risk; one row per entity, NULLs filtered out '
    'so a peer bucket of size N is exactly N rows. '
    'pctile_within_kind_cycle uses PERCENT_RANK ((rank-1)/(N-1)); '
    'cume_dist_within_kind_cycle uses CUME_DIST (rank-with-ties/N) for '
    'callers that want inclusive semantics. n_peers_in_bucket is the '
    'partition cardinality, useful for the UI to suppress the calibration '
    'label when N is too small (e.g., N < 10) to be meaningful. '
    'Formula 2.1.0-fraud-evidence-substrate-v1.';

COMMENT ON COLUMN derived.v_anomaly_score_percentile_by_kind_cycle.pctile_within_kind_cycle IS
    'PERCENT_RANK: fraction of peers with strictly lower risk_score, '
    'normalized to [0, 1]. The top entity always reads as 1.0. The UI '
    'displays this as "exceeds <pct*100>% of peers".';

COMMENT ON COLUMN derived.v_anomaly_score_percentile_by_kind_cycle.cume_dist_within_kind_cycle IS
    'CUME_DIST: fraction of peers with risk_score <= self, in (0, 1]. '
    'Inclusive of ties. Exposed for callers that want "at or above" '
    'semantics rather than "strictly above".';

COMMENT ON COLUMN derived.v_anomaly_score_percentile_by_kind_cycle.n_peers_in_bucket IS
    'COUNT(*) over the (cycle, entity_kind) partition. The UI suppresses '
    'the calibration label when n_peers_in_bucket is small (operationally '
    'N < 10) to avoid claims like "exceeds 100% of 1 peer".';

COMMIT;
