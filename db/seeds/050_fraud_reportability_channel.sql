-- ============================================================================
-- Seed: 050_fraud_reportability_channel
--
-- Companion to migration 112. Populates ref.fraud_reportability_channel with
-- one cited row per configured signal (26 rows). This is REFERENCE DATA: each
-- row maps a signal to the enforcement channel that can act on it and the
-- STATUTORY relator/whistleblower reward band where one exists.
--
-- reward_tier ladder (1 = highest reportability reward potential):
--   1  Federal False Claims Act, EXACT identity, adjudicable USD exposure
--      (excluded provider billing Medicare). 15-30% relator share.
--   2  False Claims Act / Anti-Kickback, but inferred identity or lawful-but-
--      suspicious conduct (name-resolved billing; industry payments to excluded).
--   3  HHS-OIG / CMS utilization referral -- actionable lead, NO statutory bounty.
--   4  Federal exclusion / debarment flag in a civic-money context, or NJ
--      pay-to-play -- HHS-OIG / GSA / FEC / NJ ELEC referral, NO bounty.
--   5  FEC structural / registration anomaly -- FEC complaint, NO bounty.
--
-- HONESTY NOTE: the IRS whistleblower lane (26 U.S.C. § 7623, for 501c4/527
-- "dark money" tax abuse) is NOT represented here because the platform holds no
-- IRS Form 990/8872 substrate and FEC bulk does not expose unexplained
-- nonprofit flows. No signal is mapped to it rather than fabricate one.
--
-- IDEMPOTENT VIA ON CONFLICT DO UPDATE.
-- ============================================================================

INSERT INTO ref.fraud_reportability_channel (
    signal_id, recovery_program, recovery_channel, recovery_channel_url,
    statute_citation, statute_url, reward_eligible,
    relator_share_low, relator_share_high, reward_tier,
    raw_value_is_usd, is_prior_sanction, citation_text,
    formula_version, effective_date
) VALUES

-- ---- TIER 1: Federal False Claims Act, exact identity, USD exposure ---------
(
    'provider_excluded_billing',
    'DOJ False Claims Act (federal qui tam)',
    'DOJ Civil Fraud / U.S. Attorney qui tam complaint; corroborate via HHS-OIG Hotline',
    'https://www.justice.gov/civil/false-claims-act',
    '31 U.S.C. § 3730(d) (relator share); 42 U.S.C. § 1320a-7a(a) (CMP for excluded-party claims)',
    'https://www.law.cornell.edu/uscode/text/31/3730',
    TRUE, 0.15, 0.30, 1, TRUE, TRUE,
    'A provider on the HHS-OIG LEIE who submits Medicare Part D claims presents '
    'false claims; the relator share for a successful FCA action is 15-30% of '
    'the recovery (31 U.S.C. § 3730(d)).',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'provider_excluded_billing_partb',
    'DOJ False Claims Act (federal qui tam)',
    'DOJ Civil Fraud / U.S. Attorney qui tam complaint; corroborate via HHS-OIG Hotline',
    'https://www.justice.gov/civil/false-claims-act',
    '31 U.S.C. § 3730(d) (relator share); 42 U.S.C. § 1320a-7a(a) (CMP for excluded-party claims)',
    'https://www.law.cornell.edu/uscode/text/31/3730',
    TRUE, 0.15, 0.30, 1, TRUE, TRUE,
    'A provider on the HHS-OIG LEIE who submits Medicare Part B claims presents '
    'false claims; the relator share for a successful FCA action is 15-30% of '
    'the recovery (31 U.S.C. § 3730(d)).',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'state_excluded_provider_billing',
    'DOJ / NJ False Claims Act (qui tam)',
    'DOJ Civil Fraud + NJ Office of the State Comptroller / Medicaid Fraud Division; HHS-OIG Hotline',
    'https://www.justice.gov/civil/false-claims-act',
    '31 U.S.C. § 3730(d); N.J.S.A. 2A:32C-7 (NJ FCA relator share)',
    'https://www.law.cornell.edu/uscode/text/31/3730',
    TRUE, 0.15, 0.30, 1, TRUE, TRUE,
    'A NJ-Medicaid-debarred provider still billing Medicare presents false '
    'claims to a federal program; both the federal FCA (31 U.S.C. § 3730(d)) '
    'and the NJ FCA (N.J.S.A. 2A:32C-7) authorize a 15-30% relator share.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),

-- ---- TIER 2: FCA/AKS, inferred identity or lawful-but-suspicious -----------
(
    'name_resolved_excluded_provider_billing',
    'DOJ False Claims Act (federal qui tam) — inferred identity',
    'DOJ Civil Fraud qui tam; verify identity before filing; HHS-OIG Hotline',
    'https://www.justice.gov/civil/false-claims-act',
    '31 U.S.C. § 3730(d)',
    'https://www.law.cornell.edu/uscode/text/31/3730',
    TRUE, 0.15, 0.30, 2, TRUE, TRUE,
    'Same FCA reward basis as the exact-NPI signal (15-30% relator share, '
    '31 U.S.C. § 3730(d)), but identity is name+state inferred rather than an '
    'NPI equijoin; tier 2 reflects the lower identity confidence.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'excluded_provider_received_open_payments',
    'Anti-Kickback Statute / FCA (conflict-of-interest lead)',
    'HHS-OIG Hotline; DOJ Civil Fraud if tied to billing',
    'https://oig.hhs.gov/fraud/report-fraud/',
    '42 U.S.C. § 1320a-7b (AKS); 31 U.S.C. § 3730(d) (FCA relator share)',
    'https://www.law.cornell.edu/uscode/text/42/1320a-7b',
    TRUE, 0.15, 0.30, 2, TRUE, TRUE,
    'Industry transfers of value to an excluded provider are a conflict-of-'
    'interest lead; where tied to tainted claims, AKS violations are per se '
    'false claims carrying a 15-30% FCA relator share (31 U.S.C. § 3730(d)).',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),

-- ---- TIER 3: HHS-OIG / CMS utilization referral, no bounty ------------------
(
    'opioid_prescribing_outlier',
    'HHS-OIG / CMS / DEA referral (no statutory bounty)',
    'HHS-OIG Hotline',
    'https://oig.hhs.gov/fraud/report-fraud/',
    '42 U.S.C. § 1320a-7a (Civil Monetary Penalties Law)',
    'https://www.law.cornell.edu/uscode/text/42/1320a-7a',
    FALSE, NULL, NULL, 3, FALSE, FALSE,
    'A high opioid-prescribing rate is an investigative lead routed to '
    'HHS-OIG/CMS; it is not by itself a false claim, so no statutory relator '
    'reward attaches.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'services_per_beneficiary_outlier',
    'HHS-OIG / CMS referral (no statutory bounty)',
    'HHS-OIG Hotline',
    'https://oig.hhs.gov/fraud/report-fraud/',
    '42 U.S.C. § 1320a-7a (Civil Monetary Penalties Law)',
    'https://www.law.cornell.edu/uscode/text/42/1320a-7a',
    FALSE, NULL, NULL, 3, FALSE, FALSE,
    'An extreme services-per-beneficiary ratio is a utilization-anomaly lead '
    'for HHS-OIG/CMS review; not itself a false claim, so no relator reward '
    'attaches.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),

-- ---- TIER 4: exclusion / debarment flags + NJ pay-to-play, no bounty --------
(
    'entity_on_leie',
    'HHS-OIG exclusion verification (no bounty)',
    'HHS-OIG Hotline / LEIE portal',
    'https://oig.hhs.gov/fraud/report-fraud/',
    '42 U.S.C. § 1320a-7 (exclusion authority)',
    'https://www.law.cornell.edu/uscode/text/42/1320a-7',
    FALSE, NULL, NULL, 4, FALSE, TRUE,
    'An entity matched to the HHS-OIG LEIE is a federally-excluded party; '
    'absent a billing link this is an exclusion-verification flag, not a '
    'reward-bearing claim.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'entity_on_leie_strict_address',
    'HHS-OIG exclusion verification (no bounty)',
    'HHS-OIG Hotline / LEIE portal',
    'https://oig.hhs.gov/fraud/report-fraud/',
    '42 U.S.C. § 1320a-7 (exclusion authority)',
    'https://www.law.cornell.edu/uscode/text/42/1320a-7',
    FALSE, NULL, NULL, 4, FALSE, TRUE,
    'Address-corroborated LEIE match; an exclusion-verification flag (no bounty '
    'without a billing link).',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'donor_on_leie',
    'HHS-OIG / FEC referral (no bounty)',
    'HHS-OIG Hotline; FEC complaint if campaign-finance nexus',
    'https://oig.hhs.gov/fraud/report-fraud/',
    '42 U.S.C. § 1320a-7; 52 U.S.C. § 30109',
    'https://www.law.cornell.edu/uscode/text/42/1320a-7',
    FALSE, NULL, NULL, 4, FALSE, TRUE,
    'A political donor matched to the LEIE is a federally-excluded individual; '
    'reportable to HHS-OIG and, for any campaign-finance nexus, the FEC. No '
    'statutory bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'candidate_funded_by_excluded_donors',
    'FEC complaint (no bounty)',
    'FEC Office of General Counsel complaint',
    'https://www.fec.gov/legal-resources/enforcement/complaints-process/',
    '52 U.S.C. § 30109',
    'https://www.law.cornell.edu/uscode/text/52/30109',
    FALSE, NULL, NULL, 4, FALSE, FALSE,
    'A candidate whose donors include excluded parties is a source-of-funds '
    'lead for FEC review; the FEC administers no whistleblower bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'entity_funded_and_excluded',
    'HHS-OIG / FEC referral (no bounty)',
    'HHS-OIG Hotline + FEC complaint',
    'https://oig.hhs.gov/fraud/report-fraud/',
    '42 U.S.C. § 1320a-7; 52 U.S.C. § 30109',
    'https://www.law.cornell.edu/uscode/text/42/1320a-7',
    FALSE, NULL, NULL, 4, FALSE, TRUE,
    'An entity both receiving funds and federally excluded is a cross-source '
    'lead; reportable to HHS-OIG and the FEC. No statutory bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'nj_state_candidate_on_leie',
    'NJ ELEC / HHS-OIG referral (no bounty)',
    'NJ Election Law Enforcement Commission; HHS-OIG Hotline',
    'https://www.elec.nj.gov/',
    '42 U.S.C. § 1320a-7; N.J.S.A. 19:44A (NJ Campaign Act)',
    'https://www.law.cornell.edu/uscode/text/42/1320a-7',
    FALSE, NULL, NULL, 4, FALSE, TRUE,
    'A NJ state candidate matched to the LEIE is a federally-excluded '
    'individual active in state politics; reportable to NJ ELEC and HHS-OIG. '
    'No statutory bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'entity_excluded_via_sam_uei',
    'GSA suspension & debarment (no bounty)',
    'Agency Suspension & Debarment Official / GSA OIG; SAM.gov',
    'https://sam.gov/',
    '2 C.F.R. Part 180; FAR Subpart 9.4',
    'https://www.acquisition.gov/far/subpart-9.4',
    FALSE, NULL, NULL, 4, FALSE, TRUE,
    'A SAM.gov-excluded entity (matched by UEI) is federally debarred or '
    'suspended; reportable to the agency S&D official and GSA. Debarment '
    'carries no whistleblower bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'donor_on_sam',
    'GSA debarment / FEC referral (no bounty)',
    'GSA / SAM.gov; FEC complaint if campaign nexus',
    'https://sam.gov/',
    '2 C.F.R. Part 180; 52 U.S.C. § 30109',
    'https://www.acquisition.gov/far/subpart-9.4',
    FALSE, NULL, NULL, 4, FALSE, TRUE,
    'A donor matched to SAM.gov exclusions is a federally-excluded party; '
    'reportable to GSA and, for any campaign nexus, the FEC. No bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'candidate_funded_by_sam_excluded_donors',
    'FEC complaint (no bounty)',
    'FEC Office of General Counsel complaint',
    'https://www.fec.gov/legal-resources/enforcement/complaints-process/',
    '52 U.S.C. § 30109',
    'https://www.law.cornell.edu/uscode/text/52/30109',
    FALSE, NULL, NULL, 4, FALSE, FALSE,
    'A candidate funded by SAM-excluded donors is a source-of-funds lead for '
    'FEC review; no statutory bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'candidate_funded_by_nj_contractor_employees',
    'NJ ELEC pay-to-play (no bounty)',
    'NJ Election Law Enforcement Commission',
    'https://www.elec.nj.gov/',
    'N.J.S.A. 19:44A-20.13 et seq. (pay-to-play)',
    'https://www.elec.nj.gov/pay2play.html',
    FALSE, NULL, NULL, 4, FALSE, FALSE,
    'Contributions from a state contractor''s employees to a candidate are a '
    'pay-to-play lead for NJ ELEC; no statutory bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'donor_employed_by_nj_contractor',
    'NJ ELEC pay-to-play (no bounty)',
    'NJ Election Law Enforcement Commission',
    'https://www.elec.nj.gov/',
    'N.J.S.A. 19:44A-20.13 et seq. (pay-to-play)',
    'https://www.elec.nj.gov/pay2play.html',
    FALSE, NULL, NULL, 4, FALSE, FALSE,
    'A donor employed by a state contractor is a pay-to-play disclosure lead '
    'for NJ ELEC; no statutory bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),

-- ---- TIER 5: FEC structural / registration anomalies, no bounty ------------
(
    'committee_address_clusters',
    'FEC complaint (no bounty)',
    'FEC Office of General Counsel complaint',
    'https://www.fec.gov/legal-resources/enforcement/complaints-process/',
    '52 U.S.C. § 30109',
    'https://www.law.cornell.edu/uscode/text/52/30109',
    FALSE, NULL, NULL, 5, FALSE, FALSE,
    'Many committees sharing one address is a structural FEC-registration '
    'anomaly for FEC review; no statutory bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'treasurer_concentration',
    'FEC complaint (no bounty)',
    'FEC Office of General Counsel complaint',
    'https://www.fec.gov/legal-resources/enforcement/complaints-process/',
    '52 U.S.C. § 30102 (treasurer duties)',
    'https://www.law.cornell.edu/uscode/text/52/30102',
    FALSE, NULL, NULL, 5, FALSE, FALSE,
    'One treasurer controlling many committees is an FEC recordkeeping/control '
    'anomaly (52 U.S.C. § 30102); FEC review, no bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'candidate_namesakes',
    'FEC complaint (no bounty)',
    'FEC Office of General Counsel complaint',
    'https://www.fec.gov/legal-resources/enforcement/complaints-process/',
    '52 U.S.C. § 30109',
    'https://www.law.cornell.edu/uscode/text/52/30109',
    FALSE, NULL, NULL, 5, FALSE, FALSE,
    'Distinct candidates sharing a name is a deconfliction/structural lead for '
    'FEC review; no bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'committee_name_collisions',
    'FEC complaint (no bounty)',
    'FEC Office of General Counsel complaint',
    'https://www.fec.gov/legal-resources/enforcement/complaints-process/',
    '52 U.S.C. § 30109',
    'https://www.law.cornell.edu/uscode/text/52/30109',
    FALSE, NULL, NULL, 5, FALSE, FALSE,
    'Committees with colliding names is a structural FEC anomaly for review; '
    'no bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'candidate_broken_pcc',
    'FEC complaint (no bounty)',
    'FEC Office of General Counsel complaint',
    'https://www.fec.gov/legal-resources/enforcement/complaints-process/',
    '52 U.S.C. § 30103 (committee registration)',
    'https://www.law.cornell.edu/uscode/text/52/30103',
    FALSE, NULL, NULL, 5, FALSE, FALSE,
    'A candidate referencing a principal campaign committee that is not '
    'registered is an FEC organization/registration defect (52 U.S.C. § 30103); '
    'FEC review, no bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'candidate_multiple_pccs',
    'FEC complaint (no bounty)',
    'FEC Office of General Counsel complaint',
    'https://www.fec.gov/legal-resources/enforcement/complaints-process/',
    '52 U.S.C. § 30102(e) (single principal campaign committee)',
    'https://www.law.cornell.edu/uscode/text/52/30102',
    FALSE, NULL, NULL, 5, FALSE, FALSE,
    'A candidate with multiple principal campaign committees contravenes the '
    'single-PCC rule (52 U.S.C. § 30102(e)); FEC review, no bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'treasurer_is_candidate',
    'FEC complaint (no bounty)',
    'FEC Office of General Counsel complaint',
    'https://www.fec.gov/legal-resources/enforcement/complaints-process/',
    '52 U.S.C. § 30102 (treasurer duties)',
    'https://www.law.cornell.edu/uscode/text/52/30102',
    FALSE, NULL, NULL, 5, FALSE, FALSE,
    'A candidate serving as their own committee treasurer is a control/'
    'structural flag for FEC review; no bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
),
(
    'candidate_no_pcc',
    'FEC complaint (no bounty)',
    'FEC Office of General Counsel complaint',
    'https://www.fec.gov/legal-resources/enforcement/complaints-process/',
    '52 U.S.C. § 30102(e) (single principal campaign committee)',
    'https://www.law.cornell.edu/uscode/text/52/30102',
    FALSE, NULL, NULL, 5, FALSE, FALSE,
    'A candidate with no principal campaign committee on record is an FEC '
    'registration gap (52 U.S.C. § 30102(e)); FEC review, no bounty.',
    '3.0.0-fraud-high-value-leads-v1', '2026-06-09'
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
    effective_date       = EXCLUDED.effective_date,
    updated_at           = now();
