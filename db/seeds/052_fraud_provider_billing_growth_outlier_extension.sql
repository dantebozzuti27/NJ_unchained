-- ============================================================================
-- Seed: 052_fraud_provider_billing_growth_outlier_extension
--
-- Companion to migration 118 (which ships the platform_constants, the
-- refresher, the fraud_signal_config row, the signal_family CHECK widening, and
-- the master-refresher wiring). This seed ships the evidence-card reference
-- rows + the reportability-channel mapping for the new signal:
--
--   * ref.fraud_signal_human_explanation      (rule_text + plain-English)
--   * ref.fraud_signal_severity_calibration   (severity_level + precedent)
--   * ref.fraud_signal_evidence_url_template  (upstream-verify button)
--   * ref.fraud_reportability_channel         (lead-ranking channel/tier)
--
-- Mirrors seed 051 (antipsychotic) in shape. IDEMPOTENT via ON CONFLICT DO
-- UPDATE. calibration_basis 'empirical_pctile', citation_authority 'platform',
-- and upstream_source 'CMS.gov' are all already whitelisted (mig 106), so no
-- CHECK widening is needed here.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. formula_version (FK anchor; fresh-deploy ordering safety net).
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version (
    formula_version, description, effective_date, notes
) VALUES (
    '3.6.0-fraud-provider-billing-growth-outlier-v1',
    'Pillar 2 (civic integrity) FRAUD-F8 prospective TEMPORAL detector. Adds '
    'provider_billing_growth_outlier: a CMS Part B practitioner in the top 1% '
    'of its specialty peer group on year-over-year Medicare-paid growth, gated '
    'by a material current-year payment floor and a prior-year denominator '
    'floor. The bust-out / NPI-takeover signature, flagged pre-enforcement. '
    'First cms_temporal family; severity 4, basis empirical_pctile.',
    '2026-06-10',
    'Stacks on 3.3.0-fraud-antipsychotic-elderly-outlier-v1.'
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
    'provider_billing_growth_outlier',
    'A CMS Medicare Part B practitioner whose year-over-year Medicare-paid '
    'amount (Tot_Mdcr_Pymt_Amt for the data year divided by the prior year) '
    'sits in the extreme upper tail -- top 1% -- of its OWN specialty peer '
    'group, subject to a material current-year payment floor and a prior-year '
    'denominator floor. The ranking is specialty-relative (CUME_DIST '
    'partitioned by provider type) because baseline billing growth varies by '
    'specialty. A sudden multi-fold ramp in Medicare billings is the classic '
    '"bust-out" / provider-identity-takeover pattern: an NPI is acquired or '
    'activated, billed aggressively for a short window, then abandoned before '
    'enforcement catches up. This is a statistical lead, not an adjudicated '
    'violation -- a practice can legitimately ramp (new partner, new line of '
    'service, post-pandemic recovery).',
    'platform',
    'Empirical specialty-relative year-over-year billing-growth outlier '
    '(platform methodology; tuning constants in ref.platform_constants). '
    'Context: HHS-OIG / DOJ healthcare-fraud bust-out typology.',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'Provider NPI {{entity_id}} is in the extreme upper tail of year-over-year '
    'Medicare-billing growth for its specialty in {{cycle}}: its Medicare-paid '
    'amount grew {{raw_value}}x over the prior year, placing it at the '
    '{{peer_percentile}} percentile within its specialty peer group '
    '({{peer_bucket}}). A sudden ramp of this magnitude is a recognized '
    'bust-out / NPI-takeover lead, but it is a statistical signal, not proof -- '
    'some practices legitimately ramp. Verify on the CMS Medicare Physician & '
    'Other Practitioners data before any finding.',
    '3.6.0-fraud-provider-billing-growth-outlier-v1',
    '2026-06-10'
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
    'provider_billing_growth_outlier',
    4,
    'empirical_pctile',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'Severity 4 (HIGH). An extreme year-over-year Medicare-billing ramp '
    'relative to a practitioner''s own specialty is a recognized bust-out / '
    'provider-identity-takeover indicator and routes to analyst review. One '
    'tier below the exact-match exclusion signals (severity 5) because it is a '
    'distributional, temporal lead, not an adjudicated list match: legitimate '
    'practice growth can land in the tail. The empirical_pctile basis reflects '
    'a versioned platform calibration (ref.platform_constants), not a '
    'federal-enforcement precedent.',
    '3.6.0-fraud-provider-billing-growth-outlier-v1',
    '2026-06-10'
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
-- 4. Evidence URL template: CMS Medicare Physician & Other Practitioners data
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_evidence_url_template (
    signal_id, url_template, button_label, upstream_source,
    formula_version, effective_date
) VALUES (
    'provider_billing_growth_outlier',
    'https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners',
    'View CMS Part B practitioner data',
    'CMS.gov',
    '3.6.0-fraud-provider-billing-growth-outlier-v1',
    '2026-06-10'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();


-- ----------------------------------------------------------------------------
-- 5. Reportability channel: HHS-OIG/CMS referral, no statutory bounty (tier 3).
--    A billing-dynamics anomaly is a utilization lead, not itself a false
--    claim, so no relator reward attaches. raw_value is a growth RATIO (not
--    USD), and the signal does not imply a prior sanction.
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_reportability_channel (
    signal_id, recovery_program, recovery_channel, recovery_channel_url,
    statute_citation, statute_url, reward_eligible, relator_share_low,
    relator_share_high, reward_tier, raw_value_is_usd, is_prior_sanction,
    citation_text, formula_version, effective_date
) VALUES (
    'provider_billing_growth_outlier',
    'HHS-OIG / CMS referral (no statutory bounty)',
    'HHS-OIG Hotline',
    'https://oig.hhs.gov/fraud/report-fraud/',
    '42 U.S.C. § 1320a-7a (Civil Monetary Penalties Law)',
    'https://www.law.cornell.edu/uscode/text/42/1320a-7a',
    FALSE, NULL, NULL, 3, FALSE, FALSE,
    'An extreme year-over-year Medicare-billing ramp is a bust-out / '
    'overutilization lead routed to HHS-OIG/CMS; it is not by itself a false '
    'claim, so no statutory relator reward attaches. If substantiated as '
    'medically-unnecessary or phantom billing it can become a False Claims Act '
    'matter, but the raw growth outlier is a lead, not a claim. raw_value is a '
    'growth ratio (not USD), so the lead ranking treats it as a corroborating '
    'count signal, not a dollar exposure.',
    '3.6.0-fraud-provider-billing-growth-outlier-v1', '2026-06-10'
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
