-- ============================================================================
-- Seed: 019_fraud_signal_severity_calibration
--
-- 17 rows, one per fraud signal_id. Each row documents the precedent basis
-- for the severity_level the refresher currently emits, satisfying
-- .cursor/rules/verifiable-data.mdc rules 1 + 4 (severity is reference data
-- with documented precedent, not vibes).
--
-- The severity_level on each row MUST match the SMALLINT literal the
-- corresponding refresher (in db/migrations/05[1-7]_fraud_*.sql,
-- db/migrations/06[2-6]_fraud_*.sql) emits when inserting into
-- derived.fraud_signal_observation. This invariant is pinned by the
-- companion test test_fraud_evidence_substrate.py::TestSeverityMatchesRefresher.
-- A future migration (F4-extension) will invert the dependency by having
-- refreshers READ severity_level from this table; for now this seed is the
-- read-side documentation.
--
-- HONESTY NOTE on calibration_basis = 'empirical_pctile'
-- ------------------------------------------------------
-- Several signals (donor_on_leie, donor_on_sam,
-- candidate_funded_by_excluded_donors, candidate_funded_by_sam_excluded_donors,
-- treasurer_concentration, candidate_no_pcc, candidate_broken_pcc,
-- candidate_multiple_pccs, candidate_namesakes) are assigned their severity
-- on the empirical-percentile basis: no specific FEC MUR or DOJ filing
-- directly motivates the severity, and the substrate-honest framing is
-- "this severity reflects analyst calibration against the historical NJ-
-- cycle anomaly distribution + the family-propagation rule that
-- structurally-related signals share the anchor signal's severity tier."
--
-- Per ref.fraud_signal_severity_calibration column comment, a severity-5
-- signal with calibration_basis = 'empirical_pctile' is permitted but will
-- be flagged in F4-extension as a "high-severity-without-enforcement-
-- precedent" research surface, not a violation. The flag exists so the
-- analyst is reminded that the severity is not directly anchored in an
-- enforcement matter and should be re-validated as labels accumulate.
--
-- HONESTY NOTE on FEC MUR citations
-- ---------------------------------
-- This seed cites the FEC's MUR search portal (https://www.fec.gov/data/
-- legal/matter-under-review/) for FEC-MUR-basis rows rather than specific
-- MUR numbers, because:
--   1. MUR-by-MUR linkage requires a manual research review the platform
--      has not yet undertaken at scale.
--   2. The FEC search portal is the authoritative entry point for MUR
--      research and is more durable than any single MUR URL.
--   3. precedent_summary describes the relevant pattern and section
--      so an analyst can search the portal directly.
-- A future enrichment can replace the portal URL with a specific MUR
-- citation as the platform's MUR-research substrate matures.
--
-- Idempotent via ON CONFLICT DO UPDATE.
-- ============================================================================

INSERT INTO ref.fraud_signal_severity_calibration (
    signal_id,
    severity_level,
    calibration_basis,
    precedent_url,
    precedent_summary,
    formula_version,
    effective_date
) VALUES

-- ----------------------------------------------------------------------------
-- LEIE-bearing
-- ----------------------------------------------------------------------------
(
    'entity_on_leie',
    5,
    'oig_report',
    'https://oig.hhs.gov/exclusions/files/sai_supplement_archive.asp',
    'An active LEIE listing is itself a published federal enforcement '
    'finding under 42 USC 1320a-7(a) -- the listed individual has been '
    'excluded from federal healthcare program participation following an '
    'OIG investigation. A canonical-name match between an FEC-registered '
    'entity and an active LEIE listing rises to severity 5 (CRITICAL) '
    'because the LEIE listing carries its own federal-finding weight; '
    'the analyst does not need to develop a separate enforcement '
    'rationale for the underlying exclusion.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'entity_funded_and_excluded',
    5,
    'far_authority',
    'https://www.acquisition.gov/far/9.405',
    'FAR 9.405(a) bars federal agencies from awarding new contracts to '
    'parties on a federal exclusion list (LEIE is one of the recognized '
    'lists). Receipt of federal contract dollars by an LEIE-listed '
    'individual after the active_date is therefore a procurement-side '
    'compliance failure on the awarding agency''s part. Severity 5 '
    '(CRITICAL) is anchored to the explicit FAR prohibition.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'donor_on_leie',
    5,
    'empirical_pctile',
    'https://oig.hhs.gov/exclusions/exclusions_list.asp',
    'No federal statute prohibits an LEIE-listed individual from making '
    'federal political contributions. Severity 5 here propagates from '
    'the leie_bearing signal_family -- the family''s anchor signal '
    '(entity_on_leie) carries an OIG-finding precedent, and downstream '
    'family members inherit that severity tier under the platform''s '
    'analyst-judgment family-propagation rule. The empirical-percentile '
    'basis reflects that the SEVERITY is not directly anchored to a '
    'campaign-finance enforcement matter, only to the structural overlap '
    'rate; this is a high-severity-without-direct-enforcement-precedent '
    'research surface, flagged for re-validation as L5 labels accumulate.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_funded_by_excluded_donors',
    5,
    'empirical_pctile',
    'https://oig.hhs.gov/exclusions/exclusions_list.asp',
    'Receipt-side mirror of donor_on_leie. Same family-propagation rationale: '
    'severity 5 inherited from the leie_bearing family''s anchor signal. '
    'Same caveat: this is a structural-overlap-rate calibration, not a '
    'campaign-finance enforcement precedent. Re-validate as labels '
    'accumulate.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),

-- ----------------------------------------------------------------------------
-- SAM-bearing
-- ----------------------------------------------------------------------------
(
    'entity_excluded_via_sam_uei',
    5,
    'far_authority',
    'https://www.acquisition.gov/far/9.405',
    'A UEI-deterministic match between an active USAspending contract '
    'recipient and an active SAM.gov exclusion is a FAR 9.405(a) '
    'violation -- agencies are explicitly barred from awarding new '
    'contracts to SAM-listed parties. Because UEI is unique by SAM '
    'design, the match is not subject to canonicalization ambiguity. '
    'Severity 5 (CRITICAL) anchors directly to the FAR prohibition.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'donor_on_sam',
    5,
    'empirical_pctile',
    'https://sam.gov/content/exclusions',
    'SAM.gov exclusion is a procurement-side bar; no campaign-finance '
    'statute prohibits SAM-listed individuals from contributing. Severity '
    '5 propagates from the sam_bearing family''s anchor signal '
    '(entity_excluded_via_sam_uei) under the family-propagation rule. '
    'High-severity-without-direct-enforcement-precedent surface; re-'
    'validate as L5 labels accumulate.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_funded_by_sam_excluded_donors',
    5,
    'empirical_pctile',
    'https://sam.gov/content/exclusions',
    'Receipt-side mirror of donor_on_sam. Same family-propagation '
    'rationale; same caveat.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),

-- ----------------------------------------------------------------------------
-- workforce
-- ----------------------------------------------------------------------------
(
    'donor_employed_by_nj_contractor',
    3,
    'fec_mur',
    'https://www.fec.gov/data/legal/matter-under-review/',
    '52 USC 30119 prohibits federal contractor entities from making '
    'federal political contributions during the contract period. '
    'Employees of those contractors are NOT directly subject to the '
    'prohibition, but coordinated employee-cluster contribution patterns '
    'have triggered FEC enforcement matters under conduit / coordinated-'
    'contribution theories (search the FEC MUR portal for "contractor '
    'employee" + "coordinated"). Severity 3 reflects the structural '
    'pattern''s indirectness vs. an LEIE / SAM finding -- the signal '
    'identifies a coordination risk, not a confirmed violation.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_funded_by_nj_contractor_employees',
    3,
    'fec_mur',
    'https://www.fec.gov/data/legal/matter-under-review/',
    'Receipt-side mirror of donor_employed_by_nj_contractor. Same MUR-'
    'portal precedent; severity 3 propagates from the workforce family''s '
    'anchor.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),

-- ----------------------------------------------------------------------------
-- address
-- ----------------------------------------------------------------------------
(
    'committee_address_clusters',
    4,
    'fec_mur',
    'https://www.fec.gov/data/legal/matter-under-review/',
    '11 CFR 110.4 prohibits making contributions in the name of another '
    '(the "straw donor" rule). FEC has multiple Matters Under Review '
    'investigating shell-committee structures where multiple FEC-'
    'registered committees declared the same residential or commercial '
    'address (search the MUR portal for "straw donor" + "shell '
    'committee"). Severity 4 reflects that the address-cluster pattern '
    'is a strong indicator without being a confirmed violation; '
    'investigation typically requires bank-record subpoenas the platform '
    'cannot perform.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),

-- ----------------------------------------------------------------------------
-- structural
-- ----------------------------------------------------------------------------
(
    'treasurer_concentration',
    3,
    'empirical_pctile',
    'https://www.law.cornell.edu/cfr/text/11/102.7',
    '11 CFR 102.7 places personal recordkeeping responsibility on the '
    'committee treasurer. Concentration above the peer 95th percentile '
    'is a structural-anomaly indicator: it identifies either a '
    'professional compliance shop (legitimate) or insufficient '
    'individual-treasurer attention to each committee (compliance risk). '
    'Severity 3 reflects the bivalent interpretation -- the signal does '
    'not distinguish the two cases, and an analyst review is needed.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_no_pcc',
    1,
    'empirical_pctile',
    'https://www.law.cornell.edu/cfr/text/11/101.1',
    '11 CFR 101.1(a) requires every federal candidate to designate a '
    'principal campaign committee within 15 days of becoming a candidate. '
    'Late designation is routinely processed administratively; severity '
    '1 reflects that this is most often a paperwork lag, not an '
    'enforcement matter. The signal exists for completeness so analysts '
    'can identify candidates whose paperwork may be incomplete during a '
    'cycle, not as a fraud red flag.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_broken_pcc',
    2,
    'empirical_pctile',
    'https://www.law.cornell.edu/cfr/text/11/101.1',
    '11 CFR 101.1(a) requires the PCC linkage to be bilateral. A broken '
    'linkage is a reporting inconsistency that may indicate either a '
    'data-entry error (most common) or an intentional registration '
    'evasion (rare). Severity 2 reflects that the inconsistency is '
    'usually administrative.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_multiple_pccs',
    2,
    'empirical_pctile',
    'https://www.law.cornell.edu/cfr/text/11/101.1',
    '11 CFR 101.1(a) permits exactly one PCC per candidate. Multi-PCC '
    'declarations are most often a stale-registration artifact (a prior '
    'cycle''s committee was not properly terminated). Severity 2 reflects '
    'that this is usually administrative cleanup, not fraud.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'committee_name_collisions',
    3,
    'fec_advisory',
    'https://www.law.cornell.edu/cfr/text/11/102.14',
    '11 CFR 102.14 requires committee names to be distinct. The FEC has '
    'historically compelled name changes when collisions create donor / '
    'voter confusion; AOs and Notices of Hearing in this area are '
    'documented in the FEC''s legal-resource catalog. Severity 3 reflects '
    'the disambiguation duty, which the platform itself cannot enforce '
    'but should surface for analyst attention.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'candidate_namesakes',
    3,
    'empirical_pctile',
    'https://www.fec.gov/data/candidates/',
    'Multiple candidates with the same canonical name in the same cycle '
    'is a metadata-disambiguation flag, not a violation. The FEC '
    'distinguishes namesakes via cand_id; the platform surfaces this '
    'cluster so aggregate analysis is computed at (cand_id) granularity '
    'rather than (canonical_name) granularity. Severity 3 reflects that '
    'this is a measurement-integrity concern, not a fraud indicator -- '
    'but unaddressed it can corrupt downstream rollups.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
),
(
    'treasurer_is_candidate',
    1,
    'fec_advisory',
    'https://www.fec.gov/data/legal/advisory-opinions/2009-15/',
    '11 CFR 102.7 permits the candidate to serve as their own PCC '
    'treasurer, and FEC Advisory Opinion 2009-15 explicitly addresses '
    'the configuration. Severity 1 reflects that the configuration is '
    'allowed -- it is a structural weak-internal-controls indicator, not '
    'a violation, and is common in small / first-time campaigns.',
    '2.1.0-fraud-evidence-substrate-v1',
    '2026-05-08'
)

ON CONFLICT (signal_id) DO UPDATE SET
    severity_level    = EXCLUDED.severity_level,
    calibration_basis = EXCLUDED.calibration_basis,
    precedent_url     = EXCLUDED.precedent_url,
    precedent_summary = EXCLUDED.precedent_summary,
    formula_version   = EXCLUDED.formula_version,
    effective_date    = EXCLUDED.effective_date;
