-- ============================================================================
-- Seed: 012_affordability_assumptions
--
-- Tier-A assumption constants for Phase 2 (PITI / required-income /
-- affordability-gap) and Phase 4 (personalized affordability engine).
-- Every value here is hand-cited; nothing is a "rule of thumb" without
-- a publishable source backing it.
--
-- Idempotent under re-run via ON CONFLICT DO UPDATE.
-- ============================================================================

INSERT INTO ref.affordability_assumptions
    (constant_id, effective_year, value_numeric, unit,
     source_url, source_citation, note)
VALUES

    -- ------------------------------------------------------------------
    -- Mortgage-equivalent calculation (idea spec §5.1)
    -- ------------------------------------------------------------------
    ('mortgage_default_down_pct', 0, 0.20000000, 'fraction',
     'https://www.fanniemae.com/singlefamily/eligibility',
     'idea spec §5.1; Fannie Mae conforming-loan standard for full conventional rate without PMI',
     '20% down avoids PMI under HOPA. Higher down (25-30%) is common in NJ HCOL counties; lower (3-5%) is FHA territory. The personalization engine accepts user override.'),

    ('mortgage_default_term_years', 0, 30, 'years',
     'https://www.fanniemae.com/singlefamily/originating-underwriting',
     'idea spec §5.1; Fannie Mae conforming-loan most-common amortization term',
     '30-yr fixed is the default. 15-yr fixed is the next most common; the personalization engine accepts {15, 30}.'),

    -- ------------------------------------------------------------------
    -- Insurance (the assumption with the widest variance and weakest
    -- single source; flag this in the UI when surfacing)
    -- ------------------------------------------------------------------
    ('homeowners_insurance_annual_rate_default', 0, 0.00350000, 'fraction',
     'https://content.naic.org/sites/default/files/publication-hmr-zu-homeowners-report.pdf',
     'NAIC Homeowners Insurance Report (national average); see also Insurance Information Institute industry data',
     'NJ specifically averages ~0.30-0.40% of home value annually. County-level granularity (coastal vs inland flood/wind exposure) is a future enhancement.'),

    -- ------------------------------------------------------------------
    -- Affordability threshold (idea spec §5.4 -- the "headline" %)
    -- ------------------------------------------------------------------
    ('affordability_threshold_pct', 0, 0.30000000, 'fraction',
     'https://www.huduser.gov/portal/datasets/cp.html',
     'HUD-PD&R definition of cost-burdened household: housing costs >30% of gross income; basis of HUD CHAS dataset',
     'idea spec §5.4: required income at 30% = (housing+taxes)/0.30. Severe burden threshold is 50% (separately seeded if needed).'),

    ('affordability_severe_burden_pct', 0, 0.50000000, 'fraction',
     'https://www.huduser.gov/portal/datasets/cp.html',
     'HUD-PD&R severe-burden threshold (HUD CHAS dataset)',
     'For traffic-light bands: <30% unburdened, 30-50% burdened, >=50% severely burdened.'),

    -- ------------------------------------------------------------------
    -- DTI underwriting standards (Phase 4 personalization engine)
    -- ------------------------------------------------------------------
    ('dti_front_end_cap_conventional', 0, 0.28000000, 'fraction',
     'https://selling-guide.fanniemae.com/Selling-Guide/Origination-thru-Closing/Subpart-B3-Underwriting-Borrowers/',
     'Fannie Mae Selling Guide B3-6: housing payment <=28% of stable monthly income (conventional underwriting)',
     '28% is the historical thumb; modern Fannie automated underwriting (DU) routinely allows higher with compensating factors. Used as conservative default.'),

    ('dti_back_end_cap_conventional', 0, 0.36000000, 'fraction',
     'https://selling-guide.fanniemae.com/Selling-Guide/Origination-thru-Closing/Subpart-B3-Underwriting-Borrowers/',
     'Fannie Mae Selling Guide B3-6: total monthly debt <=36% of stable monthly income (conventional)',
     '36% back-end is the conservative default. CFPB QM rule allows up to 43% (separately seeded).'),

    ('dti_back_end_cap_qm_rule', 2014, 0.43000000, 'fraction',
     'https://www.consumerfinance.gov/rules-policy/regulations/1026/43/',
     'CFPB Reg Z 1026.43(e)(2)(vi) Qualified Mortgage standard, effective 2014-01-10',
     'QM rule replaced the prior thumbs-only standard. Higher DTI loans can still be made but lose QM safe harbor.'),

    -- ------------------------------------------------------------------
    -- PMI (relevant only when down_pct < 20%)
    -- ------------------------------------------------------------------
    ('pmi_threshold_ltv', 0, 0.80000000, 'fraction',
     'https://www.law.cornell.edu/uscode/text/12/4901',
     'Homeowners Protection Act of 1998 (12 USC §4901): PMI termination at 78% LTV, requestable at 80% LTV',
     'Loans with original LTV > 80% require PMI until loan amortizes below threshold. Phase-2 V1 assumes 20% down (no PMI); Phase-4 personalization will model PMI when user_down_pct < 20%.'),

    ('pmi_annual_rate_default', 0, 0.00500000, 'fraction',
     'https://www.urban.org/research/publication/private-mortgage-insurance-overview',
     'Urban Institute Housing Finance at a Glance: average PMI premium 0.46-0.55% of loan amount annually',
     'Range 0.20-1.50% depending on credit score + LTV; 0.50% is the rough mid-range default. NOT used in Phase 2 V1 (20% down assumed).'),

    -- ------------------------------------------------------------------
    -- HOA / condo fees (currently unmodeled; placeholder for Phase 4)
    -- ------------------------------------------------------------------
    ('hoa_monthly_default', 0, 0, 'dollars',
     'https://www.census.gov/programs-surveys/ahs.html',
     'American Housing Survey: 28% of NJ owner units have HOA/condo fees, median ~$300/month for those that do',
     'Defaulted to $0 because most single-family detached do not have HOA. Phase-4 personalization should accept user override.'),

    -- ------------------------------------------------------------------
    -- Real-dollar baseline (idea spec §3.4)
    -- ------------------------------------------------------------------
    -- idea spec §3.4: "all values converted to 2026 real dollars
    -- baseline". We seed this as a constant so any code that
    -- references it (frontend, the burden ratio queries) reads the
    -- same source-of-truth.
    ('cpi_baseline_year', 0, 2026, 'count',
     'https://github.com/dantebozzuti27/NJ_unchained/blob/main/idea',
     'idea spec §3.4: convert all real values to 2026 dollars baseline',
     'When 2026 CPI-U is published (December 2026 by BLS), update derived.f_acs_mhi_real defaults. Until then, the latest-available CPI year is the de facto baseline (currently 2024).')

ON CONFLICT (constant_id, effective_year) DO UPDATE SET
    value_numeric   = EXCLUDED.value_numeric,
    unit            = EXCLUDED.unit,
    source_url      = EXCLUDED.source_url,
    source_citation = EXCLUDED.source_citation,
    note            = EXCLUDED.note;
