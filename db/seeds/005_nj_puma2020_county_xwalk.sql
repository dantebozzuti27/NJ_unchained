-- ============================================================================
-- Seed: 005_nj_puma2020_county_xwalk
--
-- Population-weighted PUMA-to-county allocation crosswalk for all
-- 74 NJ 2020-vintage PUMAs.
--
-- DATA SOURCE
-- -----------
-- PUMA codes + names sourced from the official Census TIGER/Line 2022
-- shapefile for NJ:
--
--     https://www2.census.gov/geo/tiger/TIGER2022/PUMA/tl_2022_34_puma20.zip
--
-- 72 PUMAs are wholly within a single county (allocation_factor = 1.0).
-- 2 PUMAs are multi-county; their splits are best-effort approximations
-- documented in the parent migration (028) header.
--
-- For tract-level-precise allocations, replace this seed with output
-- from Census Geocorr 2022:
--
--     https://mcdc.missouri.edu/applications/geocorr2022.html
--
-- Source: PUMA20 (2020 Vintage) -> County, weight "Population (2020)".
-- ============================================================================

INSERT INTO ref.puma2020_county_xwalk
    (state_fips, puma, county_fips, allocation_factor, source_vintage, notes)
VALUES
    -- ============================================================
    -- ATLANTIC COUNTY (34001) -- 2 single-county PUMAs
    -- ============================================================
    ('34', '00101', '34001', 1.0000, '2022-tiger',
     'Atlantic City, Pleasantville, Northfield & Coast'),
    ('34', '00102', '34001', 1.0000, '2022-tiger',
     'Outside Somers Point City -- West to Hammonton'),

    -- ============================================================
    -- BERGEN COUNTY (34003) -- 8 single-county PUMAs
    -- ============================================================
    ('34', '00301', '34003', 1.0000, '2022-tiger', 'South Central -- Hackensack & Teaneck'),
    ('34', '00302', '34003', 1.0000, '2022-tiger', 'Southwest -- Rutherford, North Arlington'),
    ('34', '00303', '34003', 1.0000, '2022-tiger', 'Southeast -- Fort Lee, Cliffside Park'),
    ('34', '00304', '34003', 1.0000, '2022-tiger', 'West Central -- Fair Lawn, Garfield, Lodi'),
    ('34', '00305', '34003', 1.0000, '2022-tiger', 'East -- Tenafly, Park Ridge, Englewood'),
    ('34', '00306', '34003', 1.0000, '2022-tiger', 'Northwest -- Ramsey, Oakland, Franklin Lakes'),
    ('34', '00307', '34003', 1.0000, '2022-tiger', 'North Central -- Bergenfield, Paramus'),
    ('34', '00308', '34003', 1.0000, '2022-tiger', 'Central -- Ridgewood, Glen Rock'),

    -- ============================================================
    -- PASSAIC COUNTY (34031) -- 4 single-county PUMAs
    -- ============================================================
    ('34', '00501', '34031', 1.0000, '2022-tiger', 'South -- Passaic & Clifton (SE)'),
    ('34', '00502', '34031', 1.0000, '2022-tiger', 'Central -- Hawthorne'),
    ('34', '00503', '34031', 1.0000, '2022-tiger', 'Paterson'),
    ('34', '00504', '34031', 1.0000, '2022-tiger', 'North -- Ringwood, Wanaque, Pompton Lakes'),

    -- ============================================================
    -- HUDSON COUNTY (34017) -- 5 single-county PUMAs
    -- ============================================================
    ('34', '00601', '34017', 1.0000, '2022-tiger', 'Central -- Jersey City North'),
    ('34', '00602', '34017', 1.0000, '2022-tiger', 'Central -- Jersey City South'),
    ('34', '00603', '34017', 1.0000, '2022-tiger', 'Northeast -- Union City & Hoboken'),
    ('34', '00604', '34017', 1.0000, '2022-tiger', 'North -- West New York, Secaucus, North Bergen'),
    ('34', '00605', '34017', 1.0000, '2022-tiger', 'South & West -- Bayonne, Kearny, Harrison'),

    -- ============================================================
    -- HUNTERDON COUNTY (34019) -- 1 PUMA
    -- ============================================================
    ('34', '00800', '34019', 1.0000, '2022-tiger', 'Whole-county PUMA'),

    -- ============================================================
    -- MIDDLESEX COUNTY (34023) -- 7 single-county PUMAs
    -- ============================================================
    ('34', '00901', '34023', 1.0000, '2022-tiger', 'Southeast'),
    ('34', '00902', '34023', 1.0000, '2022-tiger', 'Southwest'),
    ('34', '00903', '34023', 1.0000, '2022-tiger', 'Northwest -- Piscataway, South Plainfield'),
    ('34', '00904', '34023', 1.0000, '2022-tiger', 'North Central -- Metuchen, South Edison'),
    ('34', '00905', '34023', 1.0000, '2022-tiger', 'Northeast -- Carteret, North Woodbridge'),
    ('34', '00906', '34023', 1.0000, '2022-tiger', 'Central -- New Brunswick, Highland Park'),
    ('34', '00907', '34023', 1.0000, '2022-tiger', 'East Central -- Perth Amboy, Sayreville'),

    -- ============================================================
    -- SOMERSET COUNTY (34035) -- 3 single-county PUMAs
    -- ============================================================
    ('34', '01001', '34035', 1.0000, '2022-tiger', 'North & West'),
    ('34', '01002', '34035', 1.0000, '2022-tiger', 'South'),
    ('34', '01003', '34035', 1.0000, '2022-tiger', 'Central -- Bridgewater, Somerville'),

    -- ============================================================
    -- MONMOUTH COUNTY (34025) -- 6 single-county PUMAs
    -- ============================================================
    ('34', '01101', '34025', 1.0000, '2022-tiger', 'Southeast -- Wall, Tinton Falls'),
    ('34', '01102', '34025', 1.0000, '2022-tiger', 'Southwest -- Freehold, Manalapan'),
    ('34', '01103', '34025', 1.0000, '2022-tiger', 'Southeast -- Long Branch, Asbury Park'),
    ('34', '01104', '34025', 1.0000, '2022-tiger', 'Northeast -- Red Bank, NE Middletown'),
    ('34', '01105', '34025', 1.0000, '2022-tiger', 'Northwest -- Matawan, Aberdeen'),
    ('34', '01106', '34025', 1.0000, '2022-tiger', 'Central'),

    -- ============================================================
    -- OCEAN COUNTY (34029) -- 5 single-county PUMAs
    -- ============================================================
    ('34', '01201', '34029', 1.0000, '2022-tiger', 'South'),
    ('34', '01202', '34029', 1.0000, '2022-tiger', 'Central -- Toms River, Berkeley'),
    ('34', '01203', '34029', 1.0000, '2022-tiger', 'Lakewood'),
    ('34', '01204', '34029', 1.0000, '2022-tiger', 'Northwest'),
    ('34', '01205', '34029', 1.0000, '2022-tiger', 'Northeast -- Brick, Point Pleasant'),

    -- ============================================================
    -- ESSEX COUNTY (34013) -- 7 single-county PUMAs
    -- ============================================================
    ('34', '01401', '34013', 1.0000, '2022-tiger', 'Northeast -- Bloomfield'),
    ('34', '01402', '34013', 1.0000, '2022-tiger', 'South Central -- Irvington'),
    ('34', '01403', '34013', 1.0000, '2022-tiger', 'Northwest -- Montclair'),
    ('34', '01404', '34013', 1.0000, '2022-tiger', 'Southwest -- West Orange, Livingston'),
    ('34', '01405', '34013', 1.0000, '2022-tiger', 'Central -- Orange, East Orange'),
    ('34', '01406', '34013', 1.0000, '2022-tiger', 'Southeast -- Newark North & East'),
    ('34', '01407', '34013', 1.0000, '2022-tiger', 'Southeast -- Newark West & SW'),

    -- ============================================================
    -- MORRIS COUNTY (34027) -- 4 single-county PUMAs
    -- ============================================================
    ('34', '01501', '34027', 1.0000, '2022-tiger', 'North -- Dover, Kinnelon'),
    ('34', '01502', '34027', 1.0000, '2022-tiger', 'West'),
    ('34', '01503', '34027', 1.0000, '2022-tiger', 'East -- Lincoln Park'),
    ('34', '01504', '34027', 1.0000, '2022-tiger', 'South -- Morristown, Madison, Florham Park'),

    -- ============================================================
    -- SUSSEX COUNTY (34037) -- 1 PUMA
    -- ============================================================
    ('34', '01600', '34037', 1.0000, '2022-tiger', 'Whole-county PUMA'),

    -- ============================================================
    -- WARREN COUNTY (34041) -- 1 PUMA
    -- ============================================================
    ('34', '01700', '34041', 1.0000, '2022-tiger', 'Whole-county PUMA'),

    -- ============================================================
    -- UNION COUNTY (34039) -- 5 single-county PUMAs
    -- ============================================================
    ('34', '01901', '34039', 1.0000, '2022-tiger', 'North Central -- Union Twp, Roselle Park'),
    ('34', '01902', '34039', 1.0000, '2022-tiger', 'Northwest -- Summit, Cranford'),
    ('34', '01903', '34039', 1.0000, '2022-tiger', 'Southwest -- Plainfield, Westfield'),
    ('34', '01904', '34039', 1.0000, '2022-tiger', 'Southeast -- Linden, Rahway, Roselle'),
    ('34', '01905', '34039', 1.0000, '2022-tiger', 'Northeast -- Elizabeth'),

    -- ============================================================
    -- BURLINGTON COUNTY (34005) -- 3 single-county PUMAs
    -- ============================================================
    ('34', '02001', '34005', 1.0000, '2022-tiger', 'North -- Burlington City'),
    ('34', '02002', '34005', 1.0000, '2022-tiger', 'West Central'),
    ('34', '02003', '34005', 1.0000, '2022-tiger', 'South & East'),

    -- ============================================================
    -- CAMDEN COUNTY (34007) -- 4 single-county PUMAs
    -- ============================================================
    ('34', '02101', '34007', 1.0000, '2022-tiger', 'North -- Camden & Gloucester Cities'),
    ('34', '02102', '34007', 1.0000, '2022-tiger', 'Central -- Lindenwold, Collingswood'),
    ('34', '02103', '34007', 1.0000, '2022-tiger', 'South & West -- Bellmawr, Pine Hill'),
    ('34', '02104', '34007', 1.0000, '2022-tiger', 'East Central -- Haddonfield'),

    -- ============================================================
    -- GLOUCESTER COUNTY (34015) -- 2 single-county PUMAs
    -- ============================================================
    ('34', '02201', '34015', 1.0000, '2022-tiger', 'Northeast -- Woodbury'),
    ('34', '02202', '34015', 1.0000, '2022-tiger', 'South & West -- Glassboro'),

    -- ============================================================
    -- MERCER COUNTY (34021) -- 3 single-county PUMAs
    -- ============================================================
    ('34', '02301', '34021', 1.0000, '2022-tiger', 'West Central -- Trenton'),
    ('34', '02302', '34021', 1.0000, '2022-tiger', 'North -- Princeton'),
    ('34', '02303', '34021', 1.0000, '2022-tiger', 'Southwest'),

    -- ============================================================
    -- CUMBERLAND COUNTY (34011) -- 1 single-county PUMA
    -- ============================================================
    ('34', '02401', '34011', 1.0000, '2022-tiger', 'South -- Vineland & Millville'),

    -- ============================================================
    -- MULTI-COUNTY PUMAS (population-weighted approximations)
    -- ============================================================
    -- 02501: Salem (34033, ~64K total) + Cumberland (34011) North.
    -- All of Salem (~64K) + ~50K of northern Cumberland in this PUMA.
    -- Allocation: Salem 0.56, Cumberland 0.44.
    --
    -- For exact tract-level allocation, run Geocorr 2022 with
    -- weighting variable "Population (2020)".
    ('34', '02501', '34033', 0.560000, '2022-tiger',
     'Salem & Cumberland (N) -- Bridgeton; Salem share approx, replace with Geocorr.'),
    ('34', '02501', '34011', 0.440000, '2022-tiger',
     'Salem & Cumberland (N) -- Bridgeton; Cumberland-N share approx, replace with Geocorr.'),

    -- 02601: Cape May (34009, ~95K total) + Atlantic (34001) South-Central.
    -- All of Cape May (~95K) + ~25K of Atlantic SC in this PUMA.
    -- Allocation: Cape May 0.79, Atlantic 0.21.
    ('34', '02601', '34009', 0.790000, '2022-tiger',
     'Cape May & Atlantic (SC) -- Ocean City, Somers Point; Cape May share approx.'),
    ('34', '02601', '34001', 0.210000, '2022-tiger',
     'Cape May & Atlantic (SC) -- Ocean City, Somers Point; Atlantic-SC share approx.')

ON CONFLICT (state_fips, puma, county_fips) DO UPDATE SET
    allocation_factor = EXCLUDED.allocation_factor,
    source_vintage    = EXCLUDED.source_vintage,
    notes             = EXCLUDED.notes;
