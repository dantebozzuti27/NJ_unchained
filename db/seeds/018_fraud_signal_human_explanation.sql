-- ============================================================================
-- Seed: 018_fraud_signal_human_explanation
--
-- 17 rows, one per fraud signal_id, citing the specific federal authority
-- whose predicate the signal codifies. Read by the /risk/[kind]/[id] UI
-- evidence drill-down ("Why this fired" block).
--
-- Citation discipline (per .cursor/rules/verifiable-data.mdc rules 1 + 3):
--   * citation_authority is one of {FEC, HHS-OIG, GSA-SAM, FAR-Council, DOJ,
--     CRS, platform}
--   * citation_section is the specific section identifier
--     (e.g. "11 CFR 101.1", "42 USC 1320a-7(a)", "FAR 9.405")
--   * citation_url points to the federal-domain page that publishes the rule
--     (law.cornell.edu/cfr is acceptable as it mirrors the GPO eCFR)
--   * rule_text is the predicate in plain English (>=20 chars)
--   * plain_english_template is the UI-rendering template with
--     {{placeholder}} tokens (>=30 chars)
--
-- HONESTY NOTE
-- ------------
-- Some signals (e.g., donor_on_leie, donor_on_sam, candidate_funded_by_*)
-- do NOT codify a federal violation per se -- there is no statute that
-- prohibits an LEIE-listed individual from contributing to a campaign.
-- These signals are STRUCTURAL ANOMALIES that surface a pattern worthy of
-- investigation, not a conclusion that a violation occurred. The
-- rule_text and plain_english_template for these signals are written to
-- preserve that distinction explicitly so the UI cannot accidentally
-- frame the entity as a confirmed violator.
--
-- Idempotent via ON CONFLICT DO UPDATE.
-- ============================================================================

INSERT INTO ref.fraud_signal_human_explanation (
    signal_id,
    rule_text,
    citation_authority,
    citation_section,
    citation_url,
    plain_english_template,
    formula_version,
    effective_date
) VALUES

-- ----------------------------------------------------------------------------
-- LEIE-bearing (4 signals)
-- ----------------------------------------------------------------------------
(
    'entity_on_leie',
    'A federal candidate, committee, or treasurer canonical name matches an '
    'individual on the HHS-OIG List of Excluded Individuals/Entities (LEIE), '
    'a federal exclusion list maintained under the Mandatory Exclusion '
    'Authority of 42 USC 1320a-7(a). LEIE listings indicate the individual '
    'has been excluded from federal healthcare program participation '
    'following an enforcement action.',
    'HHS-OIG',
    '42 USC 1320a-7(a)',
    'https://oig.hhs.gov/exclusions/authorities.asp',
    'Entity {{entity_id}} ({{entity_kind}}) canonical name matches an active '
    'LEIE individual exclusion. The match alone is not a finding of campaign-'
    'finance wrongdoing -- LEIE is a healthcare-program exclusion list, not '
    'a campaign-finance prohibition -- but the structural overlap is a '
    'documented pattern in past FEC and DOJ investigations and warrants '
    'review of contribution sources.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'entity_funded_and_excluded',
    'A federal contractor canonical individual name matches an active LEIE '
    'individual exclusion AND received federal contract awards in the cycle '
    '(USAspending NJ-pop). FAR 9.405 prohibits agencies from awarding new '
    'contracts to individuals or firms on a federal exclusion list; '
    'continued payment to an excluded individual is a structural compliance '
    'failure.',
    'FAR-Council',
    'FAR 9.405',
    'https://www.acquisition.gov/far/9.405',
    'Entity {{entity_id}} ({{entity_kind}}) appears on the LEIE active '
    'exclusion list AND received {{raw_value}} of federal contract dollars '
    'in cycle {{cycle}}. FAR 9.405(a) bars contracting with debarred or '
    'suspended parties; this combination indicates a possible procurement-'
    'compliance failure on the federal contracting side.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'donor_on_leie',
    'A federal-campaign donor canonical name (FEC contribution receipts) '
    'matches an active LEIE individual exclusion. There is no federal '
    'statute prohibiting an LEIE-listed individual from contributing to a '
    'campaign; this signal surfaces a structural anomaly that, in past '
    'aggregate analyses, has correlated with downstream enforcement '
    'activity.',
    'platform',
    'platform/donor-overlap-with-leie',
    'https://oig.hhs.gov/exclusions/exclusions_list.asp',
    'Donor canonical name {{entity_id}} matches an active LEIE individual '
    'exclusion AND contributed an aggregate of {{raw_value}} to FEC-'
    'registered committees in cycle {{cycle}}. This is a STRUCTURAL '
    'ANOMALY -- LEIE exclusion is a healthcare-program sanction, not a '
    'campaign-finance prohibition -- and is surfaced for analyst review, '
    'not as a finding of wrongdoing.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_funded_by_excluded_donors',
    'A federal candidate received aggregate contributions from one or more '
    'donors whose canonical names match active LEIE individual exclusions. '
    'Surfaces a structural pattern; does not assert wrongdoing by the '
    'recipient candidate.',
    'platform',
    'platform/candidate-funded-by-leie',
    'https://oig.hhs.gov/exclusions/exclusions_list.asp',
    'Candidate {{entity_id}} received an aggregate of {{raw_value}} '
    '(age-decayed) in contributions from donors whose canonical names match '
    'active LEIE individual exclusions, in cycle {{cycle}}. The candidate is '
    'NOT alleged to have known the donors'' status; this signal surfaces '
    'the receipt-side structural pattern.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),

-- ----------------------------------------------------------------------------
-- SAM-bearing (3 signals)
-- ----------------------------------------------------------------------------
(
    'entity_excluded_via_sam_uei',
    'A federal contractor with an active SAM.gov exclusion (matched by the '
    '12-character Unique Entity ID, the federal procurement primary key) '
    'received federal contract awards in the cycle. Because UEI is unique '
    'by SAM design, this is a UEI-deterministic match -- the same legal '
    'entity, full stop -- and a FAR 9.405 violation if the awarding agency '
    'made the award after the exclusion''s active_date.',
    'GSA-SAM',
    'FAR 9.405 + SAM.gov Exclusions',
    'https://sam.gov/content/exclusions',
    'Entity {{entity_id}} (recipient_uei) is on the SAM.gov active '
    'exclusion list AND received {{raw_value}} of federal contract dollars '
    'in cycle {{cycle}}. SAM exclusion is a federal procurement bar (FAR '
    '9.405); a UEI-deterministic match is not subject to canonicalization '
    'ambiguity, so this rises to operational significance.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'donor_on_sam',
    'A federal-campaign donor canonical name matches an individual on the '
    'SAM.gov active exclusion list. Like donor_on_leie, this is a '
    'structural anomaly and not a campaign-finance prohibition; SAM '
    'exclusion is a procurement-side sanction, not an FEC prohibition.',
    'platform',
    'platform/donor-overlap-with-sam',
    'https://sam.gov/content/exclusions',
    'Donor canonical name {{entity_id}} matches an active SAM.gov '
    'individual exclusion AND contributed an aggregate of {{raw_value}} '
    '(age-decayed) to FEC-registered committees in cycle {{cycle}}. '
    'STRUCTURAL ANOMALY; surfaces a documented overlap pattern for '
    'analyst review.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_funded_by_sam_excluded_donors',
    'A federal candidate received aggregate contributions from one or more '
    'donors whose canonical names match active SAM.gov individual '
    'exclusions. Receipt-side structural pattern, not an allegation of '
    'wrongdoing by the candidate.',
    'platform',
    'platform/candidate-funded-by-sam',
    'https://sam.gov/content/exclusions',
    'Candidate {{entity_id}} received an aggregate of {{raw_value}} '
    '(age-decayed) in contributions from donors whose canonical names '
    'match active SAM.gov individual exclusions, in cycle {{cycle}}. '
    'Receipt-side structural anomaly.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),

-- ----------------------------------------------------------------------------
-- workforce (2 signals)
-- ----------------------------------------------------------------------------
(
    'donor_employed_by_nj_contractor',
    'An individual donor employer-name field clusters with the canonical '
    'name of a New Jersey federal contractor (USAspending recipient). 52 '
    'USC 30119 ("pay-to-play") prohibits federal contractor entities from '
    'making federal political contributions during the contract period; '
    'employees of those contractors are NOT subject to that prohibition '
    'individually, but coordinated employee-level contribution clusters '
    'have triggered FEC enforcement matters under conduit/coordination '
    'theories.',
    'FEC',
    '52 USC 30119 (pay-to-play)',
    'https://www.fec.gov/help-candidates-and-committees/'
    'understanding-ways-support-federal-candidates/contributions/',
    'Donor employer-name cluster {{entity_id}} matches a NJ federal '
    'contractor canonical name; employee donors in this cluster '
    'contributed an aggregate of {{raw_value}} in cycle {{cycle}}. '
    'Employees are NOT directly barred by 52 USC 30119, but coordinated '
    'employee-cluster patterns are documented in past FEC MURs as '
    'conduit / coordinated-contribution risks.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_funded_by_nj_contractor_employees',
    'A federal candidate received aggregate contributions from a cluster '
    'of donors whose employer-name fields canonical-match an NJ federal '
    'contractor. Receipt-side mirror of donor_employed_by_nj_contractor; '
    'surfaces the receiving candidate so the analyst can audit whether '
    'the receipt pattern looks coordinated.',
    'FEC',
    '52 USC 30119 (pay-to-play)',
    'https://www.fec.gov/help-candidates-and-committees/'
    'understanding-ways-support-federal-candidates/contributions/',
    'Candidate {{entity_id}} received an aggregate of {{raw_value}} from '
    'donors whose employer-name fields cluster as NJ federal contractor '
    'employees, in cycle {{cycle}}. Receipt-side structural pattern; '
    'review for coordination signals.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),

-- ----------------------------------------------------------------------------
-- address (1 signal)
-- ----------------------------------------------------------------------------
(
    'committee_address_clusters',
    'Multiple FEC-registered committees declare the same canonical '
    'residential or commercial address. 11 CFR 110.4 prohibits making '
    'contributions in the name of another (the "straw donor" rule); '
    'committee-level address clustering is a documented straw-donor and '
    'shell-committee structural indicator in past FEC enforcement matters.',
    'FEC',
    '11 CFR 110.4',
    'https://www.law.cornell.edu/cfr/text/11/110.4',
    'Address cluster {{entity_id}} ({{peer_bucket}}) hosts {{raw_value}} '
    'distinct FEC-registered committees in cycle {{cycle}}. 11 CFR 110.4 '
    'prohibits straw-donor structures; multi-committee address clustering '
    'is a documented structural indicator in FEC MURs investigating '
    'straw-donor arrangements.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),

-- ----------------------------------------------------------------------------
-- structural (7 signals)
-- ----------------------------------------------------------------------------
(
    'treasurer_concentration',
    'A single treasurer canonical name appears as the registered treasurer '
    'on an unusually high number of FEC-registered committees. 11 CFR '
    '102.7 holds the treasurer personally responsible for committee '
    'reporting accuracy; high concentration is a structural indicator of '
    'either a professional compliance shop (legitimate) or insufficient '
    'individual-treasurer attention to each committee (compliance risk).',
    'FEC',
    '11 CFR 102.7',
    'https://www.law.cornell.edu/cfr/text/11/102.7',
    'Treasurer canonical name {{entity_id}} serves as the registered '
    'treasurer for an unusually high number of FEC-registered committees '
    'in cycle {{cycle}} (concentration index {{raw_value}}, '
    '{{peer_bucket}} peer rank). 11 CFR 102.7 places personal recordkeeping '
    'duties on the treasurer; concentration above the peer 95th percentile '
    'is a documented audit trigger.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_no_pcc',
    'A candidate registered with the FEC has not designated a principal '
    'campaign committee, or has designated a committee ID that is not '
    'present in raw.fec_committee. 11 CFR 101.1(a) requires every '
    'federal candidate to designate a principal campaign committee within '
    '15 days of becoming a candidate.',
    'FEC',
    '11 CFR 101.1(a)',
    'https://www.law.cornell.edu/cfr/text/11/101.1',
    'Candidate {{entity_id}} has not designated a registered principal '
    'campaign committee in cycle {{cycle}}, or the designated committee '
    'is absent from FEC committee registrations. 11 CFR 101.1(a) requires '
    'PCC designation within 15 days of candidacy; a missing or unmatched '
    'PCC is a structural compliance lapse.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_broken_pcc',
    'A candidate has declared a principal campaign committee ID, but that '
    'committee does not list this candidate as its candidate. 11 CFR '
    '101.1(a) and FEC Form 1 require the PCC linkage to be bilateral '
    '(both directions of the candidate <-> PCC link must agree).',
    'FEC',
    '11 CFR 101.1(a)',
    'https://www.law.cornell.edu/cfr/text/11/101.1',
    'Candidate {{entity_id}} declares a PCC, but the declared committee '
    'does not list this candidate as its candidate (raw.fec_candidate.pcc '
    '<-> raw.fec_committee.cand_id mismatch) in cycle {{cycle}}. 11 CFR '
    '101.1 requires the PCC linkage to be bilateral; a broken linkage is '
    'a structural reporting inconsistency.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_multiple_pccs',
    'A candidate has been designated by two or more FEC-registered '
    'committees as the PCC''s candidate, simultaneously. 11 CFR 101.1(a) '
    'permits exactly one principal campaign committee per candidate.',
    'FEC',
    '11 CFR 101.1(a)',
    'https://www.law.cornell.edu/cfr/text/11/101.1',
    'Candidate {{entity_id}} is named as the candidate of {{raw_value}} '
    'distinct FEC-registered committees in cycle {{cycle}}. 11 CFR '
    '101.1(a) permits exactly one PCC per candidate; multiple-PCC '
    'declarations are a structural-reporting inconsistency.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'committee_name_collisions',
    'Two or more FEC-registered committees share the same canonical name '
    'within the cycle. 11 CFR 102.14 requires committee names to be '
    'distinct; the FEC may compel a renaming to disambiguate.',
    'FEC',
    '11 CFR 102.14',
    'https://www.law.cornell.edu/cfr/text/11/102.14',
    'Committee {{entity_id}} shares a canonical name with {{raw_value}} '
    'other FEC-registered committee(s) in cycle {{cycle}}. 11 CFR 102.14 '
    'requires distinct committee names; collisions are a documented '
    'audit trigger.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_namesakes',
    'Two or more federal candidates share the same canonical name within '
    'the same cycle. The FEC publishes guidance distinguishing namesakes '
    'via candidate ID; namesake clusters require careful disambiguation '
    'in any analysis or reporting.',
    'FEC',
    '11 CFR 101.1 + FEC namesake guidance',
    'https://www.fec.gov/data/candidates/',
    'Candidate canonical name {{entity_id}} is shared by {{raw_value}} '
    'distinct FEC-registered candidates in cycle {{cycle}}. Distinguish '
    'via cand_id; this signal flags the cluster for analyst attention so '
    'aggregate metrics can be computed at the correct (cand_id) '
    'granularity rather than the (canonical_name) granularity.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'treasurer_is_candidate',
    'The candidate is also the registered treasurer of their principal '
    'campaign committee. 11 CFR 102.7 permits this configuration but '
    'flags it as a weak internal-controls structure (no separation between '
    'the candidate and the financial-recordkeeping fiduciary). FEC '
    'Advisory Opinion 2009-15 discusses the resulting fiduciary tension.',
    'FEC',
    '11 CFR 102.7 + FEC AO 2009-15',
    'https://www.fec.gov/data/legal/advisory-opinions/2009-15/',
    'Candidate {{entity_id}} is also the registered treasurer of their '
    'PCC in cycle {{cycle}}. 11 CFR 102.7 permits this but FEC Advisory '
    'Opinion 2009-15 documents the resulting fiduciary tension '
    '(no candidate <-> treasurer separation). Structural indicator of '
    'weak internal controls, not a violation per se.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
)

ON CONFLICT (signal_id) DO UPDATE SET
    rule_text              = EXCLUDED.rule_text,
    citation_authority     = EXCLUDED.citation_authority,
    citation_section       = EXCLUDED.citation_section,
    citation_url           = EXCLUDED.citation_url,
    plain_english_template = EXCLUDED.plain_english_template,
    formula_version        = EXCLUDED.formula_version,
    effective_date         = EXCLUDED.effective_date;
