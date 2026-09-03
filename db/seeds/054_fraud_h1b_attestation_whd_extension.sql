-- ============================================================================
-- Seed: 054_fraud_h1b_attestation_whd_extension
--
-- Companion to migration 122. Ships evidence-card + reportability +
-- release-calendar rows for the four H-1B attestation / WHD signals.
-- IDEMPOTENT via ON CONFLICT DO UPDATE.
-- ============================================================================

INSERT INTO ref.formula_version (
    formula_version, description, effective_date, notes
) VALUES (
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    'Pillar 2 FRAUD-V1b H-1B attestation + WHD official-list leads. See mig 122.',
    '2026-09-02',
    'Seed companion to db/migrations/122_fraud_h1b_attestation_whd.sql.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- Human explanations
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_human_explanation (
    signal_id, rule_text, citation_authority, citation_section, citation_url,
    plain_english_template, formula_version, effective_date
) VALUES
(
    'employer_on_whd_willful_or_debarred',
    'A New Jersey H-1B employer whose canonical name matches an active row '
    'on the DOL Wage and Hour Division H-1B debarment list or willful-'
    'violator list. Debarment is active between the published start and end '
    'dates. Willful-violator status lasts 5 years from the finding '
    '(20 CFR 655.736). This is an official-list match, not a new finding.',
    'DOL-WHD',
    '20 CFR 655.736; 20 CFR 655.750(d); WHD Fact Sheet 62S',
    'https://www.dol.gov/agencies/whd/immigration/h1b/debarment',
    'Employer {{entity_id}} matches an active DOL Wage and Hour H-1B '
    'debarment or willful-violator listing in FY{{cycle}}. That is an '
    'official-list lead (20 CFR 655.736 / 655.750(d)), not a new finding. '
    'Confirm the current WHD table before any use.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'employer_level1_wage_share_outlier',
    'A New Jersey H-1B employer whose share of CERTIFIED Labor Condition '
    'Applications filed at prevailing-wage Level I sits in the extreme '
    'upper tail of NJ employers with at least h1b_level1_min_cases leveled '
    'filings. Level I is a legal OFLC wage tier, not a violation. Empirical '
    'percentile, not a statutory finding.',
    'DOL-OFLC',
    'ETA-9035 PW_WAGE_LEVEL (OFLC wage levels I-IV)',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Employer {{entity_id}} is in the extreme upper tail of H-1B LCA share '
    'filed at prevailing-wage Level I in FY{{cycle}}: share {{raw_value}} '
    '(peer percentile {{peer_percentile}}). Level I is a legal wage tier; '
    'this flag is only the peer tail. Verify the source LCA rows on the '
    'DOL OFLC performance page.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'employer_secondary_entity_share_outlier',
    'A New Jersey H-1B employer whose share of CERTIFIED LCAs attesting '
    'SECONDARY_ENTITY = Y (third-party / client-site placement, ETA-9035 '
    '§F.a.2) sits in the extreme upper tail. Empirical percentile. The '
    'attestation itself is lawful; only the extreme share is a lead.',
    'DOL-OFLC',
    'ETA-9035 §F.a.2 SECONDARY_ENTITY third-party placement attestation',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Employer {{entity_id}} is in the extreme upper tail of CERTIFIED H-1B '
    'LCA share placed at a secondary entity in FY{{cycle}}: share '
    '{{raw_value}} (peer percentile {{peer_percentile}}). Third-party '
    'placement is a lawful attestation; this flag is only the peer tail.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'employer_h1b_dependent_plus_anomaly',
    'A New Jersey employer that attested H-1B_DEPENDENT = Y on at least '
    'one CERTIFIED LCA and also fired a corroborating H-1B anomaly '
    '(below prevailing wage, LCA-vs-USCIS volume gap, Level I share tail, '
    'or secondary-entity share tail). Dependency thresholds are statutory '
    '(20 CFR 655.736); this detector uses the attestation bit as a bucket, '
    'not a recomputed headcount.',
    'DOL-OFLC',
    '20 CFR 655.736 (H-1B-dependent employer) plus corroborating FRAUD-V1 signal',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.736',
    'Employer {{entity_id}} attested H-1B-dependent in FY{{cycle}} and also '
    'fired {{raw_value}} corroborating H-1B anomalies (below-PW, volume gap, '
    'Level I tail, and/or secondary-entity tail). Dependency is a statutory '
    'bucket (20 CFR 655.736), not itself a violation.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
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


-- ----------------------------------------------------------------------------
-- Severity calibration
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_severity_calibration (
    signal_id, severity_level, calibration_basis, precedent_url,
    precedent_summary, formula_version, effective_date
) VALUES
(
    'employer_on_whd_willful_or_debarred',
    5,
    'statutory_cfr',
    'https://www.dol.gov/agencies/whd/immigration/h1b/debarment',
    'Severity 5 (CRITICAL). Presence on the WHD debarment or willful-'
    'violator list is an official-list predicate (20 CFR 655.736 / '
    '655.750(d)), one tier with exact-match exclusion signals. Still a '
    'lead: name canonicalization can collide, so the analyst must read '
    'the WHD table.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'employer_level1_wage_share_outlier',
    3,
    'empirical_pctile',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Severity 3 (ELEVATED). Level I is a legal wage tier; only the '
    'extreme share tail is a lead. Basis empirical_pctile.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'employer_secondary_entity_share_outlier',
    3,
    'empirical_pctile',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Severity 3 (ELEVATED). Third-party placement is a lawful ETA-9035 '
    'attestation; only the extreme share tail is a lead. Basis '
    'empirical_pctile.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'employer_h1b_dependent_plus_anomaly',
    4,
    'statutory_cfr',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.736',
    'Severity 4 (HIGH). H-1B-dependent status is a statutory bucket '
    '(20 CFR 655.736) that only fires here when a corroborating anomaly '
    'is also present. Basis statutory_cfr for the dependency predicate.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
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


-- ----------------------------------------------------------------------------
-- Evidence URL templates
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_evidence_url_template (
    signal_id, url_template, button_label, upstream_source,
    formula_version, effective_date
) VALUES
(
    'employer_on_whd_willful_or_debarred',
    'https://www.dol.gov/agencies/whd/immigration/h1b/debarment',
    'View WHD H-1B debarment list',
    'DOL.gov',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'employer_level1_wage_share_outlier',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'View DOL OFLC LCA files',
    'DOL.gov',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'employer_secondary_entity_share_outlier',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'View DOL OFLC LCA files',
    'DOL.gov',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
),
(
    'employer_h1b_dependent_plus_anomaly',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.736',
    'Read 20 CFR 655.736',
    'DOL.gov',
    '3.8.0-fraud-h1b-attestation-enforcement-v1',
    '2026-09-02'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();


-- ----------------------------------------------------------------------------
-- Reportability: DOL WHD — no statutory bounty (tier 3)
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_reportability_channel (
    signal_id, recovery_program, recovery_channel, recovery_channel_url,
    statute_citation, statute_url, reward_eligible, relator_share_low,
    relator_share_high, reward_tier, raw_value_is_usd, is_prior_sanction,
    citation_text, formula_version, effective_date
) VALUES
(
    'employer_on_whd_willful_or_debarred',
    'DOL Wage and Hour official H-1B list (no statutory bounty)',
    'DOL WHD H-1B debarment / willful-violator list',
    'https://www.dol.gov/agencies/whd/immigration/h1b/debarment',
    '20 CFR 655.736; 20 CFR 655.750(d); Fact Sheet 62S',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.736',
    FALSE, NULL, NULL, 3, FALSE, TRUE,
    'An official WHD list match is a prior-sanction lead, not a False '
    'Claims Act relator claim. raw_value is the boolean 1.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1', '2026-09-02'
),
(
    'employer_level1_wage_share_outlier',
    'DOL OFLC wage-level review (no statutory bounty)',
    'DOL OFLC performance data',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    '20 CFR 655.731 (required wage); ETA-9035 PW_WAGE_LEVEL',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.731',
    FALSE, NULL, NULL, 3, FALSE, FALSE,
    'An extreme Level I share is a wage-tier mix lead. raw_value is a rate.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1', '2026-09-02'
),
(
    'employer_secondary_entity_share_outlier',
    'DOL WHD third-party-placement review (no statutory bounty)',
    'DOL WHD H-1B complaint',
    'https://www.dol.gov/agencies/whd/immigration/h1b',
    'ETA-9035 §F.a.2; 20 CFR 655.734',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H',
    FALSE, NULL, NULL, 3, FALSE, FALSE,
    'An extreme secondary-entity share is a placement-pattern lead. '
    'raw_value is a rate.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1', '2026-09-02'
),
(
    'employer_h1b_dependent_plus_anomaly',
    'DOL WHD H-1B-dependent employer review (no statutory bounty)',
    'DOL WHD H-1B complaint',
    'https://www.dol.gov/agencies/whd/immigration/h1b',
    '20 CFR 655.736; 20 CFR 655.738',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.736',
    FALSE, NULL, NULL, 3, FALSE, FALSE,
    'H-1B-dependent status plus a corroborating anomaly is a review '
    'lead. raw_value is a count of corroborating signals, not USD.',
    '3.8.0-fraud-h1b-attestation-enforcement-v1', '2026-09-02'
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


-- ----------------------------------------------------------------------------
-- Release calendar
-- ----------------------------------------------------------------------------
INSERT INTO ref.release_calendar
    (source_id, cadence, schedule_label, day_of_week, day_of_month,
     month_of_year, time_of_day_local, timezone, expected_lag_hours, notes)
VALUES
    (
        'raw.dol_whd_h1b_list',
        'weekly',
        'DOL WHD H-1B debarment + willful-violator HTML lists',
        NULL, NULL, NULL, '00:00:00', 'America/New_York', 168,
        'Sources: https://www.dol.gov/agencies/whd/immigration/h1b/debarment '
        'and https://www.dol.gov/agencies/whd/immigration/h1b/willful-violator-list. '
        'Last observed update 2026-08-28, effective 2026-09-01. Tiny HTML '
        'tables; full-replace ingest.'
    )
ON CONFLICT (source_id) DO UPDATE SET
    cadence            = EXCLUDED.cadence,
    schedule_label     = EXCLUDED.schedule_label,
    expected_lag_hours = EXCLUDED.expected_lag_hours,
    notes              = EXCLUDED.notes,
    updated_at         = now();
