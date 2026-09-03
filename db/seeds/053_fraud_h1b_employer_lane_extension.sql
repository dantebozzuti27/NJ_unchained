-- ============================================================================
-- Seed: 053_fraud_h1b_employer_lane_extension
--
-- Companion to migration 121. Ships evidence-card + reportability +
-- release-calendar rows for the four H-1B employer signals.
-- IDEMPOTENT via ON CONFLICT DO UPDATE.
-- ============================================================================

INSERT INTO ref.formula_version (
    formula_version, description, effective_date, notes
) VALUES (
    '3.7.0-fraud-h1b-employer-lane-v1',
    'Pillar 2 FRAUD-V1 / POP-3 H-1B employer visa-fraud leads. See mig 121.',
    '2026-09-02',
    'Seed companion to db/migrations/121_fraud_h1b_employer_lane.sql.'
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
    'employer_below_prevailing_wage',
    'A New Jersey H-1B employer with one or more CERTIFIED Labor Condition '
    'Applications whose annualized offered wage is below the annualized '
    'prevailing wage by at least the platform constant h1b_below_pw_min_gap_usd. '
    'INA §212(n) and 20 CFR 655.731 require the offered wage to equal or exceed '
    'the prevailing wage. This is a lead assembled from public LCA disclosure '
    'rows, not an adjudicated wage-and-hour finding.',
    'DOL-OFLC',
    'INA §212(n); 20 CFR 655.731 (required wage)',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.731',
    'Employer {{entity_id}} has CERTIFIED H-1B LCA filings in FY{{cycle}} whose '
    'offered wage is below the prevailing wage by a combined ${{raw_value}} '
    '(annualized). That shortfall is a statutory required-wage lead under '
    '20 CFR 655.731, not proof of a violation — confirm the source LCA rows '
    'on the DOL OFLC performance page before any finding.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'employer_h1b_denial_rate_outlier',
    'A New Jersey USCIS H-1B petitioner whose first-decision denial rate '
    '(initial + continuing denials divided by all first decisions) sits in '
    'the extreme upper tail of NJ petitioners with at least '
    'h1b_denial_min_petitions decisions in the fiscal year. Empirical '
    'percentile, not a statutory violation.',
    'USCIS',
    'USCIS H-1B Employer Data Hub first-decision counts',
    'https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub',
    'Employer {{entity_id}} is in the extreme upper tail of H-1B petition '
    'denial rates among New Jersey petitioners in FY{{cycle}}: denial rate '
    '{{raw_value}} (peer percentile {{peer_percentile}}). A high denial rate '
    'is a lead — new petitioners and lottery-heavy specialties can land here '
    'legitimately. Verify on the USCIS H-1B Employer Data Hub.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'employer_lca_uscis_volume_gap',
    'An employer present in both DOL OFLC LCA disclosures and the USCIS H-1B '
    'Employer Data Hub whose certified LCA worker count divided by USCIS '
    'approvals sits in the extreme upper tail of matched New Jersey employers. '
    'LCA volume overstates approvals by construction (filings precede '
    'adjudication); only the extreme tail is treated as a lead.',
    'DOL-OFLC',
    'DOL OFLC LCA disclosure x USCIS Employer Data Hub (cross-source)',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Employer {{entity_id}} has an extreme certified-LCA-workers / USCIS-'
    'approvals ratio of {{raw_value}} in FY{{cycle}} (peer percentile '
    '{{peer_percentile}}). Labor Condition Applications are filed before '
    'USCIS decides the petition, so a gap is expected; this flag is only '
    'the top tail. Confirm both source files before treating it as a lead.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'employer_certified_withdrawn_rate_outlier',
    'A New Jersey H-1B employer whose share of decided LCAs that are '
    'CERTIFIED-WITHDRAWN sits in the extreme upper tail. A high '
    'certified-then-withdrawn share is a recognized benching / file-then-'
    'abandon lead, not proof of a violation.',
    'DOL-OFLC',
    'DOL OFLC LCA case_status mix (CERTIFIED-WITHDRAWN share)',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Employer {{entity_id}} is in the extreme upper tail of CERTIFIED-'
    'WITHDRAWN H-1B LCA share in FY{{cycle}}: rate {{raw_value}} (peer '
    'percentile {{peer_percentile}}). This is a statistical benching lead, '
    'not a finding. Verify the underlying LCA rows on the DOL OFLC page.',
    '3.7.0-fraud-h1b-employer-lane-v1',
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
    'employer_below_prevailing_wage',
    5,
    'statutory_cfr',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.731',
    'Severity 5 (CRITICAL). Offered wage below prevailing wage is the '
    'statutory required-wage predicate (20 CFR 655.731). One tier with the '
    'exact-match exclusion signals because the shortfall is a binary legal '
    'condition, not a distributional tail. Still a lead: annualization and '
    'rounding can produce false shorts, so the analyst must read the LCA.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'employer_h1b_denial_rate_outlier',
    4,
    'empirical_pctile',
    'https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub',
    'Severity 4 (HIGH). An extreme USCIS first-decision denial rate is a '
    'lead for petition quality / body-shop review, not an adjudicated '
    'finding. Basis empirical_pctile.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'employer_lca_uscis_volume_gap',
    3,
    'empirical_pctile',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Severity 3 (ELEVATED). LCA volume exceeds USCIS approvals by '
    'construction; only the extreme tail is a lead. Basis empirical_pctile.',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'employer_certified_withdrawn_rate_outlier',
    3,
    'empirical_pctile',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'Severity 3 (ELEVATED). High CERTIFIED-WITHDRAWN share is a benching '
    'lead. Basis empirical_pctile.',
    '3.7.0-fraud-h1b-employer-lane-v1',
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
    'employer_below_prevailing_wage',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'View DOL OFLC LCA files',
    'DOL.gov',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'employer_h1b_denial_rate_outlier',
    'https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub',
    'Search USCIS H-1B Data Hub',
    'USCIS.gov',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'employer_lca_uscis_volume_gap',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'View DOL OFLC LCA files',
    'DOL.gov',
    '3.7.0-fraud-h1b-employer-lane-v1',
    '2026-09-02'
),
(
    'employer_certified_withdrawn_rate_outlier',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    'View DOL OFLC LCA files',
    'DOL.gov',
    '3.7.0-fraud-h1b-employer-lane-v1',
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
-- Reportability: DOL WHD / USCIS — no statutory bounty (tier 3)
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_reportability_channel (
    signal_id, recovery_program, recovery_channel, recovery_channel_url,
    statute_citation, statute_url, reward_eligible, relator_share_low,
    relator_share_high, reward_tier, raw_value_is_usd, is_prior_sanction,
    citation_text, formula_version, effective_date
) VALUES
(
    'employer_below_prevailing_wage',
    'DOL Wage and Hour / OFLC required-wage complaint (no statutory bounty)',
    'DOL WHD H-1B complaint',
    'https://www.dol.gov/agencies/whd/immigration/h1b',
    '20 CFR 655.731; INA §212(n); 8 U.S.C. §1182(n)',
    'https://www.law.cornell.edu/uscode/text/8/1182',
    FALSE, NULL, NULL, 3, TRUE, FALSE,
    'A below-prevailing-wage LCA is a required-wage lead routed to DOL '
    'Wage and Hour. There is no False Claims Act relator share for an H-1B '
    'wage shortfall by itself. raw_value is USD gap.',
    '3.7.0-fraud-h1b-employer-lane-v1', '2026-09-02'
),
(
    'employer_h1b_denial_rate_outlier',
    'USCIS / DOL OFLC referral (no statutory bounty)',
    'USCIS H-1B Employer Data Hub',
    'https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub',
    'INA §214(i); 8 CFR 214.2(h)',
    'https://www.law.cornell.edu/cfr/text/8/214.2',
    FALSE, NULL, NULL, 3, FALSE, FALSE,
    'An extreme denial rate is an adjudication-quality lead, not a '
    'recoverable false claim. raw_value is a rate.',
    '3.7.0-fraud-h1b-employer-lane-v1', '2026-09-02'
),
(
    'employer_lca_uscis_volume_gap',
    'DOL OFLC / USCIS cross-source referral (no statutory bounty)',
    'DOL OFLC performance data',
    'https://www.dol.gov/agencies/eta/foreign-labor/performance',
    '20 CFR 655.730 (LCA filing) + INA §214(c)',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H/section-655.730',
    FALSE, NULL, NULL, 3, FALSE, FALSE,
    'An extreme LCA-vs-approval gap is a cross-source lead. raw_value is '
    'a ratio, not USD.',
    '3.7.0-fraud-h1b-employer-lane-v1', '2026-09-02'
),
(
    'employer_certified_withdrawn_rate_outlier',
    'DOL WHD benching / LCA withdrawal review (no statutory bounty)',
    'DOL WHD H-1B complaint',
    'https://www.dol.gov/agencies/whd/immigration/h1b',
    '20 CFR 655.731; 20 CFR 655.734',
    'https://www.ecfr.gov/current/title-20/chapter-V/part-655/subpart-H',
    FALSE, NULL, NULL, 3, FALSE, FALSE,
    'A high CERTIFIED-WITHDRAWN share is a benching lead. raw_value is a '
    'rate.',
    '3.7.0-fraud-h1b-employer-lane-v1', '2026-09-02'
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
        'raw.uscis_h1b_employer',
        'quarterly',
        'USCIS H-1B Employer Data Hub FY files (FY2026 through Q3 as of 2026-09)',
        NULL, NULL, NULL, '00:00:00', 'America/New_York', 2160,
        'Source: https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub. '
        'CSV exports of first-decision counts, not unique workers. POP-3 '
        'substrate for FRAUD-V1 denial-rate and LCA-gap signals.'
    )
ON CONFLICT (source_id) DO UPDATE SET
    cadence            = EXCLUDED.cadence,
    schedule_label     = EXCLUDED.schedule_label,
    expected_lag_hours = EXCLUDED.expected_lag_hours,
    notes              = EXCLUDED.notes,
    updated_at         = now();
