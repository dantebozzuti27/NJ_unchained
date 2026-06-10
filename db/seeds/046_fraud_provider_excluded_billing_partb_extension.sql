-- ============================================================================
-- Seed: 046_fraud_provider_excluded_billing_partb_extension
--
-- Companion to migration 109. Ships the THREE evidence-card reference rows
-- for provider_excluded_billing_partb (the Part-B companion to
-- provider_excluded_billing):
--   * ref.fraud_signal_human_explanation
--   * ref.fraud_signal_severity_calibration
--   * ref.fraud_signal_evidence_url_template
-- Mirrors seed 041 in shape; same HHS-OIG authority + severity 5; only the
-- billing roster (Part B) and dollar field (Medicare paid amount) differ.
-- IDEMPOTENT VIA ON CONFLICT DO UPDATE.
-- ============================================================================


INSERT INTO ref.formula_version (
    formula_version, description, effective_date, notes
) VALUES (
    '2.8.9-fraud-provider-excluded-billing-partb-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 signal slice. Adds '
    'provider_excluded_billing_partb: an active HHS-OIG LEIE exclusion '
    '(with a real NPI) present in CMS Medicare Part B (Physician & Other '
    'Practitioners) data for a year in which the exclusion was already in '
    'effect. Exact NPI equijoin; severity 5 (42 USC 1320a-7a). raw_value = '
    'Tot_Mdcr_Pymt_Amt. Part-B companion to provider_excluded_billing.',
    '2026-06-09',
    'Stacks on 2.8.8-nppes-substrate-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


INSERT INTO ref.fraud_signal_human_explanation (
    signal_id, rule_text, citation_authority, citation_section, citation_url,
    plain_english_template, formula_version, effective_date
) VALUES (
    'provider_excluded_billing_partb',
    'A provider on the HHS-OIG List of Excluded Individuals/Entities (LEIE), '
    'matched by exact NPI, appears in CMS Medicare Physician & Other '
    'Practitioners (Part B) data for a year in which the exclusion was '
    'already in effect (exclusion date on or before year-end and not yet '
    'reinstated). Under the OIG Special Advisory Bulletin on the Effect of '
    'Exclusion, NO federal health-care program payment may be made for any '
    'item or service furnished or ordered by an excluded person -- including '
    'the Part B services this practitioner billed. Civil monetary penalties '
    'attach under 42 USC 1320a-7a. The NPI match is exact (not name-based), '
    'so identity is high confidence; severity 5 routes every match to '
    'analyst review. This is the Part-B companion to '
    'provider_excluded_billing (Part D).',
    'HHS-OIG',
    '42 USC 1320a-7a; OIG SAB on the Effect of Exclusion (2013)',
    'https://oig.hhs.gov/exclusions/effects_of_exclusion.asp',
    'Provider NPI {{entity_id}} is on the HHS-OIG LEIE exclusion list AND '
    'appears in CMS Medicare Part B practitioner data for {{cycle}} with '
    'Medicare paid amount of ${{raw_value}}. Federal program payment is '
    'prohibited for items and services furnished or ordered by an excluded '
    'provider. This is an exact NPI match (high-confidence identity), not a '
    'name guess. Bucket = all Part B practitioners in {{cycle}}; percentile '
    '{{peer_percentile}}% reflects how rare an excluded-provider overlap is '
    'in that population. Verify on the OIG LEIE portal and the CMS '
    'provider-data file before any finding -- the match flags a '
    'payment-prohibition overlap for review, not a proven improper payment.',
    '2.8.9-fraud-provider-excluded-billing-partb-v1',
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
    'provider_excluded_billing_partb',
    5,
    'oig_report',
    'https://oig.hhs.gov/exclusions/files/sab-05092013.pdf',
    'Severity 5 (CRITICAL). The OIG 2013 Special Advisory Bulletin on the '
    'Effect of Exclusion establishes that the exclusion payment prohibition '
    'is broad: no federal health-care program payment for items or services '
    'furnished or ordered by an excluded person, with CMPs under 42 USC '
    '1320a-7a. An excluded NPI in Part B practitioner data overlapping its '
    'exclusion window is a direct payment-prohibition overlap -- the same '
    'consequence-tier as the Part-D provider_excluded_billing and the '
    'FEC-side entity_on_leie matches. Severity captures "if true, how bad" '
    '(grave); peer_percentile captures rarity in the Part B practitioner '
    'population, which lands near 1.0 for the handful of overlaps.',
    '2.8.9-fraud-provider-excluded-billing-partb-v1',
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
    'provider_excluded_billing_partb',
    'https://exclusions.oig.hhs.gov/',
    'Search OIG LEIE',
    'OIG.gov',
    '2.8.9-fraud-provider-excluded-billing-partb-v1',
    '2026-06-09'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();
