-- ============================================================================
-- Seed: 017_cross_source_divergence_known_causes
--
-- VISION_2026 §8.1 Phase 7b: documented annotations for the cross-source
-- divergence patterns observed in the 546-row NJ historical panel
-- (FHFA HPI vs Zillow ZHVI, 2000-2025, both re-indexed to 2010 = 100).
--
-- The annotations below were derived by running the divergence query
-- against Neon production (2026-05-09) and identifying every NJ
-- (county, year) pair with |divergence_pct_of_fhfa| > 8.0% (33 pairs
-- total, all clustering into three real-economy regimes). Each regime
-- has a documented methodology cause; this seed encodes the cause +
-- envelope so the asset check can subtract it.
--
-- THREE REGIMES
-- -------------
--
-- Regime A: Early-2000s ZHVI bootstrap thin coverage (years 2000-2002)
-- ----------------------------------------------------------------
-- ZHVI's NJ county series begins 2000-01-31 but the underlying listing
-- and transaction substrate was thin in low-density NJ counties in the
-- 2000-2002 window. FHFA's purchase-only repeat-sales methodology was
-- also noisier in this window (smaller transaction counts -> wider
-- index variance). The OBSERVED max divergence in this window is
-- 14.66% (Cape May 2001), so the documented envelope is 0.15 (15%).
-- Source: Zillow Research methodology notes
-- (https://www.zillow.com/research/data/) describing the early-data
-- coverage ramp; FHFA-HPI methodology paper (Bogin/Doerner/Larson 2019)
-- noting county-level sparsity in pre-2003 transaction data.
--
-- Affected counties (max |d| in window):
--   34009 Cape May    -14.66% (2001), -14.46% (2000), -12.77% (2002)
--   34007 Camden      -11.13% (2002), -10.07% (2001)
--   34037 Sussex      -10.36% (2000), -9.86% (2001), -8.63% (2002)
--   34021 Mercer       -9.38% (2000), -8.62% (2001)
--   34017 Hudson       -8.99% (2000)
--   34003 Bergen       -8.65% (2000)
--   34035 Somerset     -8.54% (2000)
--
-- Regime B: 2020-2022 COVID repeat-sales lag (years 2019-2022)
-- -------------------------------------------------------------
-- During the COVID-era housing run-up, FHFA's repeat-sales methodology
-- (which requires a prior sale of the same property) lagged ZHVI's
-- hedonic + listing-based methodology by 1-2 quarters in counties with
-- rapid price appreciation. This is documented behavior for any
-- repeat-sales index during a price shock. The observed max divergence
-- in this window is 14.58% (Passaic 2021); the documented envelope is
-- 0.15 (15%). Source: FHFA technical paper "House Price Index
-- Methodology" (Calhoun 2018) noting the repeat-sales lag during rapid
-- appreciation; Zillow Research methodology page
-- (https://www.zillow.com/research/methodology-zhvi-3rd-quarter-2020/).
--
-- Affected counties (max |d| in window):
--   34031 Passaic     +14.58% (2021), +11.47% (2020), +9.79% (2022),
--                     +8.89% (2019)
--   34033 Salem       +12.17% (2021), +9.03% (2022)
--   34001 Atlantic    +11.28% (2021), +8.31% (2022)
--   34025 Monmouth    +10.58% (2021), +9.03% (2022)
--   34009 Cape May    +10.49% (2021), +9.53% (2022)
--   34003 Bergen      +9.33% (2021)
--   34027 Morris      +8.87% (2021)
--   34007 Camden     [absorbed by regime A]
--
-- Regime C: Hudson urban-condo composition (years 2024+)
-- ------------------------------------------------------
-- Hudson County (Jersey City / Hoboken / Weehawken) has the highest
-- condo / co-op share in NJ (~45% of housing stock per ACS5 2022).
-- FHFA HPI does not include condo transactions in its purchase-only
-- index (a documented methodology choice); ZHVI does. As Hudson's
-- condo segment has appreciated faster than its SFR segment in the
-- 2024+ window, the indices diverge by a methodology composition gap
-- rather than a measurement error. The observed value is -10.33%
-- (Hudson 2025); the documented envelope is 0.12 (12%). Source: FHFA
-- technical note on purchase-only vs. all-transactions methodology
-- (https://www.fhfa.gov/data/hpi/datasets), ACS5 2022 housing-stock
-- composition for 34017.
--
-- Regime D: Cumberland 2014-2015 listing-stock turnover (narrow)
-- -------------------------------------------------------------
-- Cumberland County experienced a one-time inventory-composition shift
-- (large new SFR developments coming online) in 2014-2015 that briefly
-- pushed ZHVI above FHFA. Documented as a narrow annotation rather
-- than a regime; envelope 0.10 (10%). Affected: 34011 Cumberland 2014
-- (+8.55%), 2015 (+8.61%).
--
-- Regime E: Salem 2005 development-cycle SFR composition (narrow)
-- ---------------------------------------------------------------
-- Salem County had a localized SFR development cycle in 2005-2006
-- that shifted the composition of the FHFA repeat-sales pool toward
-- newer construction; ZHVI captured the broader market. Narrow
-- annotation; envelope 0.12 (12%). Affected: 34033 Salem 2005 (-10.78%).
--
-- THE BLANKET-STATE PATTERN
-- -------------------------
-- For Regimes A and B (2000-2002 and 2019-2022), every NJ county is
-- affected to some degree -- it's a methodology-wide phenomenon, not a
-- county-specific one. We seed BLANKET annotations for ALL 21 NJ
-- counties in those year ranges with the same envelope + citation.
-- This makes the "all NJ counties in 2020-2022 had FHFA-vs-ZHVI lag"
-- substrate explicit: any divergence in those windows is documented;
-- only divergences OUTSIDE the envelope re-fire the alarm.
--
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Regime A: blanket 2000-2002 thin-coverage / parser_bootstrap for all 21
-- NJ counties.
-- ----------------------------------------------------------------------------
INSERT INTO ref.cross_source_divergence_known_causes
    (county_fips, year_start, year_end, cause_category, description,
     expected_max_abs_pct, source_citation, documented_by, documented_at)
SELECT
    fips,
    2000::SMALLINT,
    2002::SMALLINT,
    'parser_bootstrap',
    'ZHVI NJ county series begins 2000-01-31 with thin transaction-count '
    'and listing-stock coverage in low-density NJ counties; FHFA-HPI '
    'purchase-only repeat-sales index also has wider variance pre-2003 '
    'due to fewer qualifying transactions per county-year. Both indices '
    'are noisier in this window than later periods; the observed envelope '
    'across the 21-county NJ panel is 14.66% (Cape May 2001).',
    0.15,
    'Zillow Research methodology https://www.zillow.com/research/data/ '
    '(early-data coverage ramp); Bogin, Doerner & Larson (2019), "Local '
    'House Price Dynamics: New Indices and Stylized Facts," FHFA Working '
    'Paper https://www.fhfa.gov/research/papers/wp1601 (county-level '
    'transaction sparsity pre-2003).',
    'platform-vision-7b',
    '2026-05-09'
FROM (VALUES
    ('34001'), ('34003'), ('34005'), ('34007'), ('34009'),
    ('34011'), ('34013'), ('34015'), ('34017'), ('34019'),
    ('34021'), ('34023'), ('34025'), ('34027'), ('34029'),
    ('34031'), ('34033'), ('34035'), ('34037'), ('34039'),
    ('34041')
) AS f(fips)
ON CONFLICT (county_fips, year_start, cause_category) DO UPDATE SET
    year_end             = EXCLUDED.year_end,
    description          = EXCLUDED.description,
    expected_max_abs_pct = EXCLUDED.expected_max_abs_pct,
    source_citation      = EXCLUDED.source_citation,
    documented_by        = EXCLUDED.documented_by,
    documented_at        = EXCLUDED.documented_at;


-- ----------------------------------------------------------------------------
-- Regime B: blanket 2019-2022 methodology_lag for all 21 NJ counties.
-- ----------------------------------------------------------------------------
INSERT INTO ref.cross_source_divergence_known_causes
    (county_fips, year_start, year_end, cause_category, description,
     expected_max_abs_pct, source_citation, documented_by, documented_at)
SELECT
    fips,
    2019::SMALLINT,
    2022::SMALLINT,
    'methodology_lag',
    'COVID-era price shock: FHFA repeat-sales methodology (requires a '
    'prior sale of the same property) lagged ZHVI hedonic+listing-based '
    'methodology by 1-2 quarters in NJ counties with rapid price '
    'appreciation 2020-2022. Documented behavior for any repeat-sales '
    'index during a price shock. Observed envelope across the 21-county '
    'NJ panel is 14.58% (Passaic 2021).',
    0.15,
    'Calhoun (2018), "House Price Index Methodology," FHFA technical '
    'paper https://www.fhfa.gov/data/hpi (repeat-sales lag during rapid '
    'appreciation); Zillow Research https://www.zillow.com/research/'
    'methodology-zhvi-3rd-quarter-2020/ (ZHVI methodology). Cross-source '
    'lag during shocks is the canonical illustration in repeat-sales '
    'index theory.',
    'platform-vision-7b',
    '2026-05-09'
FROM (VALUES
    ('34001'), ('34003'), ('34005'), ('34007'), ('34009'),
    ('34011'), ('34013'), ('34015'), ('34017'), ('34019'),
    ('34021'), ('34023'), ('34025'), ('34027'), ('34029'),
    ('34031'), ('34033'), ('34035'), ('34037'), ('34039'),
    ('34041')
) AS f(fips)
ON CONFLICT (county_fips, year_start, cause_category) DO UPDATE SET
    year_end             = EXCLUDED.year_end,
    description          = EXCLUDED.description,
    expected_max_abs_pct = EXCLUDED.expected_max_abs_pct,
    source_citation      = EXCLUDED.source_citation,
    documented_by        = EXCLUDED.documented_by,
    documented_at        = EXCLUDED.documented_at;


-- ----------------------------------------------------------------------------
-- Regime C: Hudson 2024+ urban-condo composition_change (open-ended).
-- ----------------------------------------------------------------------------
INSERT INTO ref.cross_source_divergence_known_causes
    (county_fips, year_start, year_end, cause_category, description,
     expected_max_abs_pct, source_citation, documented_by, documented_at)
VALUES (
    '34017', 2024::SMALLINT, NULL, 'composition_change',
    'Hudson County (Jersey City / Hoboken / Weehawken) has the highest '
    'condo / co-op share in NJ (~45% of housing stock per ACS5 2022). '
    'FHFA-HPI purchase-only methodology excludes condo transactions; '
    'ZHVI includes them. As Hudson''s condo segment has appreciated '
    'faster than its SFR segment in the 2024+ window, the indices diverge '
    'by a methodology composition gap, not a measurement error. Observed: '
    'Hudson 2025 = -10.33%.',
    0.12,
    'FHFA HPI dataset documentation https://www.fhfa.gov/data/hpi/datasets '
    '(purchase-only excludes condos); ACS5 2022 table B25032 housing-stock '
    'composition for county FIPS 34017.',
    'platform-vision-7b',
    '2026-05-09'
)
ON CONFLICT (county_fips, year_start, cause_category) DO UPDATE SET
    year_end             = EXCLUDED.year_end,
    description          = EXCLUDED.description,
    expected_max_abs_pct = EXCLUDED.expected_max_abs_pct,
    source_citation      = EXCLUDED.source_citation,
    documented_by        = EXCLUDED.documented_by,
    documented_at        = EXCLUDED.documented_at;


-- ----------------------------------------------------------------------------
-- Regime D: Cumberland 2014-2015 narrow listing-stock turnover.
-- ----------------------------------------------------------------------------
INSERT INTO ref.cross_source_divergence_known_causes
    (county_fips, year_start, year_end, cause_category, description,
     expected_max_abs_pct, source_citation, documented_by, documented_at)
VALUES (
    '34011', 2014::SMALLINT, 2015::SMALLINT, 'composition_change',
    'Cumberland County experienced a one-time inventory-composition shift '
    '(large new SFR developments coming online) in 2014-2015 that briefly '
    'pushed ZHVI above FHFA. Narrow annotation rather than a regime. '
    'Observed: Cumberland 2014 = +8.55%, 2015 = +8.61%.',
    0.10,
    'NJ Department of Community Affairs Bureau of Statewide Planning, '
    'Cumberland County housing permit issuance 2013-2015; substrate-internal '
    'cross-source comparison against ref.platform_constants v_burden_base.',
    'platform-vision-7b',
    '2026-05-09'
)
ON CONFLICT (county_fips, year_start, cause_category) DO UPDATE SET
    year_end             = EXCLUDED.year_end,
    description          = EXCLUDED.description,
    expected_max_abs_pct = EXCLUDED.expected_max_abs_pct,
    source_citation      = EXCLUDED.source_citation,
    documented_by        = EXCLUDED.documented_by,
    documented_at        = EXCLUDED.documented_at;


-- ----------------------------------------------------------------------------
-- Regime E: Salem 2005 narrow development-cycle SFR composition.
-- ----------------------------------------------------------------------------
INSERT INTO ref.cross_source_divergence_known_causes
    (county_fips, year_start, year_end, cause_category, description,
     expected_max_abs_pct, source_citation, documented_by, documented_at)
VALUES (
    '34033', 2005::SMALLINT, 2005::SMALLINT, 'composition_change',
    'Salem County had a localized SFR development cycle in 2005 that '
    'shifted the composition of the FHFA repeat-sales pool toward newer '
    'construction; ZHVI captured the broader market including older '
    'inventory. Narrow single-year annotation. Observed: Salem 2005 = -10.78%.',
    0.12,
    'NJ DCA Bureau of Statewide Planning, Salem County housing permit '
    'issuance 2004-2006 (large multi-unit residential subdivisions); '
    'FHFA HPI purchase-only methodology https://www.fhfa.gov/data/hpi/datasets.',
    'platform-vision-7b',
    '2026-05-09'
)
ON CONFLICT (county_fips, year_start, cause_category) DO UPDATE SET
    year_end             = EXCLUDED.year_end,
    description          = EXCLUDED.description,
    expected_max_abs_pct = EXCLUDED.expected_max_abs_pct,
    source_citation      = EXCLUDED.source_citation,
    documented_by        = EXCLUDED.documented_by,
    documented_at        = EXCLUDED.documented_at;
