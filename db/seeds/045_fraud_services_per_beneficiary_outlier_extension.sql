-- ============================================================================
-- Seed: 045_fraud_services_per_beneficiary_outlier_extension
--
-- Companion to migration 107. Ships the THREE evidence-card reference rows
-- for services_per_beneficiary_outlier:
--   * ref.fraud_signal_human_explanation
--   * ref.fraud_signal_severity_calibration
--   * ref.fraud_signal_evidence_url_template
-- Mirrors seed 044 in shape. IDEMPOTENT VIA ON CONFLICT DO UPDATE.
-- ============================================================================


INSERT INTO ref.formula_version (
    formula_version, description, effective_date, notes
) VALUES (
    '2.8.7-fraud-services-per-beneficiary-outlier-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 Phase-2 signal slice. Adds the '
    'services_per_beneficiary_outlier distributional signal: a CMS Part B '
    'practitioner in the top 1% of its specialty peer group on '
    'Tot_Srvcs/Tot_Benes. cms_utilization family. Tuning constants in '
    'ref.platform_constants. Severity 4, basis empirical_pctile.',
    '2026-06-09',
    'Stacks on 2.8.6-fraud-opioid-prescribing-outlier-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


INSERT INTO ref.fraud_signal_human_explanation (
    signal_id, rule_text, citation_authority, citation_section, citation_url,
    plain_english_template, formula_version, effective_date
) VALUES (
    'services_per_beneficiary_outlier',
    'A CMS Medicare Part B practitioner whose services-per-beneficiary ratio '
    '(Tot_Srvcs / Tot_Benes) sits in the extreme upper tail -- top 1% -- of '
    'its OWN specialty peer group for the data year, subject to a minimum '
    'beneficiary count and a minimum specialty-peer count. Ranking is '
    'specialty-relative (CUME_DIST partitioned by provider type) because '
    'service intensity varies by an order of magnitude across specialties. An '
    'abnormally high number of billed services per distinct patient is a '
    'classic overutilization / phantom-billing / churning indicator. This is '
    'a statistical lead, not an adjudicated violation; legitimate '
    'high-intensity specialties exist.',
    'platform',
    'Empirical specialty-relative services-per-beneficiary outlier (platform '
    'methodology; tuning constants in ref.platform_constants)',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'Provider NPI {{entity_id}} is in the extreme upper tail of '
    'services-per-beneficiary for its specialty in {{cycle}}: a ratio of '
    '{{raw_value}} places it at the {{peer_percentile}} percentile within its '
    'specialty peer group ({{peer_bucket}}). Service intensity is judged '
    'against same-specialty peers, not an absolute count, so this flags an '
    'unusually high-volume-per-patient biller relative to its own field. This '
    'is a statistical lead (possible overutilization / phantom billing), not '
    'proof of wrongdoing. Verify the underlying counts on the CMS Physician & '
    'Other Practitioners data before any finding.',
    '2.8.7-fraud-services-per-beneficiary-outlier-v1',
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
    'services_per_beneficiary_outlier',
    4,
    'empirical_pctile',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'Severity 4 (HIGH). Extreme services-per-beneficiary relative to a '
    'practitioner''s own specialty is a recognized overutilization / '
    'phantom-billing indicator and routes to analyst review. One tier below '
    'the exact-match exclusion signals (severity 5) because it is a '
    'distributional lead, not an adjudicated list match: legitimate '
    'high-intensity specialties can land in the tail. The empirical_pctile '
    'basis reflects that the cutoff is a versioned platform calibration '
    '(ref.platform_constants), not a federal-enforcement precedent. Severity '
    'captures "if true, how bad"; peer_percentile captures within-specialty '
    'rarity.',
    '2.8.7-fraud-services-per-beneficiary-outlier-v1',
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
    'services_per_beneficiary_outlier',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'View CMS Part B practitioner data',
    'CMS.gov',
    '2.8.7-fraud-services-per-beneficiary-outlier-v1',
    '2026-06-09'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();
