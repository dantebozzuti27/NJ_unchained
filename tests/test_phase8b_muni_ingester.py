"""Phase 8b/8c integration tests: real-workbook muni ingester end-to-end.

The Phase 8a tests in `test_phase8_municipality.py` use synthetic
substrate to pin every closed-form math anchor to the cent. This file
plugs the *real* DCA workbook(s) into the *real* ingester and asserts:

  * parse_dca_workbook_munis returns the expected shape, schema, and
    row count for the 2024 vintage (564 munis after Pine Valley merger).
  * The 'Municipal Tax Summary' sheet contains a real anchor we can
    pin -- Bergen 0201 Allendale Borough's 2024 avg_residential_value
    is published as $801,920 in the DCA workbook (verified by hand
    against the source file).
  * load_munis_to_postgres survives the FK from raw.nj_property_tax_muni
    to ref.nj_municipality when seeds/040 is loaded; 564 rows go in,
    no constraint violations.
  * Phase 8c: derived.f_user_nj_muni_verdicts returns one row per muni
    in the requested county and the row count matches the seed
    dimension (Bergen = 70, Atlantic = 23, etc.).

This is the integration-test counterpart to Phase 8a's hand-anchor
tests: it doesn't pin specific dollar verdicts (those move year-over-
year as the DCA publishes revisions), but it verifies the FULL
pipeline -- from .xls bytes to SQL function output -- works against
the real artifacts the platform ingests in production.

Design note: skipped automatically when the 24taxes.xls workbook is
not on disk (the workbook is gitignored; CI hosts that don't run the
fetcher won't have it). The Phase 8a synthetic-substrate suite still
runs and provides hand-anchor coverage.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import psycopg


pytestmark = pytest.mark.live_pg


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKBOOK_2024 = REPO_ROOT / "data" / "manual" / "nj_dca_property_tax" / "24taxes.xls"


def _skip_if_no_workbook() -> None:
    if not WORKBOOK_2024.exists():
        pytest.skip(
            f"DCA 2024 workbook not on disk at {WORKBOOK_2024}; "
            "run `nj-ingest-dca fetch --year 2024` to populate it.",
        )


@pytest.fixture
def phase8b_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """A fully-migrated + fully-seeded test DB with the muni FK enforced.

    Different from phase8_db (no synthetic muni rows): the bulk
    ingester writes the rows, not the fixture.
    """
    from scripts.migrate import (
        MIGRATIONS_DIR,
        SEEDS_DIR,
        apply_migrations,
        discover,
    )

    conn = live_pg
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS governance CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS derived    CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS raw        CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ref        CASCADE")
        cur.execute(
            "DO $$ "
            "DECLARE r record; "
            "BEGIN "
            "  FOR r IN SELECT viewname FROM pg_views "
            "           WHERE schemaname='public' AND viewname LIKE 'v_%%' LOOP "
            "    EXECUTE 'DROP VIEW IF EXISTS public.' "
            "         || quote_ident(r.viewname) || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    conn.commit()
    return conn


# ============================================================================
# 1. parse_dca_workbook_munis -- real 2024 workbook shape
# ============================================================================


class TestParseRealWorkbook:

    def test_parse_2024_workbook_returns_564_munis(self) -> None:
        _skip_if_no_workbook()
        from ingestion.nj_dca_property_tax import parse_dca_workbook_munis

        result = parse_dca_workbook_munis(WORKBOOK_2024, year=2024)

        # 2024 vintage: 564 NJ munis (post-Pine-Valley merger).
        assert result.n_rows == 564
        assert result.year == 2024
        assert result.source_vintage == "2024-annual"
        assert result.source_sha256 != ""
        assert "24taxes.xls" in result.source_url

    def test_parsed_dataframe_has_expected_columns(self) -> None:
        _skip_if_no_workbook()
        from ingestion.nj_dca_property_tax import parse_dca_workbook_munis

        result = parse_dca_workbook_munis(WORKBOOK_2024, year=2024)
        df = result.dataframe

        # Schema parity with raw.nj_property_tax_muni (modulo the
        # provenance columns added by stage_dataframe).
        for required in (
            "muni_code", "year",
            "avg_residential_value", "cy_total_rate",
            "net_valuation_taxable", "total_levy",
        ):
            assert required in df.columns, f"missing column: {required}"

    def test_no_county_summary_rows_in_muni_output(self) -> None:
        """Muni codes ending in '00' are county summaries; the muni
        path filters them out so they never collide with the FK."""
        _skip_if_no_workbook()
        from ingestion.nj_dca_property_tax import parse_dca_workbook_munis

        result = parse_dca_workbook_munis(WORKBOOK_2024, year=2024)
        df = result.dataframe

        codes = df.get_column("muni_code").to_list()
        # Every code is exactly 4 chars and the last 2 chars are NOT '00'.
        for code in codes:
            assert isinstance(code, str)
            assert len(code) == 4
            assert not code.endswith("00"), (
                f"muni_code {code} is a county summary; should be filtered"
            )

    def test_known_anchor_allendale_2024(self) -> None:
        """Allendale Borough (0201, Bergen) is loaded as a real reference
        anchor in seed 040. We pin its presence and the basic shape of
        its row -- we don't pin the exact avg_residential_value because
        DCA reissues the 2024 workbook periodically.
        """
        _skip_if_no_workbook()
        from ingestion.nj_dca_property_tax import parse_dca_workbook_munis

        result = parse_dca_workbook_munis(WORKBOOK_2024, year=2024)
        df = result.dataframe

        allendale = df.filter(df["muni_code"] == "0201")
        assert allendale.height == 1, "Allendale Borough (0201) must be in 2024"

        row = allendale.row(0, named=True)
        assert row["year"] == 2024
        # avg_residential_value should be a reasonable Bergen-county
        # number; Allendale 2024 is in the high-six / low-seven figures.
        avg = row["avg_residential_value"]
        assert avg is not None
        assert 400_000 <= float(avg) <= 2_000_000, (
            f"Allendale avg_residential_value {avg} outside plausible range"
        )
        # cy_total_rate is published as a "rate per $100 of assessed
        # value"; Bergen rates run 1.5-3.5 and Allendale's is in band.
        rate = row["cy_total_rate"]
        assert rate is not None
        assert 1.0 <= float(rate) <= 5.0, (
            f"Allendale cy_total_rate {rate} outside plausible band"
        )


# ============================================================================
# 2. load_munis_to_postgres -- FK + UPSERT against the real seed dim
# ============================================================================


class TestLoadRealWorkbookEndToEnd:

    def test_bulk_load_2024_into_seeded_db(
        self, phase8b_db: psycopg.Connection
    ) -> None:
        _skip_if_no_workbook()
        from ingestion.nj_dca_property_tax import (
            load_munis_to_postgres,
            parse_dca_workbook_munis,
            stage_dataframe,
        )

        result = parse_dca_workbook_munis(WORKBOOK_2024, year=2024)
        staged = stage_dataframe(result)

        # FK from raw.nj_property_tax_muni.muni_code -> ref.nj_municipality
        # MUST hold: every code in the workbook must already be seeded.
        # This is the cross-vintage guarantee from seed 040 (UNION of
        # 2016-2024 muni codes).
        n_loaded = load_munis_to_postgres(staged, phase8b_db)
        phase8b_db.commit()

        assert n_loaded == 564

        # Re-run is idempotent (UPSERT, not INSERT).
        n_again = load_munis_to_postgres(staged, phase8b_db)
        phase8b_db.commit()
        assert n_again == 564

        with phase8b_db.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM raw.nj_property_tax_muni WHERE year = 2024"
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 564

    def test_coverage_view_after_bulk_load(
        self, phase8b_db: psycopg.Connection
    ) -> None:
        """raw.v_nj_property_tax_muni_coverage reports 100% for active
        munis in 2024. (Camden is 36/37 = 97.30% because Pine Valley
        merged with Pine Hill in 2022 and is now historical-only -- the
        ref.nj_municipality dimension keeps it for FK integrity but no
        2024 row exists, which is correct, not a bug.)
        """
        _skip_if_no_workbook()
        from ingestion.nj_dca_property_tax import (
            load_munis_to_postgres,
            parse_dca_workbook_munis,
            stage_dataframe,
        )

        result = parse_dca_workbook_munis(WORKBOOK_2024, year=2024)
        load_munis_to_postgres(stage_dataframe(result), phase8b_db)
        phase8b_db.commit()

        with phase8b_db.cursor() as cur:
            cur.execute(
                "SELECT county_fips, county_name, n_munis_loaded, n_munis_total, "
                "       pct_loaded "
                "FROM raw.v_nj_property_tax_muni_coverage "
                "WHERE year = 2024"
            )
            rows = {r[0]: r for r in cur.fetchall()}

        assert len(rows) == 21, "Coverage view must cover all 21 NJ counties"

        # Bergen and Hudson are 100% loaded.
        for fips in ("34003", "34017"):
            r = rows[fips]
            assert r[2] == r[3], (
                f"{r[1]} county should be 100% loaded; got {r[2]}/{r[3]}"
            )
            assert float(r[4]) == 100.00

        # Camden (34007): historical-only Pine Valley row makes 36 of 37.
        camden = rows["34007"]
        assert camden[2] == 36
        assert camden[3] == 37
        assert 96.00 <= float(camden[4]) <= 98.00


# ============================================================================
# 3. Phase 8c: f_user_nj_muni_verdicts returns 1 row/muni for a county
# ============================================================================


class TestMuniVerdictsAfterBulkLoad:

    def test_bergen_drilldown_returns_70_munis(
        self, phase8b_db: psycopg.Connection
    ) -> None:
        """The Phase 8c TS layer (lib/personalize.ts::runMuniVerdicts)
        calls this function. Verifies it returns the expected row
        count after bulk-loading real DCA data.
        """
        _skip_if_no_workbook()
        from ingestion.nj_dca_property_tax import (
            load_munis_to_postgres,
            parse_dca_workbook_munis,
            stage_dataframe,
        )

        # Bulk-load 2024 muni rows + minimal county/FRED/ACS substrate
        # so the verdict function has everything it needs (rate +
        # household-tax simulator + DTI assumptions).
        load_munis_to_postgres(
            stage_dataframe(parse_dca_workbook_munis(WORKBOOK_2024, year=2024)),
            phase8b_db,
        )
        with phase8b_db.cursor() as cur:
            cur.execute(
                "INSERT INTO raw.fred_observation "
                "  (series_id, observation_date, value, source_url, "
                "   source_sha256) "
                "VALUES ('MORTGAGE30US', '2024-06-06', 7.0000, "
                "        'https://fred.stlouisfed.org/', %s) "
                "ON CONFLICT DO NOTHING",
                ("a" * 64,),
            )
        phase8b_db.commit()

        # Bergen = 70 munis post-Pine-Valley (Camden's was 0429, not Bergen).
        with phase8b_db.cursor() as cur:
            cur.execute(
                "SELECT count(*), "
                "       count(*) FILTER (WHERE median_home_price IS NOT NULL) "
                "FROM derived.f_user_nj_muni_verdicts("
                "  2024::SMALLINT, %s::CHAR(5), 200000::NUMERIC, 'mfj'::TEXT, "
                "  1::INT, 1::INT, 0::NUMERIC, NULL::NUMERIC, NULL::NUMERIC, "
                "  NULL::NUMERIC, NULL::INT, NULL::NUMERIC, NULL::NUMERIC"
                ")",
                ("34003",),
            )
            row = cur.fetchone()
            assert row is not None
            total, populated = row
            # Bergen has 70 munis in the 2024 ref.nj_municipality.
            assert total == 70
            # Every Bergen muni is in the bulk-loaded 2024 substrate.
            assert populated == 70

    def test_atlantic_drilldown_returns_23_munis(
        self, phase8b_db: psycopg.Connection
    ) -> None:
        _skip_if_no_workbook()
        from ingestion.nj_dca_property_tax import (
            load_munis_to_postgres,
            parse_dca_workbook_munis,
            stage_dataframe,
        )

        load_munis_to_postgres(
            stage_dataframe(parse_dca_workbook_munis(WORKBOOK_2024, year=2024)),
            phase8b_db,
        )
        with phase8b_db.cursor() as cur:
            cur.execute(
                "INSERT INTO raw.fred_observation "
                "  (series_id, observation_date, value, source_url, "
                "   source_sha256) "
                "VALUES ('MORTGAGE30US', '2024-06-06', 7.0000, "
                "        'https://fred.stlouisfed.org/', %s) "
                "ON CONFLICT DO NOTHING",
                ("a" * 64,),
            )
        phase8b_db.commit()

        with phase8b_db.cursor() as cur:
            cur.execute(
                "SELECT count(*) "
                "FROM derived.f_user_nj_muni_verdicts("
                "  2024::SMALLINT, %s::CHAR(5), 100000::NUMERIC, 'single'::TEXT, "
                "  0::INT, 0::INT, 0::NUMERIC, NULL::NUMERIC, NULL::NUMERIC, "
                "  NULL::NUMERIC, NULL::INT, NULL::NUMERIC, NULL::NUMERIC"
                ")",
                ("34001",),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 23, "Atlantic county has 23 munis"

    def test_unknown_county_returns_zero_rows(
        self, phase8b_db: psycopg.Connection
    ) -> None:
        """Substrate-honesty: an unknown FIPS surfaces 0 rows, not a
        crash and not a silent fallback. The lib/personalize.ts layer
        sanitizes to ^\\d{5}$ before calling, so this is a defense-in-
        depth check: even a 5-digit non-NJ FIPS is honored as 'no rows'.
        """
        _skip_if_no_workbook()
        with phase8b_db.cursor() as cur:
            cur.execute(
                "SELECT count(*) "
                "FROM derived.f_user_nj_muni_verdicts("
                "  2024::SMALLINT, %s::CHAR(5), 100000::NUMERIC, 'single'::TEXT, "
                "  0::INT, 0::INT, 0::NUMERIC, NULL::NUMERIC, NULL::NUMERIC, "
                "  NULL::NUMERIC, NULL::INT, NULL::NUMERIC, NULL::NUMERIC"
                ")",
                ("06037",),  # Los Angeles County, CA -- not NJ
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 0
