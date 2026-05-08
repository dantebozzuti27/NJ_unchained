-- ============================================================================
-- Seed: 014_platform_constants
--
-- VISION_2026.md §3 punch-list: pay down the BURDEN_BASE_YEAR magic-number
-- debt by registering it in ref.platform_constants. Same value (2010);
-- now traceable to a citation and a formula version.
-- ============================================================================

INSERT INTO ref.platform_constants
    (constant_id, value, description, source_url, citation_text,
     formula_version, effective_date, notes)
VALUES
    ('burden_base_year', 2010, 
     'Re-index base year for FHFA HPI and ACS5 real-income series in the '
     'housing burden classifier. The HPI(year) / HPI(base) and real_income(year) '
     '/ real_income(base) ratios both pivot on this year.',
     'https://www.fhfa.gov/data/hpi',
     'NJ-platform internal: chosen as the first year where ALL THREE substrates '
     '(FHFA HPI county-level, ACS5 county-level B19013, and BLS CPI-U annual M13) '
     'have full coverage for every NJ county. Earlier years (2009 and below) lack '
     'ACS5 coverage in lower-population NJ counties. Spec §3.4 calls for a 2026 '
     'rebase eventually; we use 2010 today and revise when CPI-U 2026 (M13) and '
     'ACS5 2026-vintage are both published. The 2010 anchor is the substrate-honest '
     'choice; nothing about it is "industry standard". See also '
     'derived.f_fhfa_hpi_indexed(2010) and derived.f_acs_mhi_real(2010) which '
     'pin the same year.',
     '1.7.0-platform-constants-v1',
     '2026-05-08'::DATE,
     'Replaces the lib/housing.ts BURDEN_BASE_YEAR = 2010 literal.'),

    ('cross_source_base_year', 2010,
     'Re-index base year for the FHFA-vs-ZHVI cross-source housing-index '
     'divergence function (derived.f_housing_index_cross_source). Both indices '
     'are normalized to base = 100 at this year so the divergence_pct_of_fhfa '
     'is a comparable signed measure across counties.',
     'https://www.fhfa.gov/data/hpi',
     'NJ-platform internal: chosen to match burden_base_year (2010) so that the '
     'cross-source housing-index plot uses the same x-axis anchor as the burden '
     'ratio chart. This makes the two surfaces comparable without a base-year '
     'mental conversion. Phase 7 asset checks (housing_index_cross_source_'
     'divergence_plausible) use this constant via inline 2010::SMALLINT today; '
     'a future Phase 7d migration will refactor to a CTE that reads this row.',
     '1.7.0-platform-constants-v1',
     '2026-05-08'::DATE,
     NULL)
ON CONFLICT (constant_id, formula_version) DO UPDATE SET
    value          = EXCLUDED.value,
    description    = EXCLUDED.description,
    source_url     = EXCLUDED.source_url,
    citation_text  = EXCLUDED.citation_text,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;
