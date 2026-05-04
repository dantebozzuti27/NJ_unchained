-- ============================================================================
-- Seed: NJ counties (21)
--
-- Source: U.S. Census Bureau, 2020 TIGER/Line shapefiles
-- (https://www.census.gov/geographies/mapping-files/time-series/geo/
--  tiger-line-file.2020.html), County (and equivalent) Areas, NJ (state FIPS 34).
--
-- ALAND / AWATER and centroids from the Census 2020 county gazetteer
-- (https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2020_Gazetteer/).
--
-- This seed is idempotent (ON CONFLICT DO NOTHING). Re-run is safe.
-- ============================================================================

BEGIN;

INSERT INTO ref.county (
    county_id, state_code, county_fips, name, name_legal,
    aland_sqmeters, awater_sqmeters, centroid_lat, centroid_lon
) VALUES
    ('NJ-ATLANTIC',   'NJ', '34001', 'Atlantic',   'Atlantic County',    1442223420,  657991440, 39.46939, -74.63312),
    ('NJ-BERGEN',     'NJ', '34003', 'Bergen',     'Bergen County',       604393922,   60624596, 40.95995, -74.07442),
    ('NJ-BURLINGTON', 'NJ', '34005', 'Burlington', 'Burlington County',  2071658480,   95983032, 39.87644, -74.66796),
    ('NJ-CAMDEN',     'NJ', '34007', 'Camden',     'Camden County',       575345894,   16811540, 39.80128, -74.96022),
    ('NJ-CAPE_MAY',   'NJ', '34009', 'Cape May',   'Cape May County',     650076107, 1554589104, 39.08555, -74.84603),
    ('NJ-CUMBERLAND', 'NJ', '34011', 'Cumberland', 'Cumberland County',  1257303280,  104977700, 39.32946, -75.12827),
    ('NJ-ESSEX',      'NJ', '34013', 'Essex',      'Essex County',        327656232,   17612572, 40.78745, -74.24643),
    ('NJ-GLOUCESTER', 'NJ', '34015', 'Gloucester', 'Gloucester County',   843066144,   53595272, 39.71790, -75.14143),
    ('NJ-HUDSON',     'NJ', '34017', 'Hudson',     'Hudson County',       119630430,   54996700, 40.73121, -74.07527),
    ('NJ-HUNTERDON',  'NJ', '34019', 'Hunterdon',  'Hunterdon County',   1108048956,   17080224, 40.56682, -74.91283),
    ('NJ-MERCER',     'NJ', '34021', 'Mercer',     'Mercer County',       580716392,   13660936, 40.28324, -74.70123),
    ('NJ-MIDDLESEX',  'NJ', '34023', 'Middlesex',  'Middlesex County',    802929696,   83379860, 40.43979, -74.40926),
    ('NJ-MONMOUTH',   'NJ', '34025', 'Monmouth',   'Monmouth County',    1227267148,  348635644, 40.28792, -74.15276),
    ('NJ-MORRIS',     'NJ', '34027', 'Morris',     'Morris County',      1187344388,   33018692, 40.86268, -74.54867),
    ('NJ-OCEAN',      'NJ', '34029', 'Ocean',      'Ocean County',       1622168784, 1009608236, 39.86532, -74.30856),
    ('NJ-PASSAIC',    'NJ', '34031', 'Passaic',    'Passaic County',      483112660,   19672484, 41.03434, -74.30068),
    ('NJ-SALEM',      'NJ', '34033', 'Salem',      'Salem County',        859541916,  175167924, 39.57215, -75.35784),
    ('NJ-SOMERSET',   'NJ', '34035', 'Somerset',   'Somerset County',     798555664,   13048164, 40.56324, -74.61643),
    ('NJ-SUSSEX',     'NJ', '34037', 'Sussex',     'Sussex County',      1339097032,   42040316, 41.13955, -74.69121),
    ('NJ-UNION',      'NJ', '34039', 'Union',      'Union County',        267341228,   25033844, 40.66021, -74.30806),
    ('NJ-WARREN',     'NJ', '34041', 'Warren',     'Warren County',      929027996,    23625848, 40.85675, -74.99721)
ON CONFLICT (county_id) DO NOTHING;

-- Sanity check: NJ has exactly 21 counties. Fail loudly if the seed missed any.
DO $$
DECLARE
    n INTEGER;
BEGIN
    SELECT count(*) INTO n FROM ref.county WHERE state_code = 'NJ';
    IF n <> 21 THEN
        RAISE EXCEPTION 'Expected 21 NJ counties, found %', n;
    END IF;
END$$;

COMMIT;
