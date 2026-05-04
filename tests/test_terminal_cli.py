"""Tests for :mod:`serving.terminal_cli` (pure helpers + optional live CLI)."""

from __future__ import annotations

import datetime as dt
import json
import os

import pytest
from click.testing import CliRunner

from serving.models import (
    AssetDetail,
    AssetSummary,
    Health,
    ReleaseCalendarHorizonRow,
    ReleaseCalendarPanel,
)
from serving.terminal_cli import ascii_sparkline, main, resolve_county_fips
from serving.terminal_nj import _TERMINAL_SUBCOMMANDS, parse_nj_relay_argv
from serving.terminal_nj import main as nj_main


def test_nj_relay_subcommands_match_terminal_cli() -> None:
    """Each ``nj-terminal`` subcommand must be forwarded by ``nj`` relay mode."""
    assert frozenset(main.commands.keys()) == _TERMINAL_SUBCOMMANDS


def test_burden_json_empty_series_matches_series_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty ``GET /pums-burden-county-series`` is []; ``burden --json`` must not error."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")

    class _ConnCM:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect",
        lambda *a, **k: _ConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.list_pums_burden_county_series",
        lambda *_a, **_k: [],
    )
    # No materialization signal -> provenance is None and JSON null.
    monkeypatch.setattr(
        "serving.terminal_cli._fetch_asset_detail",
        lambda _conn, _ds: None,
    )

    runner = CliRunner()
    result = runner.invoke(main, ["burden", "bergen", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["series"] == []
    assert payload["n_years"] == 0
    assert payload["latest"] is None
    assert payload["year_range"] is None
    assert payload["sparkline_ascii"] == ""
    assert payload["county_fips"] == "34003"
    # BBG-LIKE-2: every metric output exposes its as-of stamp,
    # even when the underlying asset has no materialization signal.
    assert payload["provenance"] is None


# ============================================================================
# BBG-LIKE-2: provenance footer ("as-of" stamp on metric outputs)
# ============================================================================


def _make_asset_detail(**overrides: object) -> AssetDetail:
    """Helper: an AssetDetail with sensible defaults; per-test overrides."""
    base: dict[str, object] = {
        "dataset_id":           "derived.pums_burden_county_segmented",
        "cadence":              "annual",
        "schedule_label":       "Census ACS PUMS October release",
        "expected_lag_hours":   24 * 30,
        "last_materialized_at": dt.datetime(
            2026, 4, 29, 14, 32, 0, tzinfo=dt.UTC,
        ),
        "last_rows_upserted":   10_349,
        "age_hours":             3.2,
        "freshness_state":      "fresh",
        "n_warn_30d":           0,
        "n_error_30d":          0,
        "last_materialization_details": {"source_vintage": "2022-acs5"},
    }
    base.update(overrides)
    return AssetDetail(**base)


def test_fmt_age_hours_buckets_minutes_hours_days() -> None:
    """Age formatter: <1h -> Nm, 1-48h -> Nh, >=48h -> Nd."""
    from serving.terminal_cli import _fmt_age_hours

    assert _fmt_age_hours(None) == "age=?"
    assert _fmt_age_hours(0.0)  == "0m ago"
    assert _fmt_age_hours(0.5)  == "30m ago"
    assert _fmt_age_hours(0.99) == "59m ago"  # 0.99h -> 59.4 min -> 59
    assert _fmt_age_hours(1.0)  == "1.0h ago"
    assert _fmt_age_hours(3.25) == "3.2h ago"  # f"{3.25:.1f}" rounds banker's
    assert _fmt_age_hours(47.9) == "47.9h ago"
    assert _fmt_age_hours(48.0) == "2.0d ago"
    assert _fmt_age_hours(72.0) == "3.0d ago"


def test_format_provenance_footer_full_detail_contains_all_facets() -> None:
    """Happy path: every documented facet appears in the one-line footer."""
    from serving.terminal_cli import format_provenance_footer

    prov = _make_asset_detail()
    line = format_provenance_footer(prov)

    assert line.startswith("as-of "), line
    assert "2026-04-29" in line  # ET-formatted timestamp
    assert "ET" in line
    assert "3.2h ago" in line
    assert "fresh" in line
    assert "10,349 rows" in line
    assert "0w/0e" in line
    # Parens, not brackets -- avoids Rich-markup collision (see helper docstring).
    assert "(derived.pums_burden_county_segmented)" in line


def test_format_provenance_footer_none_returns_no_signal_line() -> None:
    """Missing AssetDetail -> transparent 'no signal' (NOT a fake stamp)."""
    from serving.terminal_cli import format_provenance_footer

    line = format_provenance_footer(None)
    assert line == "as-of —  (no materialization signal)"


def test_format_provenance_footer_never_materialized_is_distinct() -> None:
    """Calendar entry but no governance signal -> 'never materialized'.

    Distinct from the no-signal case so an operator can tell whether the
    dataset is unknown to the platform vs. registered-but-empty.
    """
    from serving.terminal_cli import format_provenance_footer

    prov = _make_asset_detail(
        last_materialized_at=None,
        last_rows_upserted=None,
        age_hours=None,
        freshness_state="unknown",
    )
    line = format_provenance_footer(prov)
    assert line == (
        "as-of —  (derived.pums_burden_county_segmented; never materialized)"
    )


def test_format_provenance_footer_health_warns_and_errors() -> None:
    """Warns + errors render as `Nw/Me` so an operator immediately sees drift."""
    from serving.terminal_cli import format_provenance_footer

    prov = _make_asset_detail(n_warn_30d=4, n_error_30d=1)
    assert "4w/1e" in format_provenance_footer(prov)


def test_burden_json_includes_provenance_when_asset_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: burden --json carries the full AssetDetail under .provenance."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")

    class _ConnCM:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect",
        lambda *a, **k: _ConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.list_pums_burden_county_series",
        lambda *_a, **_k: [
            {
                "year": 2021,
                "burden_ratio_p50": 0.30,
                "burden_ratio_p50_se": 0.01,
                "weighted_n": 100_000,
                "sample_n": 1500,
                "suppressed": False,
            },
            {
                "year": 2022,
                "burden_ratio_p50": 0.32,
                "burden_ratio_p50_se": 0.01,
                "weighted_n": 105_000,
                "sample_n": 1550,
                "suppressed": False,
            },
        ],
    )
    expected_dataset_id = "derived.pums_burden_county_segmented"
    monkeypatch.setattr(
        "serving.terminal_cli._fetch_asset_detail",
        lambda _conn, ds: _make_asset_detail() if ds == expected_dataset_id else None,
    )

    runner = CliRunner()
    result = runner.invoke(main, ["burden", "bergen", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    prov = payload["provenance"]
    assert prov is not None
    assert prov["dataset_id"] == expected_dataset_id
    assert prov["freshness_state"] == "fresh"
    assert prov["last_rows_upserted"] == 10_349
    # The rest of the JSON body is unchanged by the provenance addition.
    assert payload["latest"]["year"] == 2022
    assert payload["n_years"] == 2


def test_burden_text_output_includes_provenance_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``burden bergen`` (no --json) prints the as-of footer below the table."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")

    class _ConnCM:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect",
        lambda *a, **k: _ConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.list_pums_burden_county_series",
        lambda *_a, **_k: [
            {
                "year": 2022,
                "burden_ratio_p50": 0.32,
                "burden_ratio_p50_se": 0.01,
                "weighted_n": 105_000,
                "sample_n": 1550,
                "suppressed": False,
            },
        ],
    )
    monkeypatch.setattr(
        "serving.terminal_cli._fetch_asset_detail",
        lambda _conn, _ds: _make_asset_detail(),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["burden", "bergen"])
    assert result.exit_code == 0, result.output
    # The footer line must appear; the precise format comes from
    # format_provenance_footer which is unit-tested above.
    assert "as-of" in result.output
    assert "fresh" in result.output
    assert "(derived.pums_burden_county_segmented)" in result.output


def test_health_model_matches_get_health_json_shape() -> None:
    """``health --json`` uses :class:`Health`; keys must stay aligned with GET /health."""
    ts = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.UTC)
    payload = Health(
        status="ok",
        db_reachable=True,
        n_errors_last_1h=0,
        timestamp=ts,
    )
    d = payload.model_dump(mode="json")
    assert set(d.keys()) == {
        "status",
        "db_reachable",
        "n_errors_last_1h",
        "api_version",
        "timestamp",
    }
    assert d["api_version"] == "0.1.0"


def test_asset_summary_matches_get_assets_json_shape() -> None:
    """``datasets --json`` uses :class:`AssetSummary`; keys align with GET /assets."""
    ts = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.UTC)
    row = AssetSummary(
        dataset_id="raw.foo",
        cadence="weekly",
        schedule_label=None,
        expected_lag_hours=24,
        last_materialized_at=ts,
        last_rows_upserted=100,
        age_hours=1.5,
        freshness_state="fresh",
        n_warn_30d=0,
        n_error_30d=1,
    )
    d = row.model_dump(mode="json")
    assert set(d.keys()) == {
        "dataset_id",
        "cadence",
        "schedule_label",
        "expected_lag_hours",
        "last_materialized_at",
        "last_rows_upserted",
        "age_hours",
        "freshness_state",
        "n_warn_30d",
        "n_error_30d",
    }


def test_release_calendar_panel_json_envelope() -> None:
    """``calendar --json`` uses :class:`ReleaseCalendarPanel` (GET /release-calendar)."""
    as_of = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.UTC)
    row = ReleaseCalendarHorizonRow(
        source_id="raw.foo",
        cadence="weekly",
        schedule_label="—",
        timezone="UTC",
        expected_lag_hours=24,
        notes=None,
        last_materialized_at=None,
        age_hours=None,
        freshness_state="unknown",
        overdue=False,
        upcoming_releases=[],
        next_expected_at=None,
        schedule_computed=False,
    )
    panel = ReleaseCalendarPanel(as_of=as_of, horizon_days=14, sources=[row])
    d = panel.model_dump(mode="json")
    assert set(d.keys()) == {"as_of", "horizon_days", "sources"}
    assert d["horizon_days"] == 14
    s0 = d["sources"][0]
    assert "upcoming_releases" in s0
    assert "next_expected_at" in s0


def test_ascii_sparkline_empty() -> None:
    assert ascii_sparkline([]) == ""


def test_ascii_sparkline_flat() -> None:
    s = ascii_sparkline([0.4, 0.4, 0.4])
    assert len(s) == 3
    assert len(set(s)) == 1


def test_ascii_sparkline_range() -> None:
    s = ascii_sparkline([0.0, 0.25, 0.5, 0.75, 1.0])
    assert len(s) == 5
    assert s[0] != s[-1]


def test_nj_cli_relays_subcommand_no_pg() -> None:
    """Relay mode runs the real nj-terminal command (no PG for catalog-only)."""
    runner = CliRunner()
    result = runner.invoke(nj_main, ["fec-metrics", "--catalog", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)


def test_parse_nj_relay_argv() -> None:
    assert parse_nj_relay_argv([]) is None
    assert parse_nj_relay_argv(["pums-series", "--county", "bergen"]) == [
        "pums-series",
        "--county",
        "bergen",
    ]
    assert parse_nj_relay_argv(["pums-burden", "00300", "--json"]) == [
        "pums-burden",
        "00300",
        "--json",
    ]
    assert parse_nj_relay_argv(["pums-burden-county", "bergen"]) == [
        "pums-burden-county",
        "bergen",
    ]
    assert parse_nj_relay_argv(
        ["pums-burden-county-series", "34003", "--json"],
    ) == [
        "pums-burden-county-series",
        "34003",
        "--json",
    ]
    assert parse_nj_relay_argv(["burden-latest", "--json"]) == ["burden-latest", "--json"]
    assert parse_nj_relay_argv(["counties", "--json"]) == ["counties", "--json"]
    assert parse_nj_relay_argv(["releases"]) == ["releases"]
    assert parse_nj_relay_argv(["fec-cycles", "--json"]) == ["fec-cycles", "--json"]
    assert parse_nj_relay_argv(["fec-money-nj", "--limit", "5"]) == [
        "fec-money-nj",
        "--limit",
        "5",
    ]
    assert parse_nj_relay_argv(["fec-candidates", "--limit", "10"]) == [
        "fec-candidates",
        "--limit",
        "10",
    ]
    assert parse_nj_relay_argv(["fec-committees", "--state", "NJ"]) == [
        "fec-committees",
        "--state",
        "NJ",
    ]
    assert parse_nj_relay_argv(["fec-candidate", "S4NJ00466", "--json"]) == [
        "fec-candidate",
        "S4NJ00466",
        "--json",
    ]
    assert parse_nj_relay_argv(["fec-committee", "C00540500"]) == [
        "fec-committee",
        "C00540500",
    ]
    assert parse_nj_relay_argv(["fec-export-candidates", "--state", "NJ"]) == [
        "fec-export-candidates",
        "--state",
        "NJ",
    ]
    assert parse_nj_relay_argv(
        ["fec-metric", "treasurer_concentration", "--limit", "5"],
    ) == [
        "fec-metric",
        "treasurer_concentration",
        "--limit",
        "5",
    ]
    assert parse_nj_relay_argv(["fec-contributions", "--cycle", "2024"]) == [
        "fec-contributions",
        "--cycle",
        "2024",
    ]
    assert parse_nj_relay_argv(["fec-enums", "states", "--cycle", "2024"]) == [
        "fec-enums",
        "states",
        "--cycle",
        "2024",
    ]
    assert parse_nj_relay_argv(["asset", "raw.foo", "--json"]) == [
        "asset",
        "raw.foo",
        "--json",
    ]
    assert parse_nj_relay_argv(["datasets", "--json"]) == ["datasets", "--json"]
    assert parse_nj_relay_argv(["bergen"]) == ["burden", "bergen"]
    assert parse_nj_relay_argv(["bergen", "acs"]) == ["acs-burden", "bergen"]
    assert parse_nj_relay_argv(["bergen", "--json"]) == ["burden", "bergen", "--json"]
    assert parse_nj_relay_argv(["hudson", "acs-burden", "-h"]) == [
        "acs-burden",
        "hudson",
        "-h",
    ]
    with pytest.raises(ValueError, match="Unknown metric"):
        parse_nj_relay_argv(["bergen", "widgets"])


def test_asset_cli_rejects_malformed_dataset_id_no_pg() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["asset", "NOT_A_VALID_ID"])
    assert result.exit_code != 0


def test_pums_burden_cli_rejects_bad_puma_no_pg() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["pums-burden", "bad"])
    assert result.exit_code != 0


def test_fec_candidates_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["fec-candidates", "--limit", "1"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_fec_candidate_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["fec-candidate", "S4NJ00466"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_fec_committee_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["fec-committee", "C00540500"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_fec_export_candidates_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["fec-export-candidates"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_fec_metric_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["fec-metric", "treasurer_concentration", "--limit", "1"],
        env={},
    )
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_fec_metric_unknown_metric_no_pg() -> None:
    """Validation fails before opening Postgres."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["fec-metric", "not_a_real_metric_id"],
        env={},
    )
    assert result.exit_code == 1
    combined = result.output + (result.stderr or "")
    assert "Unknown fraud metric" in combined
    assert "treasurer_concentration" in combined


def test_fec_contributions_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["fec-contributions", "--limit", "1"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_fec_money_nj_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["fec-money-nj", "--limit", "1"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_pums_burden_county_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["pums-burden-county"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_acs_burden_json_flags_mutually_exclusive() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["acs-burden", "bergen", "--json", "--raw-json"],
        env={},
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "not both" in combined.lower() or "either" in combined.lower()


def test_acs_burden_raw_json_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["acs-burden", "bergen", "--raw-json"],
        env={},
    )
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_acs_burden_raw_json_rejects_non_nj_fips_no_pg() -> None:
    """--raw-json enforces 34xxx before opening Postgres (400 parity)."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["acs-burden", "99999", "--raw-json"],
        env={},
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "34" in combined or "NJ" in combined or "Invalid" in combined


def test_pums_burden_county_series_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["pums-burden-county-series"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_pums_burden_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["pums-burden"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_pums_series_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["pums-series"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_burden_latest_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["burden-latest"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_fec_enums_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["fec-enums", "offices"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_fec_cycles_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["fec-cycles"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_releases_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["releases"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_counties_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["counties"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_asset_cli_requires_pg_dsn_when_id_well_formed() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["asset", "raw.fred_observation"],
        env={},
    )
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_fec_metrics_catalog_json_no_pg() -> None:
    """Static catalog path must not require PG_DSN."""
    runner = CliRunner()
    result = runner.invoke(main, ["fec-metrics", "--catalog", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) >= 1
    row0 = data[0]
    assert "id" in row0
    assert "tier" in row0
    assert "primary_key_cols" in row0


# ============================================================================
# fec-risk + fec-risk-entity (Tier 4 v3 step 6: API <-> CLI parity)
# ============================================================================


def test_fec_risk_help_no_pg() -> None:
    """--help must not require PG_DSN."""
    runner = CliRunner()
    result = runner.invoke(main, ["fec-risk", "--help"], env={})
    assert result.exit_code == 0, result.output
    assert "Risk-ranked entity queue" in result.output
    assert "--min-score" in result.output
    assert "--signal-id" in result.output


def test_fec_risk_entity_help_no_pg() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["fec-risk-entity", "--help"], env={})
    assert result.exit_code == 0, result.output
    assert "Evidence panel" in result.output


def test_fec_risk_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["fec-risk"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_fec_risk_entity_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["fec-risk-entity", "treasurer", "DOE, JOHN"], env={},
    )
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_fec_risk_entity_invalid_kind_rejected_by_click() -> None:
    """Click's Choice converter rejects unknown entity_kind before any DB."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["fec-risk-entity", "politician", "whoever"], env={},
    )
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "Invalid value" in combined or "not one of" in combined


def test_fec_risk_invalid_sort_rejected_by_click() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["fec-risk", "--sort-by", "1; DROP TABLE foo"],
        env={"PG_DSN": "postgresql://unused"},
    )
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "Invalid value" in combined or "not one of" in combined


def test_fec_risk_invalid_min_score_rejected_by_click() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["fec-risk", "--min-score", "150"],
        env={"PG_DSN": "postgresql://unused"},
    )
    assert result.exit_code == 2  # FloatRange rejection


def test_fec_risk_json_empty_queue_matches_api_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fec-risk --json`` with no matching rows must return the same
    envelope shape as ``GET /fec/risk/entities`` -- not error out."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")

    class _ConnCM:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect",
        lambda *a, **k: _ConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.list_risk_entities",
        lambda *_a, **_k: ([], 0),
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["fec-risk", "--cycle", "2024", "--min-score", "60", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["rows"] == []
    assert payload["total_count"] == 0
    assert payload["limit"] == 100
    assert payload["offset"] == 0
    f = payload["filters"]
    assert f["cycle"] == "2024"
    assert f["entity_kind"] is None
    assert f["signal_id"] is None
    assert f["min_score"] == 60
    assert f["max_score"] is None
    assert f["sort_by"] == "risk_score"
    assert f["sort_dir"] == "DESC"


def test_fec_risk_json_keyerror_translates_to_click_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitelist failure that escapes Click (e.g. defense-in-depth in
    list_risk_entities) becomes a non-zero CLI exit, not a stack trace."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")

    class _ConnCM:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect",
        lambda *a, **k: _ConnCM(),
    )

    def _raise_keyerror(*_a: object, **_k: object) -> tuple[list[object], int]:
        raise KeyError("Sort column 'evil' not allowed. Allowed: ['risk_score']")

    monkeypatch.setattr(
        "serving.terminal_cli.list_risk_entities", _raise_keyerror,
    )

    runner = CliRunner()
    result = runner.invoke(main, ["fec-risk"])
    assert result.exit_code == 1
    combined = result.output + (result.stderr or "")
    assert "not allowed" in combined


def test_fec_risk_entity_404_translates_to_click_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown entity (no fired signals) -> CLI prints the same message
    as the HTTP 404 detail, exit 1."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")

    class _ConnCM:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect",
        lambda *a, **k: _ConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.get_risk_entity",
        lambda *_a, **_k: None,
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["fec-risk-entity", "treasurer", "DOES_NOT_EXIST", "--cycle", "2024"],
    )
    assert result.exit_code == 1
    combined = result.output + (result.stderr or "")
    assert "No fired signals" in combined
    assert "DOES_NOT_EXIST" in combined
    assert "2024" in combined


def test_fec_risk_entity_json_decomposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub get_risk_entity with a known panel and check the JSON shape
    matches what the HTTP layer would return (RiskEntityPanel)."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")

    class _ConnCM:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    fake_panel = {
        "cycle":               "2024",
        "entity_kind":         "treasurer",
        "entity_id":           "DOE, JOHN",
        "risk_score":          72.50,
        "n_signals_fired":     1,
        "max_severity":        3,
        "max_peer_percentile": 0.99,
        "avg_peer_percentile": 0.99,
        "primary_peer_bucket": "kind=treasurer",
        "last_observation_at": dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.UTC),
        "observations":        [
            {
                "signal_id":         "treasurer_concentration",
                "severity":          3,
                "peer_percentile":   0.99,
                "peer_bucket":       "kind=treasurer",
                "raw_value":         3.0,
                "evidence_url":      "/fec/metrics/treasurer_concentration?cycle=2024",
                "phi_contribution":  3 * (0.04 ** 2),
                "score_share_pct":   100.0,
            },
        ],
    }

    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect",
        lambda *a, **k: _ConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.get_risk_entity",
        lambda *_a, **_k: fake_panel,
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["fec-risk-entity", "treasurer", "DOE, JOHN", "--cycle", "2024", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["entity_kind"] == "treasurer"
    assert payload["entity_id"]   == "DOE, JOHN"
    assert payload["cycle"]       == "2024"
    assert payload["risk_score"]  == 72.50
    obs = payload["observations"]
    assert len(obs) == 1
    assert obs[0]["signal_id"]       == "treasurer_concentration"
    assert obs[0]["score_share_pct"] == 100.0
    assert obs[0]["evidence_url"].startswith("/fec/metrics/")


def test_fec_risk_entity_json_and_csv_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PG_DSN", "postgresql://unused")
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["fec-risk-entity", "treasurer", "DOE, JOHN", "--json", "--csv"],
    )
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "Choose either --json or --csv" in combined


def test_fec_risk_entity_csv_writes_one_row_per_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI --csv emits the same column shape as the HTTP route. Stub
    get_risk_entity so we don't need a live database."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")

    class _ConnCM:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    fake_panel = {
        "cycle":               "2024",
        "entity_kind":         "treasurer",
        "entity_id":           "DOE, JOHN",
        "risk_score":          72.50,
        "n_signals_fired":     2,
        "max_severity":        3,
        "max_peer_percentile": 0.99,
        "avg_peer_percentile": 0.97,
        "primary_peer_bucket": "kind=treasurer",
        "last_observation_at": dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.UTC),
        "observations": [
            {
                "signal_id":         "treasurer_concentration",
                "severity":          3,
                "peer_percentile":   0.99,
                "peer_bucket":       "kind=treasurer",
                "raw_value":         3.0,
                "evidence_url":      "/fec/metrics/treasurer_concentration?cycle=2024",
                "phi_contribution":  0.0048,
                "score_share_pct":   60.0,
            },
            {
                "signal_id":         "no_pcc",
                "severity":          4,
                "peer_percentile":   0.96,
                "peer_bucket":       "state=NJ",
                "raw_value":         None,
                "evidence_url":      "/fec/metrics/no_pcc?cycle=2024",
                "phi_contribution":  0.0032,
                "score_share_pct":   40.0,
            },
        ],
    }

    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect",
        lambda *a, **k: _ConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.get_risk_entity",
        lambda *_a, **_k: fake_panel,
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "fec-risk-entity", "treasurer", "DOE, JOHN",
            "--cycle", "2024", "--csv",
        ],
    )
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln]
    # Header: comma-joined column list.
    from serving.queries_fec_risk import EVIDENCE_CSV_COLUMNS
    assert lines[0] == ",".join(EVIDENCE_CSV_COLUMNS)
    # Two observations -> two data rows.
    assert len(lines) == 3
    # entity_id contains a comma -> quoted.
    assert all('"DOE, JOHN"' in ln for ln in lines[1:])
    assert "treasurer_concentration" in lines[1]
    assert "no_pcc" in lines[2]


def test_nj_relay_forwards_fec_risk() -> None:
    """The ``nj`` relay must reach ``fec-risk`` and ``fec-risk-entity``.

    We drive each via ``env={}`` so the inner command's PG_DSN guard
    fires (exit 2 with a "PG_DSN" message). That proves the relay
    routed to the right subcommand: any other command would either
    error out at Click parsing (exit 2 with a different message) or
    succeed (exit 0 -- e.g. ``fec-metrics --catalog``).

    ``--help`` cannot be used here because Click consumes it at the
    relay's own option layer and never forwards.
    """
    runner = CliRunner()
    for argv in (
        ["fec-risk"],
        ["fec-risk-entity", "treasurer", "DOE, JOHN"],
    ):
        result = runner.invoke(nj_main, argv, env={})
        assert result.exit_code == 2, result.output
        combined = result.output + (result.stderr or "")
        assert "PG_DSN" in combined


# ============================================================================
# HPI + INCOME per-county series (BBG-LIKE-1 metric expansion)
# ============================================================================
#
# Each test uses the same connection-shim + monkeypatch pattern as the burden
# tests above so we can exercise the JSON / text branches without a live
# Postgres. The shapes asserted here are the contract the API publishes to
# the UI, so they must not drift.


class _HPIConnCM:
    """Reusable null connection context manager for HPI/INCOME tests."""

    def __enter__(self) -> object:
        return object()

    def __exit__(self, *args: object) -> None:
        return None


_HPI_FAKE_ROWS = [
    {
        "county_fips":    "34003",
        "county_name":    "Bergen",
        "year":           2000,
        "hpi_indexed":    100.000,
        "hpi_raw":        205.5,
        "base_year_used": 2000,
        "annual_change":  None,
        "n_transactions": 1234,
    },
    {
        "county_fips":    "34003",
        "county_name":    "Bergen",
        "year":           2001,
        "hpi_indexed":    105.250,
        "hpi_raw":        216.3,
        "base_year_used": 2000,
        "annual_change":  0.0525,
        "n_transactions": 1300,
    },
    {
        "county_fips":    "34003",
        "county_name":    "Bergen",
        "year":           2022,
        "hpi_indexed":    310.420,
        "hpi_raw":        637.9,
        "base_year_used": 2000,
        "annual_change":  0.0710,
        "n_transactions": 1812,
    },
]


_INCOME_FAKE_ROWS = [
    {
        "county_fips":      "34003",
        "county_name":      "Bergen",
        "year":             2010,
        "product":          "acs5",
        "estimate_real":    100_500.0,
        "estimate_nominal":  82_300.0,
        "deflator":          1.221,
        "base_year_used":   2022,
        "dollar_year":      2010,
        "margin_of_error":   1450.0,
    },
    {
        "county_fips":      "34003",
        "county_name":      "Bergen",
        "year":             2022,
        "product":          "acs5",
        "estimate_real":    113_200.0,
        "estimate_nominal": 113_200.0,
        "deflator":          1.000,
        "base_year_used":   2022,
        "dollar_year":      2022,
        "margin_of_error":   1620.0,
    },
]


def test_hpi_json_includes_provenance_and_sparkline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hpi --json`` mirrors GET /hpi/{fips}/series + carries provenance."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")
    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect", lambda *a, **k: _HPIConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.list_hpi_county_series",
        lambda *_a, **_k: _HPI_FAKE_ROWS,
    )
    expected_dataset_id = "raw.fhfa_hpi_county"
    monkeypatch.setattr(
        "serving.terminal_cli._fetch_asset_detail",
        lambda _conn, ds: (
            _make_asset_detail(dataset_id=expected_dataset_id)
            if ds == expected_dataset_id else None
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["hpi", "bergen", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["metric"] == "fhfa_hpi_indexed"
    assert payload["county_fips"] == "34003"
    assert payload["county_name"] == "Bergen"
    assert payload["base_year"] == 2000
    assert payload["n_years"] == 3
    assert payload["year_range"] == [2000, 2022]
    assert payload["latest"]["year"] == 2022
    assert payload["latest"]["hpi_indexed"] == 310.420
    # Sparkline is non-empty when we have rows.
    assert len(payload["sparkline_ascii"]) == 3
    # Series is the full Pydantic shape.
    assert len(payload["series"]) == 3
    assert payload["series"][0]["base_year_used"] == 2000
    # BBG-LIKE-2: provenance points at the right dataset_id.
    assert payload["provenance"]["dataset_id"] == expected_dataset_id


def test_hpi_json_empty_series_does_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``GET /hpi/{fips}/series`` is []; ``hpi --json`` mirrors that."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")
    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect", lambda *a, **k: _HPIConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.list_hpi_county_series",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "serving.terminal_cli._fetch_asset_detail",
        lambda _conn, _ds: None,
    )

    runner = CliRunner()
    result = runner.invoke(main, ["hpi", "bergen", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["series"] == []
    assert payload["n_years"] == 0
    assert payload["latest"] is None
    assert payload["year_range"] is None
    assert payload["sparkline_ascii"] == ""
    assert payload["provenance"] is None


def test_hpi_text_output_includes_provenance_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text mode renders the table + as-of footer with the right dataset id."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")
    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect", lambda *a, **k: _HPIConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.list_hpi_county_series",
        lambda *_a, **_k: _HPI_FAKE_ROWS,
    )
    monkeypatch.setattr(
        "serving.terminal_cli._fetch_asset_detail",
        lambda _conn, _ds: _make_asset_detail(dataset_id="raw.fhfa_hpi_county"),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["hpi", "bergen"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Bergen" in out
    assert "2000" in out          # base year row
    assert "as-of" in out
    assert "(raw.fhfa_hpi_county)" in out


def test_hpi_text_empty_series_errors_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --json, an empty series is a hard error so the analyst notices."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")
    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect", lambda *a, **k: _HPIConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.list_hpi_county_series",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "serving.terminal_cli._fetch_asset_detail",
        lambda _conn, _ds: None,
    )

    runner = CliRunner()
    result = runner.invoke(main, ["hpi", "bergen"])
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "No FHFA HPI series" in combined
    assert "raw.fhfa_hpi_county" in combined


def test_hpi_cli_requires_pg_dsn() -> None:
    """Same DSN-guard contract as every other metric command."""
    runner = CliRunner()
    result = runner.invoke(main, ["hpi", "bergen"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_hpi_cli_rejects_missing_county() -> None:
    """No COUNTY argument -> usage error before any DB connect."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["hpi"], env={"PG_DSN": "postgresql://unused"},
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "COUNTY" in combined or "--county" in combined


def test_hpi_base_year_out_of_range_rejected_by_click() -> None:
    """Click's IntRange enforces the [1975, 2030] window before any DB hit."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["hpi", "bergen", "--base-year", "1800"],
        env={"PG_DSN": "postgresql://unused"},
    )
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "Invalid value" in combined or "not in the range" in combined


def test_income_json_uses_resolved_default_base_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When --base-year is omitted, the CLI calls resolve_default_income_base_year
    and forwards the result to list_income_county_series."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")
    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect", lambda *a, **k: _HPIConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_default_income_base_year",
        lambda _conn: 2022,
    )

    captured: dict[str, object] = {}

    def _fake_list(_conn: object, **kwargs: object) -> list[dict[str, object]]:
        captured.update(kwargs)
        return _INCOME_FAKE_ROWS

    monkeypatch.setattr(
        "serving.terminal_cli.list_income_county_series", _fake_list,
    )
    monkeypatch.setattr(
        "serving.terminal_cli._fetch_asset_detail",
        lambda _conn, _ds: _make_asset_detail(
            dataset_id="raw.acs_median_household_income",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["income", "bergen", "--json"])
    assert result.exit_code == 0, result.output
    assert captured["base_year"] == 2022
    assert captured["product"] == "acs5"

    payload = json.loads(result.output)
    assert payload["metric"] == "acs_mhi_real"
    assert payload["base_year"] == 2022
    assert payload["product"] == "acs5"
    assert payload["latest"]["estimate_real"] == 113_200.0
    assert payload["provenance"]["dataset_id"] == "raw.acs_median_household_income"
    assert len(payload["series"]) == 2


def test_income_default_base_year_unresolvable_errors_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If both ACS and CPI tables are empty, the CLI must error out
    rather than silently picking a wrong year."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")
    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect", lambda *a, **k: _HPIConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_default_income_base_year",
        lambda _conn: None,
    )

    runner = CliRunner()
    result = runner.invoke(main, ["income", "bergen"])
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "default deflation base year" in combined
    assert "--base-year" in combined


def test_income_explicit_base_year_skips_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--base-year 2020`` must NOT call the resolver."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")
    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect", lambda *a, **k: _HPIConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )

    def _resolver_should_not_be_called(_conn: object) -> int | None:
        msg = "resolve_default_income_base_year called when --base-year is set"
        raise AssertionError(msg)

    monkeypatch.setattr(
        "serving.terminal_cli.resolve_default_income_base_year",
        _resolver_should_not_be_called,
    )
    monkeypatch.setattr(
        "serving.terminal_cli.list_income_county_series",
        lambda *_a, **_k: _INCOME_FAKE_ROWS,
    )
    monkeypatch.setattr(
        "serving.terminal_cli._fetch_asset_detail",
        lambda _conn, _ds: None,
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["income", "bergen", "--base-year", "2020", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["base_year"] == 2020


def test_income_json_empty_series_does_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``GET /income/{fips}/series`` is []; ``income --json`` mirrors that."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")
    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect", lambda *a, **k: _HPIConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_default_income_base_year",
        lambda _conn: 2022,
    )
    monkeypatch.setattr(
        "serving.terminal_cli.list_income_county_series",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "serving.terminal_cli._fetch_asset_detail",
        lambda _conn, _ds: None,
    )

    runner = CliRunner()
    result = runner.invoke(main, ["income", "bergen", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["series"] == []
    assert payload["n_years"] == 0
    assert payload["latest"] is None
    assert payload["year_range"] is None
    assert payload["sparkline_ascii"] == ""
    assert payload["base_year"] == 2022


def test_income_text_output_includes_provenance_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text mode renders the table + as-of footer for the right dataset id."""
    monkeypatch.setenv("PG_DSN", "postgresql://unused")
    monkeypatch.setattr(
        "serving.terminal_cli.psycopg.connect", lambda *a, **k: _HPIConnCM(),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_county_fips",
        lambda _conn, _raw: ("34003", "Bergen"),
    )
    monkeypatch.setattr(
        "serving.terminal_cli.resolve_default_income_base_year",
        lambda _conn: 2022,
    )
    monkeypatch.setattr(
        "serving.terminal_cli.list_income_county_series",
        lambda *_a, **_k: _INCOME_FAKE_ROWS,
    )
    monkeypatch.setattr(
        "serving.terminal_cli._fetch_asset_detail",
        lambda _conn, _ds: _make_asset_detail(
            dataset_id="raw.acs_median_household_income",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["income", "bergen"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Bergen" in out
    assert "as-of" in out
    assert "(raw.acs_median_household_income)" in out


def test_income_cli_requires_pg_dsn() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["income", "bergen"], env={})
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "PG_DSN" in combined


def test_income_invalid_product_rejected_by_click() -> None:
    """Click's Choice rejects unknown product strings before any DB hit."""
    runner = CliRunner()
    result = runner.invoke(
        main, ["income", "bergen", "--product", "acs10"],
        env={"PG_DSN": "postgresql://unused"},
    )
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "Invalid value" in combined or "not one of" in combined


# ============================================================================
# nj relay parity for hpi + income
# ============================================================================


def test_nj_relay_maps_county_to_hpi() -> None:
    """``nj BERGEN HPI`` resolves to ``nj-terminal hpi BERGEN``."""
    assert parse_nj_relay_argv(["bergen", "hpi"]) == ["hpi", "bergen"]
    assert parse_nj_relay_argv(["bergen", "prices"]) == ["hpi", "bergen"]
    assert parse_nj_relay_argv(["bergen", "fhfa"]) == ["hpi", "bergen"]


def test_nj_relay_maps_county_to_income() -> None:
    """``nj BERGEN INCOME`` resolves to ``nj-terminal income BERGEN``."""
    assert parse_nj_relay_argv(["bergen", "income"]) == ["income", "bergen"]
    assert parse_nj_relay_argv(["bergen", "mhi"]) == ["income", "bergen"]
    assert parse_nj_relay_argv(["bergen", "acs-income"]) == ["income", "bergen"]


def test_nj_relay_forwards_hpi_subcommand() -> None:
    """Direct ``hpi`` and ``income`` subcommands pass through unchanged."""
    assert parse_nj_relay_argv(["hpi", "bergen", "--json"]) == [
        "hpi", "bergen", "--json",
    ]
    assert parse_nj_relay_argv(
        ["income", "bergen", "--product", "acs1"],
    ) == ["income", "bergen", "--product", "acs1"]


@pytest.mark.live_pg
def test_resolve_county_fips_live() -> None:
    """Requires seeded ref.county (NJ)."""
    import psycopg

    dsn = os.environ.get("PG_TEST_DSN")
    if not dsn:
        pytest.skip("PG_TEST_DSN not set")

    with psycopg.connect(dsn) as conn:
        r = resolve_county_fips(conn, "bergen")
        assert r is not None
        assert r[0] == "34003"
        assert "Bergen" in r[1]

        r2 = resolve_county_fips(conn, "34003")
        assert r2 == r
