#!/usr/bin/env bash
# =============================================================================
# Deploy Phase 4-8c + Phase 6 substrate to the production Neon database.
#
# What this does, in order:
#   1. Validates NEON_DATABASE_URL is set and reachable.
#   2. Applies every pending migration in db/migrations/ (idempotent).
#   3. Applies every pending seed in db/seeds/ (idempotent).
#   4. Bulk-loads FRED MORTGAGE30US 2010-01-01..2024-12-31 (~750 weekly rows).
#   5. Bulk-loads ACS5 B19013 median household income for NJ, 2010-2024
#      (~21 counties x 15 years = ~315 rows).
#   6. Bulk-loads NJ DCA county-level property tax 2016-2024 (~189 rows).
#   7. Bulk-loads NJ DCA muni-level property tax 2016-2024 (~5,082 rows).
#   8. Bulk-loads Zillow ZHVI county-level monthly 2000-01..present
#      (~21 counties x ~313 months = ~6,600 rows). Phase 6 substrate.
#   9. Prints verification queries: row counts, function existence, last
#      migration applied, smoke-test of derived.f_user_nj_county_verdicts
#      and derived.f_housing_index_cross_source.
#
# Idempotency contract:
#   - Every step is safe to re-run. Migrations are gated by sha256-checked
#     governance.schema_migrations rows. Seeds use ON CONFLICT DO UPDATE.
#     Raw UPSERTs use the natural PK (county_fips, year) / (muni_code, year)
#     so re-running with the same vintage just refreshes ingested_at.
#
# Pre-requisites:
#   - NEON_DATABASE_URL exported in this shell. Get it from
#     Vercel dashboard -> nj-unchained -> Settings -> Environment Variables
#     -> NEON_DATABASE_URL -> click the eye icon to reveal.
#   - .venv set up: `python -m venv .venv && .venv/bin/pip install -e .`
#   - Public internet (FRED, Census, NJ DCA fetches happen during the run).
#   - The on-disk NJ DCA workbooks data/manual/nj_dca_property_tax/{16..24}taxes.xls
#     -- the load-muni step uses the local cache when present and falls back
#     to fetching fresh otherwise.
#
# Runtime: ~5-10 minutes against a typical Neon endpoint (the migration
# train is ~78 statements; the bulk loads are dominated by HTTP latency
# to the public API hosts, not by Postgres throughput).
#
# Failure modes worth knowing:
#   - If FRED returns 429 (rate limited) the FRED step retries with
#     backoff. Set FRED_API_KEY to raise the rate limit (optional).
#   - If Census ACS5 has not yet released a vintage you requested, the
#     ingester logs a warning and skips that year; substrate-honest NULL
#     bubbles through to the page.
#
# Usage:
#   export NEON_DATABASE_URL='postgresql://user:pass@ep-xxx-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require'
#   bash scripts/deploy_neon_substrate.sh
# =============================================================================

set -euo pipefail

# ----------------------------------------------------------------------------
# 0. Pre-flight
# ----------------------------------------------------------------------------

if [[ -z "${NEON_DATABASE_URL:-}" ]]; then
    echo "ERROR: NEON_DATABASE_URL is not set." >&2
    echo "       Pull it from Vercel: dashboard -> nj-unchained -> Settings" >&2
    echo "       -> Environment Variables -> NEON_DATABASE_URL -> reveal." >&2
    echo "       Then: export NEON_DATABASE_URL='postgresql://...'" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -x .venv/bin/nj-migrate ]]; then
    echo "ERROR: .venv/bin/nj-migrate not found. Run:" >&2
    echo "         python -m venv .venv && .venv/bin/pip install -e ." >&2
    exit 1
fi

# Common alias so every step uses the same DSN.
DSN="$NEON_DATABASE_URL"
PYTHON=.venv/bin/python
NJ_MIGRATE=.venv/bin/nj-migrate
NJ_FRED=.venv/bin/nj-ingest-fred
NJ_ACS_INCOME=.venv/bin/nj-ingest-acs-income
NJ_DCA=.venv/bin/nj-ingest-dca
NJ_ZHVI=.venv/bin/nj-ingest-zhvi

echo "[deploy] Repo root: $REPO_ROOT"
echo "[deploy] Target DB host: $(echo "$DSN" | sed -E 's,.*@([^/?]+).*,\1,')"
echo

# ----------------------------------------------------------------------------
# 1. Reachability check (single round-trip canary)
# ----------------------------------------------------------------------------

echo "[1/9] Pinging Neon..."
"$PYTHON" - <<'PY'
import os, psycopg, sys
url = os.environ["NEON_DATABASE_URL"]
with psycopg.connect(url, connect_timeout=20) as conn, conn.cursor() as cur:
    cur.execute("SELECT current_database(), current_user, version()")
    db, user, ver = cur.fetchone()
    print(f"      db={db}, user={user}, pg={ver.split(',')[0]}")
PY
echo

# ----------------------------------------------------------------------------
# 2. Migrations
# ----------------------------------------------------------------------------

echo "[2/9] Applying migrations (db/migrations/*.sql)..."
"$NJ_MIGRATE" apply --dsn "$DSN"
echo

# ----------------------------------------------------------------------------
# 3. Seeds
# ----------------------------------------------------------------------------

echo "[3/9] Applying seeds (db/seeds/*.sql)..."
"$NJ_MIGRATE" seed --dsn "$DSN"
echo

# ----------------------------------------------------------------------------
# 4. FRED MORTGAGE30US
# ----------------------------------------------------------------------------

echo "[4/9] Bulk-loading FRED MORTGAGE30US 2010-01-01..2024-12-31..."
"$NJ_FRED" load \
    --start-date 2010-01-01 \
    --end-date   2024-12-31 \
    --series     MORTGAGE30US \
    --dsn        "$DSN"
echo

# ----------------------------------------------------------------------------
# 5. ACS5 B19013 median household income for NJ, 2010-2024
# ----------------------------------------------------------------------------

echo "[5/9] Bulk-loading ACS5 B19013 (NJ counties, 2010-2024)..."
"$NJ_ACS_INCOME" load \
    --start-year 2010 \
    --end-year   2024 \
    --product    acs5 \
    --state      34 \
    --dsn        "$DSN"
echo

# ----------------------------------------------------------------------------
# 6. NJ DCA county-level property tax (mig 070 substrate)
# ----------------------------------------------------------------------------

echo "[6/9] Bulk-loading NJ DCA county property tax 2016-2024..."
"$NJ_DCA" load \
    --start-year 2016 \
    --end-year   2024 \
    --dsn        "$DSN"
echo

# ----------------------------------------------------------------------------
# 7. NJ DCA muni-level property tax (mig 077 substrate)
# ----------------------------------------------------------------------------

echo "[7/9] Bulk-loading NJ DCA municipality property tax 2016-2024..."
"$NJ_DCA" load-muni \
    --start-year 2016 \
    --end-year   2024 \
    --dsn        "$DSN"
echo

# ----------------------------------------------------------------------------
# 8. Zillow ZHVI county monthly (mig 079 substrate, Phase 6)
# ----------------------------------------------------------------------------

echo "[8/9] Bulk-loading Zillow ZHVI county monthly (NJ, 2000-01..present)..."
"$NJ_ZHVI" load \
    --dsn        "$DSN"
echo

# ----------------------------------------------------------------------------
# 9. Verification: print row counts + smoke-test the personalization engine
#    + ZHVI cross-source surface
# ----------------------------------------------------------------------------

echo "[9/9] Verifying production state..."
"$PYTHON" - <<'PY'
import os, psycopg
url = os.environ["NEON_DATABASE_URL"]
with psycopg.connect(url, connect_timeout=20) as conn, conn.cursor() as cur:
    cur.execute("""
        SELECT to_regclass('ref.nj_municipality') IS NOT NULL,
               to_regclass('raw.nj_property_tax_county') IS NOT NULL,
               to_regclass('raw.nj_property_tax_muni') IS NOT NULL,
               to_regclass('raw.zillow_zhvi_county') IS NOT NULL,
               to_regclass('derived.v_affordability_gap') IS NOT NULL,
               to_regclass('derived.v_muni_affordability_gap') IS NOT NULL,
               to_regclass('derived.v_zhvi_county_annual') IS NOT NULL
    """)
    print("    schema:", cur.fetchone())

    cur.execute("SELECT count(*) FROM ref.nj_municipality")
    print(f"    ref.nj_municipality:           {cur.fetchone()[0]} rows")

    cur.execute("SELECT count(*) FROM raw.nj_property_tax_county")
    print(f"    raw.nj_property_tax_county:    {cur.fetchone()[0]} rows")

    cur.execute("SELECT count(*) FROM raw.nj_property_tax_muni")
    print(f"    raw.nj_property_tax_muni:      {cur.fetchone()[0]} rows")

    cur.execute("SELECT count(*) FROM raw.fred_observation WHERE series_id='MORTGAGE30US'")
    print(f"    raw.fred_observation MORT30US: {cur.fetchone()[0]} rows")

    cur.execute("SELECT count(*) FROM raw.acs_median_household_income")
    print(f"    raw.acs_median_household_income: {cur.fetchone()[0]} rows")

    cur.execute("""
        SELECT count(*),
               count(DISTINCT county_fips),
               min(observation_month),
               max(observation_month)
        FROM raw.zillow_zhvi_county
    """)
    n, n_counties, lo, hi = cur.fetchone()
    print(f"    raw.zillow_zhvi_county:        {n} rows, "
          f"{n_counties} counties, {lo}..{hi}")

    # Cross-source housing-index divergence at base year 2010 -- the
    # spec §8.1 substrate. Counts how many (county, year) pairs have
    # both indices loaded and prints the worst NJ divergence in 2024.
    cur.execute("""
        SELECT count(*) FILTER (WHERE divergence_pct_of_fhfa IS NOT NULL),
               count(*)
        FROM derived.f_housing_index_cross_source(2010::SMALLINT)
    """)
    both, total = cur.fetchone()
    print(f"    f_housing_index_cross_source(2010): "
          f"{both} of {total} rows have BOTH FHFA + ZHVI loaded")

    # Verifiable-data substrate (mig 080 / 081 / seeds 014 / 015): every
    # platform constant and every tier band has provenance loaded.
    cur.execute("""
        SELECT constant_id, value::INT
        FROM ref.platform_constants
        WHERE constant_id IN ('burden_base_year', 'cross_source_base_year')
        ORDER BY constant_id
    """)
    pc = cur.fetchall()
    print(f"    ref.platform_constants:        {len(pc)} verifiable constant(s) loaded")
    for cid, val in pc:
        print(f"      {cid:<28} = {val}")

    cur.execute("""
        SELECT band_ord, label, lower_bound, upper_bound
        FROM ref.tier_bands
        WHERE tier_kind = 'burden_growth_ratio'
        ORDER BY band_ord
    """)
    tb = cur.fetchall()
    print(f"    ref.tier_bands burden_growth_ratio: {len(tb)} band(s) loaded")
    for ord_, label, lo, hi in tb:
        lo_s = "(-inf" if lo is None else f"[{float(lo):.2f}"
        hi_s = "+inf)" if hi is None else f"{float(hi):.2f})"
        print(f"      ord={ord_} {label:<10} {lo_s}, {hi_s}")

    cur.execute("""
        SELECT county_fips,
               round(fhfa_hpi_indexed::NUMERIC, 1) AS fhfa,
               round(zillow_zhvi_indexed::NUMERIC, 1) AS zhvi,
               round(divergence_pct_of_fhfa::NUMERIC, 4) AS pct
        FROM derived.f_housing_index_cross_source(2010::SMALLINT)
        WHERE year = 2024
          AND divergence_pct_of_fhfa IS NOT NULL
        ORDER BY abs(divergence_pct_of_fhfa) DESC
        LIMIT 3
    """)
    print(f"    Worst NJ FHFA-vs-ZHVI divergences in 2024:")
    for row in cur.fetchall():
        print(f"      {row}")

    # Smoke-test: f_user_nj_county_verdicts at $200K MFJ-1-1, 2024.
    # If this returns 21 rows with at least one non-NULL median, the
    # /personalize page will render successfully.
    cur.execute("""
        SELECT count(*) AS n_rows,
               count(*) FILTER (WHERE median_home_price IS NOT NULL)
                                                AS n_populated,
               count(*) FILTER (WHERE verdict_dti = 'affordable') AS n_affordable
        FROM derived.f_user_nj_county_verdicts(
            2024::SMALLINT, 200000::NUMERIC, 'mfj'::TEXT,
            1::INT, 1::INT, 0::NUMERIC,
            NULL::NUMERIC, NULL::NUMERIC, NULL::NUMERIC, NULL::INT,
            NULL::NUMERIC, NULL::NUMERIC
        )
    """)
    n, populated, affordable = cur.fetchone()
    print(f"    f_user_nj_county_verdicts 2024 $200K MFJ:")
    print(f"      {n} rows, {populated} populated, {affordable} affordable")
PY
echo
echo "[deploy] Done. Now hit:"
echo "  https://nj-unchained.vercel.app/personalize?gross=200000&filing=mfj&deps=1&kids=1"
echo "  https://nj-unchained.vercel.app/personalize?gross=200000&filing=mfj&deps=1&kids=1&county=34003"
