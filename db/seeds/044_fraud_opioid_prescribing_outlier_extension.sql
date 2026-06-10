-- ============================================================================
-- Seed: 044_fraud_opioid_prescribing_outlier_extension
--
-- Companion to migration 106 (mig 106 ships the platform_constants, the
-- signal_family + upstream_source CHECK widenings, the refresher, the
-- fraud_signal_config row, and the master-refresher wiring). This seed
-- ships the THREE evidence-card reference rows:
--
--   * ref.fraud_signal_human_explanation     (rule_text + plain-English)
--   * ref.fraud_signal_severity_calibration  (severity_level + precedent)
--   * ref.fraud_signal_evidence_url_template (upstream-verify button)
--
-- Mirrors seed 043 in shape and structure.
--
-- IDEMPOTENT VIA ON CONFLICT DO UPDATE on every INSERT.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. formula_version (FK anchor; mig 106 already INSERT-ON-CONFLICTed this --
--    duplicated here as a fresh-deploy ordering safety net).
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version (
    formula_version,
    description,
    effective_date,
    notes
) VALUES (
    '2.8.6-fraud-opioid-prescribing-outlier-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 Phase-2 signal slice. Adds the '
    'opioid_prescribing_outlier distributional signal: a CMS Part D '
    'prescriber in the top 1% of its specialty peer group on the CMS '
    'opioid-prescribing rate. First cms_utilization family signal. Tuning '
    'constants in ref.platform_constants. Severity 4, basis empirical_pctile.',
    '2026-06-09',
    'Stacks on 2.8.5-fraud-state-excluded-provider-billing-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 2. Human explanation: platform empirical methodology + plain English
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
    'opioid_prescribing_outlier',
    'A CMS Medicare Part D prescriber whose opioid-prescribing rate '
    '(CMS-published Opioid_Prscrbr_Rate, = opioid claims / total claims) '
    'sits in the extreme upper tail -- top 1% -- of its OWN specialty peer '
    'group for the data year, subject to a minimum claim volume and a '
    'minimum specialty-peer count. The ranking is specialty-relative '
    '(CUME_DIST partitioned by provider type) precisely because opioid '
    'prescribing legitimately varies by an order of magnitude across '
    'specialties; a pediatrician in the top 1% of pediatricians is a far '
    'stronger lead than a pain specialist with a high absolute rate. This '
    'is a statistical lead -- a pill-mill / diversion indicator -- not an '
    'adjudicated violation; legitimate high-volume specialists exist.',
    'platform',
    'Empirical specialty-relative opioid-rate outlier (platform '
    'methodology; tuning constants in ref.platform_constants)',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'Provider NPI {{entity_id}} is in the extreme upper tail of opioid '
    'prescribing for its specialty in {{cycle}}: an opioid-prescribing rate '
    'of {{raw_value}}% places it at the {{peer_percentile}} percentile '
    'within its specialty peer group ({{peer_bucket}}). Opioid prescribing '
    'is judged against same-specialty peers, not an absolute rate, so this '
    'flags an unusual prescriber relative to its own field. This is a '
    'statistical lead (possible diversion / pill mill), not proof of '
    'wrongdoing -- legitimate high-volume specialists exist. Verify the '
    'underlying rate on the CMS Part D Prescribers data before any finding.',
    '2.8.6-fraud-opioid-prescribing-outlier-v1',
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


-- ----------------------------------------------------------------------------
-- 3. Severity calibration: severity_level=4 (HIGH lead), empirical_pctile
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
    'opioid_prescribing_outlier',
    4,
    'empirical_pctile',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'Severity 4 (HIGH). Extreme opioid prescribing relative to a '
    'prescriber''s own specialty is a recognized pill-mill / diversion / '
    'patient-harm indicator and routes to analyst review. It is one tier '
    'below the exact-match exclusion signals (severity 5) because it is a '
    'distributional lead, not an adjudicated list match: legitimate '
    'high-volume pain specialists can land in the tail. The empirical_pctile '
    'basis reflects that the cutoff is a versioned platform calibration '
    '(ref.platform_constants), not a federal-enforcement precedent. Severity '
    'captures "if true, how bad"; peer_percentile captures within-specialty '
    'rarity.',
    '2.8.6-fraud-opioid-prescribing-outlier-v1',
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


-- ----------------------------------------------------------------------------
-- 4. Evidence URL template: CMS Part D Prescribers data
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_evidence_url_template (
    signal_id,
    url_template,
    button_label,
    upstream_source,
    formula_version,
    effective_date
) VALUES (
    'opioid_prescribing_outlier',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'View CMS Part D prescriber data',
    'CMS.gov',
    '2.8.6-fraud-opioid-prescribing-outlier-v1',
    '2026-06-09'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();
