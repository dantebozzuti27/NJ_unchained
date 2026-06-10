-- ============================================================================
-- Seed: 048_fraud_name_resolved_excluded_provider_billing_extension
--
-- Companion to migration 110. Ships the THREE evidence-card reference rows
-- for name_resolved_excluded_provider_billing (the NPPES identity-recall
-- companion to provider_excluded_billing):
--   * ref.fraud_signal_human_explanation
--   * ref.fraud_signal_severity_calibration   (severity 3, inferred_identity)
--   * ref.fraud_signal_evidence_url_template
-- The plain-English template MUST surface that identity is name+state
-- inferred (lower confidence) so an analyst verifies before acting.
-- IDEMPOTENT VIA ON CONFLICT DO UPDATE.
-- ============================================================================


INSERT INTO ref.formula_version (
    formula_version, description, effective_date, notes
) VALUES (
    '2.9.0-fraud-name-resolved-excluded-provider-billing-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 Phase-3 identity-spine recall signal. '
    'name_resolved_excluded_provider_billing: a name-only LEIE individual '
    'exclusion resolved via NPPES (unique canonical LAST|FIRST + practice '
    'state) to an NPI present in CMS Medicare billing within the exclusion '
    'window. Inferred identity, severity 3, basis inferred_identity.',
    '2026-06-09',
    'Stacks on 2.8.9-fraud-provider-excluded-billing-partb-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


INSERT INTO ref.fraud_signal_human_explanation (
    signal_id, rule_text, citation_authority, citation_section, citation_url,
    plain_english_template, formula_version, effective_date
) VALUES (
    'name_resolved_excluded_provider_billing',
    'A provider on the HHS-OIG List of Excluded Individuals/Entities (LEIE) '
    'whose exclusion record carries NO usable NPI, but whose canonical name '
    '(LAST|FIRST) and state resolve -- UNIQUELY -- to a single NPPES-registered '
    'provider, and that resolved NPI appears in CMS Medicare billing (Part D '
    'or Part B) for a year in which the exclusion was already in effect. The '
    'NPPES resolution is required to be unique: if more than one provider '
    'shares the name in that state, no match is emitted. Because identity is '
    'inferred from name + state rather than an exact NPI on the exclusion '
    'list, this is a MODERATE-confidence lead (severity 3), strictly weaker '
    'than the exact-NPI provider_excluded_billing signals. If confirmed, the '
    'federal payment prohibition under 42 USC 1320a-7a applies.',
    'HHS-OIG',
    '42 USC 1320a-7a; OIG SAB on the Effect of Exclusion (2013); identity '
    'resolved via NPPES (CMS NPI Registry)',
    'https://oig.hhs.gov/exclusions/effects_of_exclusion.asp',
    'Provider NPI {{entity_id}} was matched to an HHS-OIG LEIE exclusion that '
    'lists a name but NO NPI: the excluded person''s name and state resolve '
    'UNIQUELY to this NPPES-registered NPI, which appears in CMS Medicare '
    'billing for {{cycle}} with combined Medicare exposure of ${{raw_value}}. '
    'This is an INFERRED (name + state) identity match, not an exact NPI '
    'match -- it is a moderate-confidence lead, not proof. Bucket = all CMS '
    'billers in {{cycle}}; percentile {{peer_percentile}}% reflects rarity. '
    'Confirm the name, state, and NPI on the OIG LEIE portal and NPPES before '
    'any finding; if confirmed, federal program payment to an excluded '
    'provider is prohibited.',
    '2.9.0-fraud-name-resolved-excluded-provider-billing-v1',
    '2026-06-09'
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


INSERT INTO ref.fraud_signal_severity_calibration (
    signal_id, severity_level, calibration_basis, precedent_url,
    precedent_summary, formula_version, effective_date
) VALUES (
    'name_resolved_excluded_provider_billing',
    3,
    'inferred_identity',
    'https://oig.hhs.gov/exclusions/effects_of_exclusion.asp',
    'Severity 3 (MODERATE). The underlying conduct -- an excluded provider '
    'billing Medicare -- is identical to the severity-5 exact-NPI signals, '
    'but here IDENTITY is inferred from a unique name + state resolution via '
    'NPPES rather than an exact NPI on the LEIE row. The two-notch reduction '
    'from 5 encodes that inference risk (a unique name+state match is strong '
    'but not certain). The calibration_basis is inferred_identity -- an '
    'honest, distinct category (the verifiable-data invariant forbids '
    'labeling an inferred match with the exact-match oig_report basis). '
    'Severity captures "if true, how bad"; the inference caveat is carried in '
    'the explanation so analysts verify before acting.',
    '2.9.0-fraud-name-resolved-excluded-provider-billing-v1',
    '2026-06-09'
)
ON CONFLICT (signal_id) DO UPDATE SET
    severity_level     = EXCLUDED.severity_level,
    calibration_basis  = EXCLUDED.calibration_basis,
    precedent_url      = EXCLUDED.precedent_url,
    precedent_summary  = EXCLUDED.precedent_summary,
    formula_version    = EXCLUDED.formula_version,
    effective_date     = EXCLUDED.effective_date,
    updated_at         = now();


INSERT INTO ref.fraud_signal_evidence_url_template (
    signal_id, url_template, button_label, upstream_source,
    formula_version, effective_date
) VALUES (
    'name_resolved_excluded_provider_billing',
    'https://exclusions.oig.hhs.gov/',
    'Search OIG LEIE',
    'OIG.gov',
    '2.9.0-fraud-name-resolved-excluded-provider-billing-v1',
    '2026-06-09'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();
