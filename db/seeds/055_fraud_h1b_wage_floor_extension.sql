-- ============================================================================
-- Seed: 055_fraud_h1b_wage_floor_extension
--
-- Companion to migration 124. Evidence-card + reportability rows for
-- the at-PW-floor tail and LCA willful-attestation signals.
-- IDEMPOTENT via ON CONFLICT DO UPDATE.
-- ============================================================================

INSERT INTO ref.formula_version (
    formula_version, description, effective_date, notes
) VALUES (
    '3.9.0-fraud-h1b-wage-floor-v1',
    'Pillar 2 FRAUD-V1c H-1B at-PW-floor tail + LCA willful attestation. See mig 124.',
    '2026-09-02',
    'Seed companion to db/migrations/124_fraud_h1b_wage_floor.sql.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


INSERT INTO ref.fraud_signal_human_explanation (
    signal_id, rule_text, citation_authority, citation_section, citation_url,
    plain_english_template, formula_version, effective_date
) VALUES
(
    'employer_wage_at_pw_floor_share_outlier',
    'A New Jersey H-1B employer whose share of CERTIFIED Labor Condition '
    'Applications filed with offered wage exactly equal to the prevailing '
    'wage (same unit) sits in the extreme upper tail of NJ employers with '
    'at least h1b_floor_min_cases compared filings. Filing at the floor is '
    'lawful under 20 CFR 655.731 (offered >= PW). Empirical percentile.',
    'DOL-OFLC',
    '20 CFR 655.731 (required wage); ETA-9035 offered vs prevailing wage',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.731',
    'Employer {{entity_id}} is in the extreme upper tail of CERTIFIED H-1B '
    'LCA share filed exactly at the prevailing wage in FY{{cycle}}: share '
    '{{raw_value}} (peer percentile {{peer_percentile}}). That is a lawful '
    'statutory floor (20 CFR 655.731); this flag is only the peer tail. '
    'Verify the source LCA rows on the DOL OFLC performance page.',
    '3.9.0-fraud-h1b-wage-floor-v1',
    '2026-09-02'
),
(
    'employer_lca_willful_attestation',
    'A New Jersey H-1B employer with at least one CERTIFIED Labor Condition '
    'Application that attested WILLFUL_VIOLATOR = Y on Form ETA-9035. This '
    'is the employer''s own attestation, distinct from the DOL WHD official '
    'willful-violator list.',
    'DOL-OFLC',
    'ETA-9035 WILLFUL_VIOLATOR attestation; 20 CFR 655.736',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.736',
    'Employer {{entity_id}} attested WILLFUL_VIOLATOR = Y on {{raw_value}} '
    'CERTIFIED H-1B LCA filing(s) in FY{{cycle}}. That is the employer''s '
    'own ETA-9035 attestation, not a new WHD finding. Confirm the source '
    'LCA row on the DOL OFLC performance page.',
    '3.9.0-fraud-h1b-wage-floor-v1',
    '2026-09-02'
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


UPDATE ref.fraud_signal_human_explanation
SET rule_text = replace(
        rule_text,
        'or secondary-entity share tail).',
        'secondary-entity share tail, or at-PW-floor share tail).'
    ),
    plain_english_template = replace(
        plain_english_template,
        'and/or secondary-entity tail).',
        'secondary-entity tail, and/or at-PW-floor tail).'
    ),
    formula_version = '3.9.0-fraud-h1b-wage-floor-v1',
    updated_at = now()
WHERE signal_id = 'employer_h1b_dependent_plus_anomaly';


INSERT INTO ref.fraud_signal_severity_calibration (
    signal_id, severity_level, calibration_basis, precedent_url,
    precedent_summary, formula_version, effective_date
) VALUES
(
    'employer_wage_at_pw_floor_share_outlier',
    3,
    'empirical_pctile',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.731',
    'Severity 3 (ELEVATED). Filing at the prevailing-wage floor is lawful; '
    'only the extreme share tail is a lead. Basis empirical_pctile.',
    '3.9.0-fraud-h1b-wage-floor-v1',
    '2026-09-02'
),
(
    'employer_lca_willful_attestation',
    5,
    'statutory_cfr',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.736',
    'Severity 5 (CRITICAL). WILLFUL_VIOLATOR = Y on the LCA is the '
    'employer''s own statutory-status attestation (20 CFR 655.736). One '
    'tier with official-list matches. Still a lead: confirm the source row.',
    '3.9.0-fraud-h1b-wage-floor-v1',
    '2026-09-02'
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
) VALUES
(
    'employer_wage_at_pw_floor_share_outlier',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'View DOL OFLC LCA files',
    'DOL.gov',
    '3.9.0-fraud-h1b-wage-floor-v1',
    '2026-09-02'
),
(
    'employer_lca_willful_attestation',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'View DOL OFLC LCA files',
    'DOL.gov',
    '3.9.0-fraud-h1b-wage-floor-v1',
    '2026-09-02'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();


INSERT INTO ref.fraud_reportability_channel (
    signal_id, recovery_program, recovery_channel, recovery_channel_url,
    statute_citation, statute_url, reward_eligible, relator_share_low,
    relator_share_high, reward_tier, raw_value_is_usd, is_prior_sanction,
    citation_text, formula_version, effective_date
) VALUES
(
    'employer_wage_at_pw_floor_share_outlier',
    'DOL OFLC required-wage mix review (no statutory bounty)',
    'DOL OFLC performance data',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    '20 CFR 655.731 (required wage)',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.731',
    FALSE, NULL, NULL, 3, FALSE, FALSE,
    'An extreme at-PW-floor share is a wage-mix lead. raw_value is a rate.',
    '3.9.0-fraud-h1b-wage-floor-v1', '2026-09-02'
),
(
    'employer_lca_willful_attestation',
    'DOL WHD willful-violator review (no statutory bounty)',
    'DOL WHD H-1B complaint',
    'https://www.dol.gov/agencies/whd/immigration/h1b',
    '20 CFR 655.736; ETA-9035 WILLFUL_VIOLATOR',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.736',
    FALSE, NULL, NULL, 3, FALSE, TRUE,
    'An LCA willful-violator attestation is a prior-status lead. raw_value '
    'is a count of CERTIFIED cases.',
    '3.9.0-fraud-h1b-wage-floor-v1', '2026-09-02'
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
