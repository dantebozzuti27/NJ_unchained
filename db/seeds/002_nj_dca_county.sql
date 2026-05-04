-- ============================================================================
-- Seed: 002_nj_dca_county
--
-- DCA county code (01..21) -> NJ county FIPS mapping.
--
-- Depends on db/seeds/001_nj_counties.sql being applied first; the
-- migration runner guarantees lexical-order application within a
-- directory, so 001_nj_counties.sql lands before this file.
--
-- Idempotent under re-run via the same `dca_code` PRIMARY KEY conflict
-- handling as the rest of our seed loaders.
-- ============================================================================

INSERT INTO ref.nj_dca_county (dca_code, county_fips, county_name) VALUES
    ('01', '34001', 'Atlantic'),
    ('02', '34003', 'Bergen'),
    ('03', '34005', 'Burlington'),
    ('04', '34007', 'Camden'),
    ('05', '34009', 'Cape May'),
    ('06', '34011', 'Cumberland'),
    ('07', '34013', 'Essex'),
    ('08', '34015', 'Gloucester'),
    ('09', '34017', 'Hudson'),
    ('10', '34019', 'Hunterdon'),
    ('11', '34021', 'Mercer'),
    ('12', '34023', 'Middlesex'),
    ('13', '34025', 'Monmouth'),
    ('14', '34027', 'Morris'),
    ('15', '34029', 'Ocean'),
    ('16', '34031', 'Passaic'),
    ('17', '34033', 'Salem'),
    ('18', '34035', 'Somerset'),
    ('19', '34037', 'Sussex'),
    ('20', '34039', 'Union'),
    ('21', '34041', 'Warren')
ON CONFLICT (dca_code) DO UPDATE SET
    county_fips = EXCLUDED.county_fips,
    county_name = EXCLUDED.county_name;
