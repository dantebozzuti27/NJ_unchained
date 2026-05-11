-- ============================================================================
-- Seed: 021_fraud_signal_strict_address_extension
--
-- Extends the three fraud-signal reference tables (seeded by 018/019/020)
-- with a single new row each for the entity_on_leie_strict_address signal
-- introduced in migration 092.
--
-- WHY A SEPARATE SEED FILE (and not edits to 018/019/020):
--   * 018/019/020 are already applied + checksum-recorded in production
--     governance.schema_migrations. Editing them would force a checksum
--     mismatch on every fresh deploy (the platform's migrate-script
--     refuses to silently re-apply a changed seed -- see
--     scripts/migrate.py drift-detection).
--   * Each new signal that lands in fraud_signal_config needs matching
--     rows in human_explanation + severity_calibration + evidence_url_template
--     so the evidence-card UI on /risk/[kind]/[id] does not render with
--     missing pieces. Adding all three INSERTs in a single seed
--     file co-locates the substrate so a single new-signal migration
--     can be reviewed as one diff.
--
-- IDEMPOTENT VIA ON CONFLICT DO UPDATE on every INSERT (mirroring the
-- 018/019/020 pattern). Re-running this seed is a no-op against the
-- existing rows.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. formula_version row stamping (MUST come first -- the other three
--    reference tables FK to ref.formula_version, so the row must exist
--    before any seed INSERT that references it).
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version (
    formula_version,
    description,
    effective_date,
    notes
) VALUES (
    '2.3.0-fraud-strict-address-v1',
    'Pillar 2 (civic integrity) Phase F5b-strict. Adds the '
    'entity_on_leie_strict_address signal -- a strict-evidence variant '
    'of entity_on_leie that requires canonical LAST|FIRST name AND '
    'city + 5-digit ZIP overlap with the LEIE individual record. '
    'Lifts peer_percentile from ~0.945 (name-only) to ~0.999 '
    '(name+address) for strict matches, driving non-zero risk_score '
    'contribution under the tail-only fraud_risk_score formula '
    '(phi = sev * max(0, p - 0.95)^2). Severity unchanged at 5 '
    '(consequence-tier preserved; evidence strength surfaced via '
    'percentile). Co-located with seed 021 (this file) and migration '
    '092.',
    '2026-05-10',
    'Stacks on 2.2.0-fraud-evidence-view-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 2. Human explanation: what the rule is, federal authority, plain-English
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
    'entity_on_leie_strict_address',
    'A federal candidate or treasurer canonical name matches an active '
    'LEIE individual exclusion AND the entity''s mailing address (city + '
    '5-digit ZIP) overlaps the LEIE individual''s registered address. '
    'LEIE is the HHS-OIG List of Excluded Individuals/Entities, maintained '
    'under the Mandatory Exclusion Authority of 42 USC 1320a-7(a). The '
    'address anchor strengthens the identity-match evidence beyond the '
    'name-only entity_on_leie signal: requiring city + ZIP overlap '
    'collapses the false-positive base rate from ~5% of all FEC entities '
    '(name-only matches) to ~0.05% (name + address matches), making '
    'each strict match a high-evidence structural anomaly worthy of '
    'analyst review of the underlying identity claim.',
    'HHS-OIG',
    '42 USC 1320a-7(a)',
    'https://oig.hhs.gov/exclusions/authorities.asp',
    'Entity {{entity_id}} ({{entity_kind}}) canonical name AND mailing '
    'address (city + ZIP) match an active LEIE individual exclusion. '
    'This is a strict-evidence match -- the name-only false-positive '
    'rate (~5% of FEC entities have last+first name overlap with LEIE) '
    'collapses to ~0.05% when address is also required. The match is '
    'NOT a finding of campaign-finance violation -- LEIE is a healthcare-'
    'program exclusion list, not a campaign-finance prohibition -- but '
    'the strict structural overlap supports analyst review of the '
    'underlying identity claim and the entity''s contribution sources.',
    '2.3.0-fraud-strict-address-v1',
    '2026-05-10'
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
-- 3. Severity calibration: precedent basis for severity_level=5
--
-- Same severity tier as entity_on_leie (consequence-tier is unchanged --
-- the federal-sanction overlap remains the same potential harm). The
-- precedent basis is oig_report because LEIE is itself a published
-- federal enforcement finding. The strict-vs-loose distinction is
-- expressed via peer_percentile (evidence strength), not severity.
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
    'entity_on_leie_strict_address',
    5,
    'oig_report',
    'https://oig.hhs.gov/exclusions/files/sai_supplement_archive.asp',
    'Same consequence-tier as entity_on_leie (severity 5 / CRITICAL) '
    'because LEIE listings carry their own federal-finding weight under '
    '42 USC 1320a-7(a). The strict variant tightens evidence STRENGTH '
    '(rate-based percentile lifts from ~0.945 name-only to ~0.999 name+'
    'address), not consequence severity. Substrate-honest orthogonality: '
    'severity captures "if true, how bad"; peer_percentile captures '
    '"how likely is it true". Both signals can fire on the same entity; '
    'the analyst reads them as layered evidence (loose name match + '
    'strict name+address match = compound corroboration of identity).',
    '2.3.0-fraud-strict-address-v1',
    '2026-05-10'
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
-- 4. Evidence URL template: upstream-verify button on the evidence card
--
-- LEIE's online search at https://exclusions.oig.hhs.gov accepts a GET-
-- shaped URL that pre-fills the search form via query params, but only
-- if the analyst clicks through the form -- the underlying search is
-- POST-shaped. So we link to the OIG LEIE landing page (same as the
-- loose variant) and trust the analyst to type the name shown on the
-- evidence card. v2 can add a deep-link via the OIG public API.
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_evidence_url_template (
    signal_id,
    url_template,
    button_label,
    upstream_source,
    formula_version,
    effective_date
) VALUES (
    'entity_on_leie_strict_address',
    'https://oig.hhs.gov/exclusions/exclusions_list.asp',
    'Verify on OIG.gov',
    'OIG.gov',
    '2.3.0-fraud-strict-address-v1',
    '2026-05-10'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();


