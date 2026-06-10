-- ============================================================================
-- Seed: 049_fraud_excluded_provider_received_open_payments_extension
--
-- Companion to migration 111. Ships the THREE evidence-card reference rows
-- for excluded_provider_received_open_payments (the first signal mined from
-- the CMS Open Payments substrate):
--   * ref.fraud_signal_human_explanation
--   * ref.fraud_signal_severity_calibration
--   * ref.fraud_signal_evidence_url_template
-- Mirrors seed 046 in shape, but the conduct (receiving an industry transfer
-- of value while excluded) is a CONFLICT-OF-INTEREST lead, not a federal
-- payment-prohibition breach -> SEVERITY 3 (vs 5 for the Medicare-billing
-- exact-match signals). Identity is still an EXACT NPI match, so the basis is
-- 'oig_report' (the OIG exclusion authority), not the inferred-identity basis.
-- IDEMPOTENT VIA ON CONFLICT DO UPDATE.
-- ============================================================================


INSERT INTO ref.formula_version (
    formula_version, description, effective_date, notes
) VALUES (
    '2.9.1-fraud-excluded-provider-received-open-payments-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 signal slice. Adds '
    'excluded_provider_received_open_payments: an active HHS-OIG LEIE '
    'exclusion (with a real NPI) present as a covered recipient in CMS Open '
    'Payments General Payments for a program year in which the exclusion was '
    'in effect. Exact NPI equijoin; conflict-of-interest lead (severity 3). '
    'raw_value = total payment_amount received. First Open Payments signal.',
    '2026-06-09',
    'Stacks on 2.9.0-fraud-name-resolved-excluded-provider-billing-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


INSERT INTO ref.fraud_signal_human_explanation (
    signal_id, rule_text, citation_authority, citation_section, citation_url,
    plain_english_template, formula_version, effective_date
) VALUES (
    'excluded_provider_received_open_payments',
    'A provider on the HHS-OIG List of Excluded Individuals/Entities (LEIE), '
    'matched by exact NPI, appears as a covered recipient in CMS Open Payments '
    'General Payments for a program year in which the exclusion was already in '
    'effect (exclusion date on or before year-end and not yet reinstated). '
    'Open Payments records transfers of value (consulting and speaker fees, '
    'meals, travel, royalties) from drug and device manufacturers to '
    'physicians and practitioners. An industry transfer of value is NOT a '
    'federal health-care program payment, so this is NOT itself a 42 USC '
    '1320a-7a payment-prohibition breach -- it is a CONFLICT-OF-INTEREST lead '
    'that an OIG-excluded provider remains professionally active enough for '
    'industry to court them. The NPI match is exact (not name-based), so '
    'identity is high confidence; severity 3 routes the match to review as a '
    'corroborating signal alongside any Medicare-billing overlap.',
    'HHS-OIG',
    'OIG SAB on the Effect of Exclusion (2013); 42 USC 1320a-7 (exclusion authority)',
    'https://oig.hhs.gov/exclusions/effects_of_exclusion.asp',
    'Provider NPI {{entity_id}} is on the HHS-OIG LEIE exclusion list AND '
    'received industry transfers of value totaling ${{raw_value}} recorded in '
    'CMS Open Payments for program year {{cycle}}, while the exclusion was in '
    'effect. Receiving industry payments is not itself a prohibited federal '
    'payment, but an excluded provider still being paid by drug and device '
    'makers is a conflict-of-interest lead worth review. This is an exact NPI '
    'match (high-confidence identity), not a name guess. Bucket = all Open '
    'Payments recipients in {{cycle}}; percentile {{peer_percentile}}% '
    'reflects how rare an excluded-provider overlap is in that population. '
    'Verify on the OIG LEIE portal and CMS Open Payments before any finding '
    '-- the match flags a conflict-of-interest lead for review, not a proven '
    'violation.',
    '2.9.1-fraud-excluded-provider-received-open-payments-v1',
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
    'excluded_provider_received_open_payments',
    3,
    'oig_report',
    'https://oig.hhs.gov/exclusions/files/sab-05092013.pdf',
    'Severity 3 (MODERATE). The OIG 2013 Special Advisory Bulletin on the '
    'Effect of Exclusion establishes the breadth of the exclusion payment '
    'prohibition, but Open Payments transfers of value are industry-to-'
    'physician payments, NOT federal health-care program payments -- so '
    'receiving them is not itself the 1320a-7a breach that the severity-5 '
    'Medicare-billing signals capture. It is a corroborating conflict-of-'
    'interest lead: an excluded provider whom industry is still paying is '
    'likely still practicing. Identity is exact-NPI (high confidence), so the '
    'basis is oig_report rather than inferred_identity; severity is moderate '
    'because the CONDUCT is lawful-but-suspicious, not prohibited. '
    'peer_percentile captures rarity in the Open Payments recipient '
    'population.',
    '2.9.1-fraud-excluded-provider-received-open-payments-v1',
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
    'excluded_provider_received_open_payments',
    'https://openpaymentsdata.cms.gov/',
    'Search CMS Open Payments',
    'CMS.gov',
    '2.9.1-fraud-excluded-provider-received-open-payments-v1',
    '2026-06-09'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();
