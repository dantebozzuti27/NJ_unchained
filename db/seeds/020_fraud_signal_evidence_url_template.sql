-- ============================================================================
-- Seed: 020_fraud_signal_evidence_url_template
--
-- One row per fraud signal_id (17 total) wiring the upstream-verify URL the
-- UI renders as the "Verify on FEC.gov" / "Verify on OIG.gov" / "Verify on
-- SAM.gov" button on each evidence card.
--
-- Substitution discipline:
--   * {{entity_id}} substitutes the firing-row's entity_id verbatim
--   * {{cycle}} substitutes the firing-row's cycle (CHAR(4))
--   * Templates that need URL-encoding (treasurer names with commas + spaces)
--     are pre-encoded here -- the comma "%2C" + space "%20" / "+" form is
--     stable across browsers and matches FEC.gov's own search-result links.
--
-- Coverage:
--   * 8 FEC-active signals (all firing today after F1 ingest):
--       candidate_no_pcc, candidate_broken_pcc, candidate_multiple_pccs,
--       candidate_namesakes, committee_address_clusters,
--       committee_name_collisions, treasurer_concentration,
--       treasurer_is_candidate
--   * 3 LEIE-bearing signals (deferred to F8 ingest):
--       entity_on_leie, donor_on_leie, candidate_funded_by_excluded_donors
--   * 3 SAM-bearing signals (deferred to F8 ingest):
--       entity_excluded_via_sam_uei, donor_on_sam,
--       candidate_funded_by_sam_excluded_donors
--   * 2 USAspending-bearing signals (deferred to F8 ingest):
--       entity_funded_and_excluded, candidate_funded_by_nj_contractor_employees
--   * 1 NJ-contractor-graph signal (deferred to F8 ingest):
--       donor_employed_by_nj_contractor
--
-- Idempotent via ON CONFLICT DO UPDATE.
-- ============================================================================

INSERT INTO ref.fraud_signal_evidence_url_template (
    signal_id,
    url_template,
    button_label,
    upstream_source,
    formula_version,
    effective_date
) VALUES

-- ----------------------------------------------------------------------------
-- FEC-active signals (firing today)
-- ----------------------------------------------------------------------------
(   'candidate_no_pcc',
    'https://www.fec.gov/data/candidate/{{entity_id}}/?cycle={{cycle}}',
    'Verify on FEC.gov',
    'FEC.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

(   'candidate_broken_pcc',
    'https://www.fec.gov/data/candidate/{{entity_id}}/?cycle={{cycle}}',
    'Verify on FEC.gov',
    'FEC.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

(   'candidate_multiple_pccs',
    'https://www.fec.gov/data/candidate/{{entity_id}}/?cycle={{cycle}}',
    'Verify on FEC.gov',
    'FEC.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

-- candidate_namesakes: link to the FEC candidate-search page filtered by
-- candidate ID. Analyst clicks through to see the namesake collisions.
(   'candidate_namesakes',
    'https://www.fec.gov/data/candidate/{{entity_id}}/?cycle={{cycle}}',
    'Verify on FEC.gov',
    'FEC.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

(   'committee_name_collisions',
    'https://www.fec.gov/data/committee/{{entity_id}}/?cycle={{cycle}}',
    'Verify on FEC.gov',
    'FEC.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

-- committee_address_clusters: entity_id is composite (address|city|state|zip5),
-- not a cmte_id, so we cannot template entity_id into FEC's committee-detail
-- URL. Link to the FEC committee search page filtered by cycle; the analyst
-- selects the relevant cluster from the platform card.
(   'committee_address_clusters',
    'https://www.fec.gov/data/committees/?cycle={{cycle}}',
    'Browse FEC.gov committees',
    'FEC.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

-- treasurer_concentration: entity_id is the treasurer's canonical name. Link
-- to FEC.gov committee search filtered by treasurer_name. URL-encoding for
-- comma + space happens at the UI layer (encodeURIComponent).
(   'treasurer_concentration',
    'https://www.fec.gov/data/committees/?treasurer_name={{entity_id}}&cycle={{cycle}}',
    'Verify on FEC.gov',
    'FEC.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

(   'treasurer_is_candidate',
    'https://www.fec.gov/data/committee/{{entity_id}}/?cycle={{cycle}}',
    'Verify on FEC.gov',
    'FEC.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

-- ----------------------------------------------------------------------------
-- LEIE-bearing signals (deferred to F8 ingest -- urls present so the
-- substrate is complete the moment observations start emitting)
-- ----------------------------------------------------------------------------
(   'entity_on_leie',
    'https://oig.hhs.gov/exclusions/exclusions_list.asp',
    'Search OIG LEIE',
    'OIG.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

(   'donor_on_leie',
    'https://oig.hhs.gov/exclusions/exclusions_list.asp',
    'Search OIG LEIE',
    'OIG.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

(   'candidate_funded_by_excluded_donors',
    'https://www.fec.gov/data/candidate/{{entity_id}}/?cycle={{cycle}}',
    'Verify candidate on FEC.gov',
    'FEC.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

-- ----------------------------------------------------------------------------
-- SAM-bearing signals (deferred to F8 ingest)
-- ----------------------------------------------------------------------------
(   'entity_excluded_via_sam_uei',
    'https://sam.gov/content/exclusions',
    'Search SAM.gov exclusions',
    'SAM.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

(   'donor_on_sam',
    'https://sam.gov/content/exclusions',
    'Search SAM.gov exclusions',
    'SAM.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

(   'candidate_funded_by_sam_excluded_donors',
    'https://www.fec.gov/data/candidate/{{entity_id}}/?cycle={{cycle}}',
    'Verify candidate on FEC.gov',
    'FEC.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

-- ----------------------------------------------------------------------------
-- USAspending-bearing signals (deferred to F8 ingest)
-- ----------------------------------------------------------------------------
(   'entity_funded_and_excluded',
    'https://www.usaspending.gov/search?keywords={{entity_id}}',
    'Search USAspending.gov',
    'USAspending.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

(   'candidate_funded_by_nj_contractor_employees',
    'https://www.fec.gov/data/candidate/{{entity_id}}/?cycle={{cycle}}',
    'Verify candidate on FEC.gov',
    'FEC.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08'),

-- ----------------------------------------------------------------------------
-- NJ-contractor-graph signal (deferred to F8 ingest)
-- ----------------------------------------------------------------------------
(   'donor_employed_by_nj_contractor',
    'https://www.usaspending.gov/search?keywords=NJ&award_type_codes=A%2CB%2CC%2CD',
    'Browse NJ contractors on USAspending.gov',
    'USAspending.gov',
    '2.2.0-fraud-evidence-view-v1',
    '2026-05-08')

ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date;
