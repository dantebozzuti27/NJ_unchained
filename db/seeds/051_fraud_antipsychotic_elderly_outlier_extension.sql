-- ============================================================================
-- Seed: 051_fraud_antipsychotic_elderly_outlier_extension
--
-- Companion to migration 115 (which ships the platform_constants, the
-- refresher, the fraud_signal_config row, and the master-refresher wiring).
-- This seed ships the evidence-card reference rows + the reportability-channel
-- mapping for the new signal:
--
--   * ref.fraud_signal_human_explanation      (rule_text + plain-English)
--   * ref.fraud_signal_severity_calibration   (severity_level + precedent)
--   * ref.fraud_signal_evidence_url_template  (upstream-verify button)
--   * ref.fraud_reportability_channel         (lead-ranking channel/tier)
--
-- Mirrors seed 044 (opioid) in shape. IDEMPOTENT via ON CONFLICT DO UPDATE.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. formula_version (FK anchor; fresh-deploy ordering safety net).
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version (
    formula_version, description, effective_date, notes
) VALUES (
    '3.3.0-fraud-antipsychotic-elderly-outlier-v1',
    'Pillar 2 (civic integrity) FRAUD-F8 prospective detector. Adds '
    'antipsychotic_elderly_outlier: a CMS Part D prescriber in the top 1% of '
    'its specialty peer group on the share of elderly (>=65) beneficiaries '
    'receiving antipsychotics. Nursing-home chemical-restraint lead, flagged '
    'pre-enforcement. cms_utilization family; severity 4, basis '
    'empirical_pctile.',
    '2026-06-09',
    'Stacks on 3.2.0-cms-partd-behavioral-columns-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 2. Human explanation
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_human_explanation (
    signal_id, rule_text, citation_authority, citation_section, citation_url,
    plain_english_template, formula_version, effective_date
) VALUES (
    'antipsychotic_elderly_outlier',
    'A CMS Medicare Part D prescriber whose share of elderly (>=65) '
    'beneficiaries receiving an antipsychotic (Antpsyct_GE65_Tot_Benes / '
    'GE65_Tot_Benes) sits in the extreme upper tail -- top 1% -- of its OWN '
    'specialty peer group for the data year, subject to a minimum elderly '
    'beneficiary count and a minimum specialty-peer count. The ranking is '
    'specialty-relative (CUME_DIST partitioned by provider type) because '
    'psychiatrists legitimately prescribe antipsychotics far more than '
    'internists. Antipsychotic over-prescribing to the elderly -- especially '
    'dementia patients -- is a recognized harm and fraud pattern ("chemical '
    'restraint" and medically-unnecessary billing); CMS runs the National '
    'Partnership to Improve Dementia Care to reduce it and the FDA places a '
    'boxed warning on these drugs in elderly dementia patients. This is a '
    'statistical lead, not an adjudicated violation.',
    'platform',
    'Empirical specialty-relative elderly-antipsychotic outlier (platform '
    'methodology; tuning constants in ref.platform_constants). Context: CMS '
    'National Partnership to Improve Dementia Care; FDA boxed warning.',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'Provider NPI {{entity_id}} is in the extreme upper tail of elderly '
    'antipsychotic prescribing for its specialty in {{cycle}}: {{raw_value}}% '
    'of its elderly (>=65) beneficiaries received an antipsychotic, placing it '
    'at the {{peer_percentile}} percentile within its specialty peer group '
    '({{peer_bucket}}). Antipsychotic over-prescribing to the elderly is a '
    'known "chemical restraint" / medically-unnecessary-billing pattern, but '
    'this is a statistical lead, not proof -- some psychiatric practices '
    'legitimately land in the tail. Verify on the CMS Part D Prescribers data '
    'before any finding.',
    '3.3.0-fraud-antipsychotic-elderly-outlier-v1',
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
-- 3. Severity calibration: severity 4 (HIGH lead), empirical_pctile
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_severity_calibration (
    signal_id, severity_level, calibration_basis, precedent_url,
    precedent_summary, formula_version, effective_date
) VALUES (
    'antipsychotic_elderly_outlier',
    4,
    'empirical_pctile',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'Severity 4 (HIGH). Extreme antipsychotic prescribing to the elderly '
    'relative to a prescriber''s own specialty is a recognized chemical-'
    'restraint / medically-unnecessary-prescribing indicator and routes to '
    'analyst review. One tier below the exact-match exclusion signals '
    '(severity 5) because it is a distributional lead, not an adjudicated list '
    'match: legitimate psychiatric practices can land in the tail. The '
    'empirical_pctile basis reflects a versioned platform calibration '
    '(ref.platform_constants), not a federal-enforcement precedent.',
    '3.3.0-fraud-antipsychotic-elderly-outlier-v1',
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
    signal_id, url_template, button_label, upstream_source,
    formula_version, effective_date
) VALUES (
    'antipsychotic_elderly_outlier',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers',
    'View CMS Part D prescriber data',
    'CMS.gov',
    '3.3.0-fraud-antipsychotic-elderly-outlier-v1',
    '2026-06-09'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();


-- ----------------------------------------------------------------------------
-- 5. Reportability channel: HHS-OIG/CMS referral, no statutory bounty (tier 3)
--    Same lane as the opioid / services outliers -- a utilization-anomaly lead,
--    not itself a false claim, so no relator reward attaches.
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_reportability_channel (
    signal_id, recovery_program, recovery_channel, recovery_channel_url,
    statute_citation, statute_url, reward_eligible, relator_share_low,
    relator_share_high, reward_tier, raw_value_is_usd, is_prior_sanction,
    citation_text, formula_version, effective_date
) VALUES (
    'antipsychotic_elderly_outlier',
    'HHS-OIG / CMS referral (no statutory bounty)',
    'HHS-OIG Hotline',
    'https://oig.hhs.gov/fraud/report-fraud/',
    '42 U.S.C. § 1320a-7a (Civil Monetary Penalties Law)',
    'https://www.law.cornell.edu/uscode/text/42/1320a-7a',
    FALSE, NULL, NULL, 3, FALSE, FALSE,
    'An extreme elderly-antipsychotic rate is a chemical-restraint / medically-'
    'unnecessary-prescribing lead routed to HHS-OIG/CMS; it is not by itself a '
    'false claim, so no statutory relator reward attaches. If substantiated as '
    'medically-unnecessary billing it can become a False Claims Act matter, but '
    'the raw outlier is a lead, not a claim.',
    '3.3.0-fraud-antipsychotic-elderly-outlier-v1', '2026-06-09'
)
ON CONFLICT (signal_id) DO UPDATE SET
    recovery_program     = EXCLUDED.recovery_program,
    recovery_channel     = EXCLUDED.recovery_channel,
    recovery_channel_url = EXCLUDED.recovery_channel_url,
    statute_citation     = EXCLUDED.statute_citation,
    statute_url          = EXCLUDED.statute_url,
    reward_eligible      = EXCLUDED.reward_eligible,
    relator_share_low    = EXCLUDED.relator_share_low,
    relator_share_high   = EXCLUDED.relator_share_high,
    reward_tier          = EXCLUDED.reward_tier,
    raw_value_is_usd     = EXCLUDED.raw_value_is_usd,
    is_prior_sanction    = EXCLUDED.is_prior_sanction,
    citation_text        = EXCLUDED.citation_text,
    formula_version      = EXCLUDED.formula_version,
    effective_date       = EXCLUDED.effective_date;
