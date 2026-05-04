-- ============================================================================
-- Migration: 025_nj_dca_property_tax
--
-- TIER 2: NJ Department of Community Affairs (DCA) annual county-level
-- property tax data. The single most important NJ-specific variable for
-- the burden-ratio analysis: NJ has the highest effective property-tax
-- rate in the United States (Tax Foundation 2026: ~2.2% of assessed
-- value). For NJ, ACS B25088 (median monthly owner cost with mortgage)
-- already includes property tax, but this dataset gives:
--
--   1. Property tax DECOMPOSED (county / school / municipal shares),
--      enabling questions like "how much of NJ's housing burden is
--      driven by school funding vs municipal services?"
--
--   2. Tax decomposed for ALL owner-occupied homes, not just those with
--      mortgages (B25088 has separate w/-mortgage and without-mortgage
--      tables; DCA gives one number).
--
--   3. The EFFECTIVE TAX RATE (CY Total Rate), so we can model
--      counterfactuals like "what would Bergen's burden be at Mercer's
--      tax rate?"
--
--   4. Coverage from 1998 forward, deeper than ACS 5-year (which starts
--      at 2009 for county-level estimates).
--
-- DCA's "MuniCode" is a 4-digit code where the last two digits encode
-- the municipality and "00" means the county-level summary. We extract
-- only the "00" rows for raw.nj_property_tax_county; municipal rows go
-- to a future raw.nj_property_tax_municipal (out of scope for the
-- county-level burden analysis).
--
-- DCA COUNTY CODE -> FIPS COUNTY CODE
-- ------------------------------------
-- DCA codes its 21 counties 01..21 in alphabetical order. NJ's FIPS
-- county codes are odd-numbered 001..041 in the same alphabetical order.
-- The mapping is therefore deterministic: fips_county = 2*dca_code - 1,
-- prefixed with '34' (NJ state FIPS). We seed this mapping below for
-- explicitness rather than computing it at query time.
-- ============================================================================

-- Mapping table only; the 21 rows that populate it live in
-- db/seeds/002_nj_dca_county.sql. Seeds run AFTER all migrations, so
-- the FK to ref.county is satisfied before we insert the mapping rows.
CREATE TABLE ref.nj_dca_county (
    dca_code     CHAR(2)  PRIMARY KEY CHECK (dca_code ~ '^[0-2][0-9]$'),
    county_fips  CHAR(5)  NOT NULL UNIQUE REFERENCES ref.county(county_fips),
    county_name  TEXT     NOT NULL UNIQUE
);

COMMENT ON TABLE ref.nj_dca_county IS
    'DCA county code (01..21) -> NJ county FIPS mapping. Deterministic '
    'from alphabetical order, seeded explicitly for traceability.';


CREATE TABLE raw.nj_property_tax_county (
    county_fips                  CHAR(5)        NOT NULL
        REFERENCES ref.county(county_fips),
    year                         SMALLINT       NOT NULL
        CHECK (year BETWEEN 1998 AND 2099),

    -- Tax base (assessed value the levy is computed against).
    net_valuation_taxable        NUMERIC(20,2)
        CHECK (net_valuation_taxable IS NULL OR net_valuation_taxable >= 0),

    -- Total dollars levied, decomposed by purpose.
    total_county_levy            NUMERIC(20,2),
    total_school_levy            NUMERIC(20,2),
    total_municipal_levy         NUMERIC(20,2),
    total_levy                   NUMERIC(20,2),

    -- Effective tax rates (percent, e.g. 2.85 means 2.85% of assessed).
    cy_total_rate                NUMERIC(8,5),
    cy_county_rate               NUMERIC(8,5),
    cy_school_rate               NUMERIC(8,5),
    cy_municipal_rate            NUMERIC(8,5),

    -- Headline residential statistics (the numbers a NJ homeowner sees).
    avg_residential_value        NUMERIC(14,2),
    avg_total_property_taxes     NUMERIC(12,2),
    avg_county_taxes             NUMERIC(12,2),
    avg_school_taxes             NUMERIC(12,2),
    avg_municipal_taxes          NUMERIC(12,2),

    -- Equalized (post-revaluation) tax base, lets us compare counties on
    -- a market-value basis even when they re-assess on different cycles.
    cy_equalized_property_value  NUMERIC(20,2),
    cy_total_eq_rate             NUMERIC(8,5),

    source_url                   TEXT           NOT NULL,
    source_sha256                CHAR(64)       NOT NULL,
    source_vintage               TEXT           NOT NULL,   -- e.g. "2024-annual"
    ingested_at                  TIMESTAMPTZ    NOT NULL DEFAULT now(),

    PRIMARY KEY (county_fips, year)
);

COMMENT ON TABLE raw.nj_property_tax_county IS
    'NJ DCA annual county-level property tax statistics. Headline columns: '
    'avg_total_property_taxes (the dollar figure homeowners see) and '
    'cy_total_rate (effective tax rate, percent of assessed value).';

CREATE INDEX raw_nj_property_tax_year_idx ON raw.nj_property_tax_county (year);


-- Property tax burden: avg_total_property_taxes / median household income.
-- This is the additional burden NJ owner households carry on top of
-- ACS B25088's monthly owner cost. Note that B25088 already includes
-- property tax; this view exists to make the property-tax SHARE explicit
-- and to support counterfactual analyses ("what if this county had the
-- state median tax rate?").
CREATE VIEW derived.nj_property_tax_burden AS
SELECT
    p.county_fips,
    p.year,
    inc.product,
    p.avg_total_property_taxes,
    p.avg_residential_value,
    p.cy_total_rate,
    inc.estimate                       AS household_income,

    CASE WHEN inc.estimate > 0 AND p.avg_total_property_taxes IS NOT NULL
         THEN round(p.avg_total_property_taxes / inc.estimate, 4)
    END                                AS property_tax_share_of_income,

    -- The effective tax burden if you bought a median-priced NJ home in
    -- this county: avg_taxes / avg_residential_value (should approximate
    -- cy_total_rate / 100, modulo equalization). This sanity-checks
    -- DCA's own published rate.
    CASE WHEN p.avg_residential_value > 0 AND p.avg_total_property_taxes IS NOT NULL
         THEN round(
             (p.avg_total_property_taxes / p.avg_residential_value) * 100, 5
         )
    END                                AS implied_effective_rate
FROM raw.nj_property_tax_county      p
LEFT JOIN raw.acs_median_household_income inc
       ON inc.county_fips = p.county_fips
      AND inc.year        = p.year
      AND inc.estimate IS NOT NULL;

COMMENT ON VIEW derived.nj_property_tax_burden IS
    'NJ property tax as a share of ACS median household income, per '
    '(county, year). Joins on the latest ACS data available for that year.';


CREATE VIEW public.v_nj_property_tax_recent AS
SELECT
    c.county_id,
    c.name                         AS county_name,
    p.year,
    p.avg_residential_value,
    p.avg_total_property_taxes,
    p.cy_total_rate,
    round(p.avg_county_taxes,    2) AS avg_county_taxes,
    round(p.avg_school_taxes,    2) AS avg_school_taxes,
    round(p.avg_municipal_taxes, 2) AS avg_municipal_taxes
FROM raw.nj_property_tax_county p
JOIN ref.county                 c ON c.county_fips = p.county_fips
WHERE p.year >= 2018
ORDER BY p.year DESC, p.avg_total_property_taxes DESC;

COMMENT ON VIEW public.v_nj_property_tax_recent IS
    'NJ property tax recent panel (2018+), ranked by burden. The '
    'shorthand view for the property-tax dashboard.';
