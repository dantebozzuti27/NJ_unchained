-- ============================================================================
-- Seed: 023_fraud_nj_state_candidate_on_leie_extension
--
-- Companion to migration 098 (mig 098 ships the canonicalizer + refresher
-- + entity_kind whitelist + master-refresher wiring + signal_config row +
-- v_entity_fraud_evidence widening; this seed ships the THREE reference
-- rows the evidence-card UI needs to render the new signal end-to-end:
--
--   * ref.fraud_signal_human_explanation     (rule_text + plain-English)
--   * ref.fraud_signal_severity_calibration  (severity_level + precedent)
--   * ref.fraud_signal_evidence_url_template (upstream-verify button)
--
-- Mirrors seed 021 (entity_on_leie_strict_address) in shape and structure.
--
-- IDEMPOTENT VIA ON CONFLICT DO UPDATE on every INSERT. Re-running this
-- seed is a no-op against the existing rows.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. formula_version row stamping (MUST come first -- the other three
--    reference tables FK to ref.formula_version, so the row must exist
--    before any seed INSERT that references it). Mig 098 already
--    INSERT-ON-CONFLICTed this same row; the duplicate INSERT here is
--    a defensive idempotency anchor for fresh-deploy ordering.
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version (
    formula_version,
    description,
    effective_date,
    notes
) VALUES (
    '2.7.1-fraud-nj-state-candidate-on-leie-v1',
    'Pillar 2 (civic integrity) Phase F8.5-cross-source. Adds the '
    'nj_state_candidate_on_leie cross-source signal. Mirrors entity_on_leie '
    'in shape and severity (binary, severity=5, rate-based percentile) but '
    'fires against ref.nj_state_candidate -- the manually-curated NJ-state '
    'roster (10 publicly-announced 2025 NJ gubernatorial primary candidates '
    'today). Bucket population is small (~10-50 candidates per election '
    'year), so even one match yields percentile 0.9-0.98 -- meaningful '
    'enough to drive top-N rankings under the tail-only fraud_risk_score '
    'formula. Closes the F8.5 substrate-honesty gap (mig 093 lines 45-52).',
    '2026-05-12',
    'Stacks on 2.7.0-fraud-signal-drift-baseline-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 2. Human explanation: federal authority for the LEIE list, plain English
--    template for the evidence-card body
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_human_explanation (
    signal_id,
    rule_text,
    citation_authority,
    citation_section,
    citation_url,
    plain_english_template,
    formula_version,
    effective_date
) VALUES (
    'nj_state_candidate_on_leie',
    'A publicly-announced NJ statewide / state-legislative candidate '
    '(per ref.nj_state_candidate, the manually-curated NJ-state roster) '
    'has a canonical "LAST|FIRST" name match against an active LEIE '
    'individual exclusion. LEIE is the HHS-OIG List of Excluded '
    'Individuals/Entities, maintained under the Mandatory Exclusion '
    'Authority of 42 USC 1320a-7(a). The match is name-only (no state '
    'or DOB filter, mirroring entity_on_leie''s precision/recall trade) '
    'and severity-5 routes every match to analyst review. The NJ-state '
    'roster bucket is small (~10-50 candidates per election year), so '
    'a single match yields a very high rate-based percentile.',
    'HHS-OIG',
    '42 USC 1320a-7(a)',
    'https://oig.hhs.gov/exclusions/authorities.asp',
    'NJ statewide candidate {{entity_id}} (canonical name from '
    'ref.nj_state_candidate.full_name) matches an active LEIE '
    'individual exclusion. The match is name-only -- LEIE does NOT '
    'publish SSN/EIN by Privacy Act mandate, so identity '
    'verification requires the analyst to cross-reference the OIG '
    'LEIE search portal directly. The match is NOT a finding of '
    'campaign-finance violation -- LEIE is a healthcare-program '
    'exclusion list, not a campaign-finance prohibition -- but it is '
    'a high-priority structural overlap that warrants identity '
    'review. Bucket = NJ state candidates in this election year; '
    'percentile {{peer_percentile}}% reflects rarity within that '
    'small bucket.',
    '2.7.1-fraud-nj-state-candidate-on-leie-v1',
    '2026-05-12'
)
ON CONFLICT (signal_id) DO UPDATE SET
    rule_text              = EXCLUDED.rule_text,
    citation_authority     = EXCLUDED.citation_authority,
    citation_section       = EXCLUDED.citation_section,
    citation_url           = EXCLUDED.citation_url,
    plain_english_template = EXCLUDED.plain_english_template,
    formula_version        = EXCLUDED.formula_version,
    effective_date         = EXCLUDED.effective_date,
    updated_at             = now();


-- ----------------------------------------------------------------------------
-- 3. Severity calibration: severity_level=5, precedent_basis=oig_report
--
-- Same severity-5 (CRITICAL) tier as entity_on_leie -- the consequence-
-- tier (federal-sanction overlap) is identical regardless of which name
-- table the FEC / NJ-state side comes from. The strict-vs-loose-vs-NJ-
-- state distinction is expressed entirely via peer_percentile (evidence
-- strength + rarity within bucket), not severity.
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_severity_calibration (
    signal_id,
    severity_level,
    calibration_basis,
    precedent_url,
    precedent_summary,
    formula_version,
    effective_date
) VALUES (
    'nj_state_candidate_on_leie',
    5,
    'oig_report',
    'https://oig.hhs.gov/exclusions/files/sai_supplement_archive.asp',
    'Same consequence-tier as entity_on_leie (severity 5 / CRITICAL). '
    'LEIE listings carry their own federal-finding weight under 42 USC '
    '1320a-7(a) regardless of which side of the join produced the '
    'name match (FEC candidate vs FEC treasurer vs NJ-state candidate). '
    'Substrate-honest orthogonality preserved: severity captures "if '
    'true, how bad" (federal sanction overlap is grave); peer_percentile '
    'captures "how likely is it true given the candidate population we '
    'compared against." The NJ-state bucket is small enough that the '
    'percentile naturally lands at 0.9-0.98 even for a single match -- '
    'the rarity is bucket-relative, exactly the substrate-honest '
    'percentile semantic.',
    '2.7.1-fraud-nj-state-candidate-on-leie-v1',
    '2026-05-12'
)
ON CONFLICT (signal_id) DO UPDATE SET
    severity_level     = EXCLUDED.severity_level,
    calibration_basis  = EXCLUDED.calibration_basis,
    precedent_url      = EXCLUDED.precedent_url,
    precedent_summary  = EXCLUDED.precedent_summary,
    formula_version    = EXCLUDED.formula_version,
    effective_date     = EXCLUDED.effective_date,
    updated_at         = now();


-- ----------------------------------------------------------------------------
-- 4. Evidence URL template: same OIG.gov LEIE search landing page as the
--    loose entity_on_leie variant. The OIG portal's deep-link is POST-
--    shaped (requires session token), so we cannot pre-fill a name into
--    the URL. The button label is "Search OIG LEIE" matching the loose
--    variant's convention so the analyst sees a consistent verification
--    affordance across all LEIE-bearing signals.
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_evidence_url_template (
    signal_id,
    url_template,
    button_label,
    upstream_source,
    formula_version,
    effective_date
) VALUES (
    'nj_state_candidate_on_leie',
    'https://oig.hhs.gov/exclusions/exclusions_list.asp',
    'Search OIG LEIE',
    'OIG.gov',
    '2.7.1-fraud-nj-state-candidate-on-leie-v1',
    '2026-05-12'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();
