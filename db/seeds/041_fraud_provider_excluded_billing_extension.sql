-- ============================================================================
-- Seed: 041_fraud_provider_excluded_billing_extension
--
-- Companion to migration 101 (mig 101 ships the entity_kind whitelist widening
-- + refresher + fraud_signal_config row + master-refresher wiring +
-- v_entity_fraud_evidence widening; this seed ships the THREE reference rows
-- the evidence-card UI needs to render the provider_excluded_billing signal
-- end-to-end:
--
--   * ref.fraud_signal_human_explanation     (rule_text + plain-English)
--   * ref.fraud_signal_severity_calibration  (severity_level + precedent)
--   * ref.fraud_signal_evidence_url_template (upstream-verify button)
--
-- Mirrors seed 023 (nj_state_candidate_on_leie) in shape and structure.
--
-- IDEMPOTENT VIA ON CONFLICT DO UPDATE on every INSERT.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. formula_version (FK anchor; mig 101 already INSERT-ON-CONFLICTed this --
--    duplicated here as a fresh-deploy ordering safety net).
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version (
    formula_version,
    description,
    effective_date,
    notes
) VALUES (
    '2.8.1-fraud-provider-excluded-billing-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 signal slice. Adds entity_kind '
    'provider (NPI-keyed) and the provider_excluded_billing cross-source '
    'signal: an active HHS-OIG LEIE exclusion (with a real NPI) present in '
    'CMS Medicare Part D prescriber data for a year in which the exclusion '
    'was already in effect. Exact NPI equijoin; severity 5; raw_value carries '
    'gross Part D drug cost. First signal against entity_kind=provider.',
    '2026-06-08',
    'Stacks on 2.8.0-cms-medicare-substrate-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 2. Human explanation: federal payment-prohibition authority + plain English
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
    'provider_excluded_billing',
    'A provider on the HHS-OIG List of Excluded Individuals/Entities (LEIE), '
    'matched by exact NPI, appears in CMS Medicare Part D prescriber data for '
    'a year in which the exclusion was already in effect (exclusion date on or '
    'before year-end and not yet reinstated). Under the OIG Special Advisory '
    'Bulletin on the Effect of Exclusion, NO federal health-care program '
    'payment may be made for any item or service furnished, ordered, or '
    'prescribed by an excluded person -- including the prescriber''s drugs '
    'dispensed under Part D. Civil monetary penalties attach under 42 USC '
    '1320a-7a. The NPI match is exact (not name-based), so identity is high '
    'confidence; severity 5 routes every match to analyst review.',
    'HHS-OIG',
    '42 USC 1320a-7a; OIG SAB on the Effect of Exclusion (2013)',
    'https://oig.hhs.gov/exclusions/effects_of_exclusion.asp',
    'Provider NPI {{entity_id}} is on the HHS-OIG LEIE exclusion list AND '
    'appears in CMS Medicare Part D prescriber data for {{cycle}} with gross '
    'Part D drug cost of ${{raw_value}}. Federal program payment is prohibited '
    'for items, services, and prescriptions from an excluded provider. This is '
    'an exact NPI match (high-confidence identity), not a name guess. Bucket = '
    'all Part D prescribers in {{cycle}}; percentile {{peer_percentile}}% '
    'reflects how rare an excluded-provider overlap is in that population. '
    'Verify on the OIG LEIE portal and the CMS provider-data file before any '
    'finding -- the match flags a payment-prohibition overlap for review, not '
    'a proven improper payment.',
    '2.8.1-fraud-provider-excluded-billing-v1',
    '2026-06-08'
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
-- 3. Severity calibration: severity_level=5 (payment-prohibition overlap)
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
    'provider_excluded_billing',
    5,
    'oig_report',
    'https://oig.hhs.gov/exclusions/files/sab-05092013.pdf',
    'Severity 5 (CRITICAL). The OIG 2013 Special Advisory Bulletin on the '
    'Effect of Exclusion establishes that the exclusion payment prohibition is '
    'broad: no federal health-care program payment for items or services '
    'furnished, ordered, or prescribed by an excluded person, and CMPs apply '
    'under 42 USC 1320a-7a. An excluded NPI in Part D data overlapping its '
    'exclusion window is a direct payment-prohibition overlap -- the same '
    'consequence-tier as the FEC-side entity_on_leie matches. Severity '
    'captures "if true, how bad" (grave); peer_percentile captures rarity in '
    'the ~1.1M-prescriber population, which lands near 1.0 for the handful of '
    'overlaps.',
    '2.8.1-fraud-provider-excluded-billing-v1',
    '2026-06-08'
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
-- 4. Evidence URL template: OIG LEIE search portal
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_evidence_url_template (
    signal_id,
    url_template,
    button_label,
    upstream_source,
    formula_version,
    effective_date
) VALUES (
    'provider_excluded_billing',
    'https://exclusions.oig.hhs.gov/',
    'Search OIG LEIE',
    'OIG.gov',
    '2.8.1-fraud-provider-excluded-billing-v1',
    '2026-06-08'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();
