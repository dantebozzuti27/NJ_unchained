"""Bloomberg-style terminal shortcuts over curated SQL (BBG-LIKE-1).

Reads ``PG_DSN`` and opens a single psycopg connection per invocation —
no HTTP hop, no pool lifecycle — suitable for cron + analyst shells.

Example::

    export PG_DSN=postgresql://localhost/nj_dev
    nj bergen
    nj-terminal burden bergen
    nj-terminal burden --county "atlantic" --product acs1 --tenure owner_w_mtg
    nj-terminal acs-burden bergen
    nj-terminal acs-burden bergen --raw-json
    nj-terminal datasets
    nj-terminal calendar --days 14
    nj-terminal health
    nj-terminal health --json
    nj-terminal burden bergen --json
    nj-terminal fec-summary --json
    nj-terminal fec-cycles --json
    nj-terminal fec-enums states --cycle 2024 --json
    nj-terminal fec-metrics --cycle 2024
    nj-terminal fec-money-nj --cycle 2024 --limit 20 --json
    nj-terminal fec-contributions --donor-state NJ --limit 50 --json
    nj-terminal fec-candidates --state NJ --cycle 2024 --json
    nj-terminal fec-candidate S4NJ00466 --json
    nj-terminal fec-committee C00540500 --cycle 2024 --json
    nj-terminal fec-export-candidates --state NJ --cycle 2024 -o /tmp/c.csv
    nj-terminal fec-metric treasurer_concentration --cycle 2024 --limit 20 --json
    nj-terminal fec-metrics --catalog
    nj-terminal fec-risk --cycle 2024 --min-score 60 --json
    nj-terminal fec-risk-entity treasurer "DOE, JOHN" --cycle 2024 --json
    nj-terminal asset raw.fred_observation
    nj-terminal counties --json
    nj-terminal burden-latest --json
    nj-terminal pums-series --county bergen --json
    nj-terminal pums-burden 00300 --dim race --json
    nj-terminal pums-burden-county bergen --dim overall --json
    nj-terminal pums-burden-county-series bergen --json
    nj-terminal releases --json
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import sys
from decimal import Decimal
from typing import TYPE_CHECKING, Any, BinaryIO
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Iterator

import click
import psycopg
from rich.console import Console
from rich.table import Table

from serving.models import (
    AssetDetail,
    AssetSummary,
    BurdenRow,
    CountyRef,
    FecCandidateDetail,
    FecCandidateRow,
    FecCommitteeDetail,
    FecCommitteeRow,
    FecContributionRow,
    FecEnumValue,
    FecMoneyToNjRow,
    FecPagedResponse,
    FraudMetricCatalogEntry,
    FraudMetricResult,
    Health,
    HpiCountyRow,
    IncomeCountyRow,
    PumsBurdenCountyRow,
    PumsBurdenCountySeriesRow,
    PumsBurdenRow,
    ReleaseCalendarHorizonRow,
    ReleaseCalendarPanel,
    ReleaseCalendarRow,
    RiskEntityPanel,
    RiskQueueResponse,
    RiskQueueRow,
    RiskSignalObservation,
)
from serving.queries import (
    DEFAULT_PUMS_PRODUCT,
    HPI_DEFAULT_BASE_YEAR,
    compute_freshness_state,
    count_recent_errors,
    get_asset_detail,
    list_assets,
    list_burden_for_county,
    list_burden_latest_year_nj,
    list_counties,
    list_hpi_county_series,
    list_income_county_series,
    list_pums_burden_county_for_county,
    list_pums_burden_county_latest,
    list_pums_burden_county_series,
    list_pums_burden_for_puma,
    list_pums_burden_latest,
    list_release_calendar,
    list_release_calendar_detailed,
    resolve_default_income_base_year,
    select_one,
)
from serving.queries_fec import (
    MAX_EXPORT,
    CandidateFilters,
    CommitteeFilters,
    ContributionFilters,
    MoneyToNjFilters,
    clear_summary_cache,
    get_candidate_detail,
    get_committee_detail,
    get_summary,
    list_candidates,
    list_committees,
    list_contributions,
    list_distinct_cycles,
    list_distinct_offices,
    list_distinct_parties,
    list_distinct_states,
    list_money_to_nj,
    stream_candidates,
    stream_committees,
    stream_contributions,
    stream_money_to_nj,
)
from serving.queries_fec_metrics import (
    MetricSpec,
    get_catalog,
    get_metric,
    list_metric,
    metric_counts,
    stream_metric,
)
from serving.queries_fec_risk import (
    DEFAULT_LIMIT as RISK_DEFAULT_LIMIT,
)
from serving.queries_fec_risk import (
    DEFAULT_SORT_BY as RISK_DEFAULT_SORT_BY,
)
from serving.queries_fec_risk import (
    EVIDENCE_CSV_COLUMNS as RISK_EVIDENCE_CSV_COLUMNS,
)
from serving.queries_fec_risk import (
    MAX_LIMIT as RISK_MAX_LIMIT,
)
from serving.queries_fec_risk import (
    SORT_COLS as RISK_SORT_COLS,
)
from serving.queries_fec_risk import (
    VALID_ENTITY_KINDS,
    get_risk_entity,
    list_risk_entities,
)
from serving.queries_fec_risk import (
    evidence_csv_rows as risk_evidence_csv_rows,
)
from serving.release_schedule import compute_release_calendar_row
from serving.routes.assets import _DATASET_ID_RE

CLI_VERSION = "0.34.0"
_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
_PUMA_5_RE = re.compile(r"^[0-9]{5}$")  # PUMA or 5-digit county FIPS
_ET = ZoneInfo("America/New_York")


def _fmt_et(z: dt.datetime | None) -> str:
    if z is None:
        return "—"
    if z.tzinfo is None:
        z = z.replace(tzinfo=dt.UTC)
    local = z.astimezone(_ET)
    return local.strftime("%Y-%m-%d %H:%M ET")


# ============================================================================
# Provenance / "as-of" footer (BBG-LIKE-2)
# ============================================================================
#
# Every BBG quote carries a discrete "as-of" stamp ("BERGEN BURDEN  0.32  AS OF
# 2026-04-29 09:32 EDT  source: Census ACS 5Y"). Our metric commands print the
# value but historically have not printed the as-of context, which is the
# difference between an analyst-grade tool and an analyst-toy. The two helpers
# below compose with any per-county detail command:
#
#   prov = _fetch_asset_detail(conn, "derived.pums_burden_county_segmented")
#   console.print(_format_provenance_footer(prov))   # rich table footer
#   payload["provenance"] = prov.model_dump(mode="json") if prov else None
#
# Reuses the same Pydantic AssetDetail shape as GET /assets/{schema}/{table};
# the only divergence from the API is presentational (one-line text format
# vs. structured JSON).
# ============================================================================


def _fetch_asset_detail(
    conn: psycopg.Connection, dataset_id: str,
) -> AssetDetail | None:
    """Return the AssetDetail Pydantic model for ``dataset_id``, or None.

    None when the dataset has no calendar entry AND no governance
    materialization signal -- caller decides whether that's "—" in the UI
    or a hard error.
    """
    row = get_asset_detail(conn, dataset_id=dataset_id)
    if row is None:
        return None
    state, age_h = compute_freshness_state(
        last_materialized_at=row.get("last_materialized_at"),
        expected_lag_hours=row.get("expected_lag_hours"),
    )
    return AssetDetail(
        dataset_id=row["dataset_id"],
        cadence=row.get("cadence"),
        schedule_label=row.get("schedule_label"),
        expected_lag_hours=row.get("expected_lag_hours"),
        last_materialized_at=row.get("last_materialized_at"),
        last_rows_upserted=row.get("last_rows_upserted"),
        age_hours=age_h,
        freshness_state=state,
        n_warn_30d=row.get("n_warn_30d", 0),
        n_error_30d=row.get("n_error_30d", 0),
        last_materialization_details=row.get("last_materialization_details"),
    )


def _fmt_age_hours(age_hours: float | None) -> str:
    """Pretty-print fractional hours: '3.2h ago' / '2.1d ago' / '12m ago'."""
    if age_hours is None:
        return "age=?"
    if age_hours < 1.0:
        minutes = round(age_hours * 60)
        return f"{minutes}m ago"
    if age_hours < 48.0:
        return f"{age_hours:.1f}h ago"
    days = age_hours / 24.0
    return f"{days:.1f}d ago"


def format_provenance_footer(prov: AssetDetail | None) -> str:
    """One-line BBG-style as-of stamp.

    Stable text contract; tests pin the format. Fields:
      ``as-of <ET timestamp>  ·  <age>  ·  <freshness>  ·  <rows>  ·
      <warns>w/<errs>e  (<dataset_id>)``

    Square brackets are intentionally avoided. Rich's ``console.print``
    interprets ``[foo]`` as markup; the dataset_id is exactly the kind
    of token (``derived.pums_burden_county_segmented``) that would
    silently break a ``[dim]...[/dim]`` wrapper if put inside brackets.

    When ``prov`` is None or has never materialized, returns a
    transparent "no signal" line that does not pretend to be data:

      ``as-of —  (no materialization signal)``         -- prov None
      ``as-of —  (<dataset_id>; never materialized)``  -- materialized=null
    """
    if prov is None:
        return "as-of —  (no materialization signal)"
    if prov.last_materialized_at is None:
        return f"as-of —  ({prov.dataset_id}; never materialized)"
    ts = _fmt_et(prov.last_materialized_at)
    age_s = _fmt_age_hours(prov.age_hours)
    rows_s = (
        f"{prov.last_rows_upserted:,} rows"
        if prov.last_rows_upserted is not None
        else "—"
    )
    health_s = f"{prov.n_warn_30d}w/{prov.n_error_30d}e"
    return (
        f"as-of {ts}  ·  {age_s}  ·  {prov.freshness_state}  ·  "
        f"{rows_s}  ·  {health_s}  ({prov.dataset_id})"
    )


def _provenance_to_json(prov: AssetDetail | None) -> dict[str, Any] | None:
    """Serialize provenance for the JSON output; None when unavailable."""
    return prov.model_dump(mode="json") if prov is not None else None


def ascii_sparkline(values: list[float]) -> str:
    """Map a numeric series to eight-height unicode sparkline blocks."""
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return _SPARK_BLOCKS[4] * len(values)
    out: list[str] = []
    span = hi - lo
    for v in values:
        idx = int((v - lo) / span * 7)
        out.append(_SPARK_BLOCKS[idx])
    return "".join(out)


def _nj_fips(s: str) -> bool:
    return bool(re.fullmatch(r"34\d{3}", s.strip()))


def resolve_county_fips(conn: psycopg.Connection, q: str) -> tuple[str, str] | None:
    """Return (county_fips, canonical_name) or None if not found."""
    raw = q.strip()
    if not raw:
        return None
    rows = list_counties(conn, state_code="NJ")
    by_fips = {r["county_fips"]: r["name"] for r in rows}
    if _nj_fips(raw):
        fips = raw.strip()
        name = by_fips.get(fips)
        if name:
            return fips, name
        raise click.ClickException(f"Unknown NJ county FIPS {fips!r}.")

    key = raw.lower()
    exact = [r for r in rows if r["name"].lower() == key]
    if len(exact) == 1:
        return exact[0]["county_fips"], exact[0]["name"]
    partial = [r for r in rows if key in r["name"].lower()]
    if len(partial) == 1:
        return partial[0]["county_fips"], partial[0]["name"]
    if len(partial) > 1:
        names = ", ".join(sorted(p["name"] for p in partial))
        raise click.ClickException(
            f"Ambiguous county {raw!r}; matches: {names}. Use full name or 34xxx FIPS.",
        )
    raise click.ClickException(
        f"Unknown NJ county {raw!r}. Try a name (e.g. Bergen) or 5-digit FIPS.",
    )


def _fmt_ratio_val(x: object) -> str:
    if x is None:
        return "—"
    if isinstance(x, (int, float, Decimal)):
        return f"{float(x):.4f}"
    return str(x)


def _maybe_float(x: object) -> float | None:
    if x is None:
        return None
    if isinstance(x, (int, float, Decimal)):
        return float(x)
    return None


def _require_dsn() -> str:
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        click.echo("PG_DSN must be set.", err=True)
        sys.exit(2)
    return dsn


def _json_dump(data: object) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


_FEC_ENUM_KIND_HTTP: dict[str, str] = {
    "cycles": "GET /fec/cycles",
    "states": "GET /fec/states",
    "parties": "GET /fec/parties",
    "offices": "GET /fec/offices",
}


def _fec_enum_rows(
    conn: psycopg.Connection,
    *,
    kind: str,
    cycle: str | None,
) -> list[FecEnumValue]:
    if kind == "cycles":
        rows = list_distinct_cycles(conn)
    elif kind == "states":
        rows = list_distinct_states(conn, cycle=cycle)
    elif kind == "parties":
        rows = list_distinct_parties(conn, cycle=cycle)
    elif kind == "offices":
        rows = list_distinct_offices(conn, cycle=cycle)
    else:
        raise AssertionError(kind)
    return [FecEnumValue.model_validate(r) for r in rows]


def _emit_fec_enum_panel(kind: str, cycle: str | None, as_json: bool) -> None:
    """Human or JSON output for one FEC enum endpoint."""
    k = kind.lower()
    eff_cycle = None if k == "cycles" else cycle
    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        refs = _fec_enum_rows(conn, kind=k, cycle=eff_cycle)

    if as_json:
        _json_dump([x.model_dump(mode="json") for x in refs])
        return

    titles = {
        "cycles": "FEC cycles (raw.fec_candidate)",
        "states": "FEC cand_office_st",
        "parties": "FEC cand_pty_affiliation",
        "offices": "FEC cand_office (H/S/P)",
    }
    title = titles[k]
    if eff_cycle:
        title = f"{title} · cycle={eff_cycle}"

    console = Console(highlight=False)
    table = Table(title=title, show_lines=False)
    if k == "cycles":
        table.add_column("cycle", style="cyan", no_wrap=True)
        table.add_column("candidates", justify="right")
    else:
        table.add_column("value", style="cyan", no_wrap=True)
        table.add_column("count", justify="right")
    for x in refs:
        table.add_row(x.value, str(x.count))
    console.print(table)
    console.print(f"[dim]Parallel to {_FEC_ENUM_KIND_HTTP[k]}.[/dim]")


@click.group()
@click.version_option(version=CLI_VERSION, prog_name="nj-terminal")
def main() -> None:
    """NJ affordability terminal: one-shot metrics from Postgres (no HTTP).

    Try ``burden``, ``pums-series``, ``pums-burden``, ``pums-burden-county``,
    ``pums-burden-county-series``, ``acs-burden``, ``burden-latest``,
    ``counties``, ``datasets``, ``asset``, ``releases``, ``calendar``,
    ``health``, ``fec-summary``, ``fec-cycles``, ``fec-enums``,
    ``fec-metrics``, ``fec-money-nj``, ``fec-contributions``,
    ``fec-candidates``, ``fec-committees``.
    """


@main.command("burden")
@click.argument("county_pos", metavar="[COUNTY]", required=False)
@click.option(
    "--county",
    "-c",
    help="County name, 34xxx FIPS, or any 5-digit county_fips (overrides positional).",
)
@click.option(
    "--product",
    "-p",
    type=click.Choice(["acs5", "acs1"]),
    default="acs5",
    show_default=True,
    help="ACS PUMS product backing derived.pums_burden_county_segmented.",
)
@click.option(
    "--tenure",
    "-t",
    type=click.Choice(["renter", "owner_w_mtg", "owner_no_mtg"]),
    default="renter",
    show_default=True,
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON (series rows + sparkline).",
)
def cmd_burden(
    county_pos: str | None,
    county: str | None,
    product: str,
    tenure: str,
    as_json: bool,
) -> None:
    """Latest PUMS county burden ratio + multi-year sparkline (overall segment).

    Matches the housing UI Trend view: segment ``overall``, tenure-filtered.
    ``--json`` with no series rows mirrors ``GET /pums-burden-county-series``
    (HTTP 200 with an empty list), not a terminal error.
    """
    dsn = _require_dsn()

    target = (county or county_pos or "").strip()
    if not target:
        raise click.UsageError("Provide COUNTY (e.g. bergen) or --county.")

    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        if _PUMA_5_RE.fullmatch(target):
            county_fips = target
            cref = list_counties(conn, state_code="NJ")
            county_name = next(
                (r["name"] for r in cref if r["county_fips"] == county_fips),
                county_fips,
            )
        else:
            resolved = resolve_county_fips(conn, target)
            if resolved is None:
                raise click.UsageError("County required.")
            county_fips, county_name = resolved

        series = list_pums_burden_county_series(
            conn,
            county_fips=county_fips,
            product=product,
            tenure=tenure,
            include_suppressed=False,
        )
        prov = _fetch_asset_detail(conn, "derived.pums_burden_county_segmented")

    series.sort(key=lambda r: r["year"])
    ratios = [float(r["burden_ratio_p50"]) for r in series]
    years = [int(r["year"]) for r in series]

    if as_json:
        series_rows: list[dict[str, Any]] = []
        for r in series:
            series_rows.append(
                {
                    "year": int(r["year"]),
                    "burden_ratio_p50": float(r["burden_ratio_p50"]),
                    "burden_ratio_p50_se": _maybe_float(r.get("burden_ratio_p50_se")),
                    "weighted_n": r.get("weighted_n"),
                    "sample_n": r.get("sample_n"),
                    "suppressed": r.get("suppressed"),
                }
            )
        if series:
            latest = series[-1]
            y_latest = int(latest["year"])
            r_latest = float(latest["burden_ratio_p50"])
            se = latest.get("burden_ratio_p50_se")
            spark = ascii_sparkline(ratios)
            yr_lo, yr_hi = years[0], years[-1]
            latest_payload: dict[str, Any] | None = {
                "year": y_latest,
                "burden_ratio_p50": r_latest,
                "burden_ratio_p50_se": _maybe_float(se),
            }
            year_range: list[int] | None = [yr_lo, yr_hi]
        else:
            spark = ""
            latest_payload = None
            year_range = None

        _json_dump(
            {
                "metric": "pums_burden_county_series",
                "segment_dim": "overall",
                "county_fips": county_fips,
                "county_name": county_name,
                "product": product,
                "tenure": tenure,
                "year_range": year_range,
                "n_years": len(years),
                "sparkline_ascii": spark,
                "latest": latest_payload,
                "series": series_rows,
                "provenance": _provenance_to_json(prov),
            }
        )
        return

    if not series:
        raise click.ClickException(
            f"No PUMS burden series for {county_name} ({county_fips}) "
            f"product={product} tenure={tenure}. Is derived data materialized?",
        )

    latest = series[-1]
    y_latest = int(latest["year"])
    r_latest = float(latest["burden_ratio_p50"])
    se = latest.get("burden_ratio_p50_se")
    se_s = f"{float(se):.4f}" if se is not None else "—"

    spark = ascii_sparkline(ratios)
    yr_lo, yr_hi = years[0], years[-1]

    console = Console(highlight=False)
    table = Table(title=None, show_header=False, box=None, padding=(0, 2))
    table.add_column("k", style="dim", justify="right")
    table.add_column("v")
    table.add_row("county", f"{county_name} ({county_fips})")
    table.add_row("product / tenure", f"{product} / {tenure}")
    table.add_row("years", f"{yr_lo}-{yr_hi} ({len(years)} vintages)")
    table.add_row("latest year", str(y_latest))
    table.add_row("burden ratio p50", f"{r_latest:.4f}  (SE {se_s})")
    table.add_row("sparkline", spark)

    console.print(table)
    console.print(f"[dim]{format_provenance_footer(prov)}[/dim]")
    console.print(
        "[dim]Ratio of medians: median monthly housing cost / median household income. "
        "Sparkline is normalized min-max within this county series.[/dim]",
    )


# ============================================================================
# `nj-terminal hpi <county>` -- FHFA HPI county series, base-year normalized
# ============================================================================
#
# Mirror of GET /hpi/{county_fips}/series. The CLI defaults to base_year=2000
# (the same default the API uses), so `nj BERGEN HPI` renders a series where
# 2000 = 100.000 -- the chart caption the dashboard already uses.
#
# JSON output mirrors cmd_burden's shape: metric / county / latest /
# sparkline / series / provenance. Empty list responses are NOT errors
# in --json mode: 200 OK with rows=[] (matches the API on a county with
# no data at this base year). Without --json, an empty series raises
# a ClickException so analysts get an explicit non-zero exit code.
# ============================================================================


@main.command("hpi")
@click.argument("county_pos", metavar="[COUNTY]", required=False)
@click.option(
    "--county",
    "-c",
    help="County name, 34xxx FIPS, or any 5-digit county_fips (overrides positional).",
)
@click.option(
    "--base-year",
    "-b",
    type=click.IntRange(min=1975, max=2030),
    default=HPI_DEFAULT_BASE_YEAR,
    show_default=True,
    help="Year for which the indexed value should equal 100.000.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON (series rows + sparkline). Parallel to GET /hpi/{fips}/series.",
)
def cmd_hpi(
    county_pos: str | None,
    county: str | None,
    base_year: int,
    as_json: bool,
) -> None:
    """FHFA House Price Index annual series, re-indexed to ``base_year``.

    Returns the county's HPI from the earliest available year through
    the most recent vintage, expressed as a level relative to a chosen
    anchor year. The published all-transactions index controls for
    compositional change in the housing stock -- so this is the right
    series for "how much have prices appreciated", as opposed to ACS
    B25077 which answers "what does a typical owned home sell for".
    """
    dsn = _require_dsn()

    target = (county or county_pos or "").strip()
    if not target:
        raise click.UsageError("Provide COUNTY (e.g. bergen) or --county.")

    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        if _PUMA_5_RE.fullmatch(target):
            county_fips = target
            cref = list_counties(conn, state_code="NJ")
            county_name = next(
                (r["name"] for r in cref if r["county_fips"] == county_fips),
                county_fips,
            )
        else:
            resolved = resolve_county_fips(conn, target)
            if resolved is None:
                raise click.UsageError("County required.")
            county_fips, county_name = resolved

        raw_rows = list_hpi_county_series(
            conn, county_fips=county_fips, base_year=base_year,
        )
        prov = _fetch_asset_detail(conn, "raw.fhfa_hpi_county")

    rows = [HpiCountyRow.model_validate(r) for r in raw_rows]
    indexed_vals = [float(r.hpi_indexed) for r in rows]
    years = [r.year for r in rows]

    if as_json:
        if rows:
            spark = ascii_sparkline(indexed_vals)
            latest = rows[-1]
            latest_payload: dict[str, Any] | None = {
                "year": latest.year,
                "hpi_indexed": float(latest.hpi_indexed),
                "hpi_raw": float(latest.hpi_raw),
                "annual_change": (
                    float(latest.annual_change)
                    if latest.annual_change is not None
                    else None
                ),
            }
            year_range: list[int] | None = [years[0], years[-1]]
        else:
            spark = ""
            latest_payload = None
            year_range = None

        _json_dump(
            {
                "metric": "fhfa_hpi_indexed",
                "county_fips": county_fips,
                "county_name": county_name,
                "base_year": base_year,
                "year_range": year_range,
                "n_years": len(years),
                "sparkline_ascii": spark,
                "latest": latest_payload,
                "series": [r.model_dump(mode="json") for r in rows],
                "provenance": _provenance_to_json(prov),
            },
        )
        return

    if not rows:
        raise click.ClickException(
            f"No FHFA HPI series for {county_name} ({county_fips}) "
            f"at base_year={base_year}. Is raw.fhfa_hpi_county loaded?",
        )

    latest = rows[-1]
    spark = ascii_sparkline(indexed_vals)
    yr_lo, yr_hi = years[0], years[-1]

    console = Console(highlight=False)
    table = Table(title=None, show_header=False, box=None, padding=(0, 2))
    table.add_column("k", style="dim", justify="right")
    table.add_column("v")
    table.add_row("county", f"{county_name} ({county_fips})")
    table.add_row("base year", f"{base_year} (=100.000)")
    table.add_row("years", f"{yr_lo}-{yr_hi} ({len(years)} vintages)")
    table.add_row("latest year", str(latest.year))
    table.add_row(
        "HPI (indexed)",
        f"{float(latest.hpi_indexed):.3f}  (raw {float(latest.hpi_raw):.3f})",
    )
    if latest.annual_change is not None:
        table.add_row("annual change", f"{float(latest.annual_change):.4f}")
    table.add_row("sparkline", spark)

    console.print(table)
    console.print(f"[dim]{format_provenance_footer(prov)}[/dim]")
    console.print(
        "[dim]FHFA all-transactions repeat-sales index. "
        "Sparkline is normalized min-max within this county series.[/dim]",
    )


# ============================================================================
# `nj-terminal income <county>` -- ACS B19013, CPI-deflated to base_year $$
# ============================================================================
#
# Mirror of GET /income/{county_fips}/series. The base year defaults to
# `min(max(dollar_year), max(cpi_year))` so the series shows up in
# "today's dollars" without the analyst picking a year. When that
# default cannot be computed (one of the two source tables is empty),
# the CLI errors out explicitly instead of falling back to a wrong year.
# ============================================================================


@main.command("income")
@click.argument("county_pos", metavar="[COUNTY]", required=False)
@click.option(
    "--county",
    "-c",
    help="County name, 34xxx FIPS, or any 5-digit county_fips (overrides positional).",
)
@click.option(
    "--product",
    "-p",
    type=click.Choice(["acs5", "acs1"]),
    default="acs5",
    show_default=True,
    help="ACS product backing the series. acs5 has lower MOE; acs1 is fresher.",
)
@click.option(
    "--base-year",
    "-b",
    type=click.IntRange(min=2005, max=2030),
    default=None,
    help=(
        "Deflate to this year's dollars. Default: most recent year for "
        "which both ACS dollar_year and CPI are loaded."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON (series rows + sparkline). Parallel to GET /income/{fips}/series.",
)
def cmd_income(
    county_pos: str | None,
    county: str | None,
    product: str,
    base_year: int | None,
    as_json: bool,
) -> None:
    """ACS B19013 median household income, CPI-deflated to constant $$.

    The series is deflated to ``--base-year`` dollars (default: today).
    Suppressed estimates are excluded; the row schema therefore never
    has a NULL ``estimate_real``. For long horizons or high inflation
    the choice of base year matters; the CLI echoes the chosen base
    year explicitly so consumers can pin it.
    """
    dsn = _require_dsn()

    target = (county or county_pos or "").strip()
    if not target:
        raise click.UsageError("Provide COUNTY (e.g. bergen) or --county.")

    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        if _PUMA_5_RE.fullmatch(target):
            county_fips = target
            cref = list_counties(conn, state_code="NJ")
            county_name = next(
                (r["name"] for r in cref if r["county_fips"] == county_fips),
                county_fips,
            )
        else:
            resolved = resolve_county_fips(conn, target)
            if resolved is None:
                raise click.UsageError("County required.")
            county_fips, county_name = resolved

        eff_base = base_year
        if eff_base is None:
            eff_base = resolve_default_income_base_year(conn)
            if eff_base is None:
                raise click.ClickException(
                    "No ACS income or CPI data is loaded; cannot compute "
                    "a default deflation base year. Pass --base-year explicitly.",
                )

        raw_rows = list_income_county_series(
            conn,
            county_fips=county_fips,
            base_year=eff_base,
            product=product,
        )
        prov = _fetch_asset_detail(conn, "raw.acs_median_household_income")

    rows = [IncomeCountyRow.model_validate(r) for r in raw_rows]
    real_vals = [float(r.estimate_real) for r in rows]
    years = [r.year for r in rows]

    if as_json:
        if rows:
            spark = ascii_sparkline(real_vals)
            latest = rows[-1]
            latest_payload: dict[str, Any] | None = {
                "year": latest.year,
                "estimate_real": float(latest.estimate_real),
                "estimate_nominal": float(latest.estimate_nominal),
                "deflator": float(latest.deflator),
            }
            year_range: list[int] | None = [years[0], years[-1]]
        else:
            spark = ""
            latest_payload = None
            year_range = None

        _json_dump(
            {
                "metric": "acs_mhi_real",
                "county_fips": county_fips,
                "county_name": county_name,
                "product": product,
                "base_year": eff_base,
                "year_range": year_range,
                "n_years": len(years),
                "sparkline_ascii": spark,
                "latest": latest_payload,
                "series": [r.model_dump(mode="json") for r in rows],
                "provenance": _provenance_to_json(prov),
            },
        )
        return

    if not rows:
        raise click.ClickException(
            f"No ACS income series for {county_name} ({county_fips}) "
            f"product={product} base_year={eff_base}. "
            "Is raw.acs_median_household_income loaded?",
        )

    latest = rows[-1]
    spark = ascii_sparkline(real_vals)
    yr_lo, yr_hi = years[0], years[-1]

    console = Console(highlight=False)
    table = Table(title=None, show_header=False, box=None, padding=(0, 2))
    table.add_column("k", style="dim", justify="right")
    table.add_column("v")
    table.add_row("county", f"{county_name} ({county_fips})")
    table.add_row("product", product)
    table.add_row("base year", f"{eff_base} dollars")
    table.add_row("years", f"{yr_lo}-{yr_hi} ({len(years)} vintages)")
    table.add_row("latest year", str(latest.year))
    table.add_row(
        "income (real)",
        f"${float(latest.estimate_real):,.0f}  "
        f"(nominal ${float(latest.estimate_nominal):,.0f})",
    )
    table.add_row("deflator", f"{float(latest.deflator):.4f}")
    table.add_row("sparkline", spark)

    console.print(table)
    console.print(f"[dim]{format_provenance_footer(prov)}[/dim]")
    console.print(
        "[dim]ACS B19013 median household income, CPI-U deflated. "
        "Sparkline is normalized min-max within this county series.[/dim]",
    )


@main.command("pums-series")
@click.option(
    "--county",
    "-c",
    default=None,
    help="County name, 34xxx FIPS, or any 5-digit county_fips; omit for all counties.",
)
@click.option(
    "--tenure",
    "-t",
    type=click.Choice(["renter", "owner_w_mtg", "owner_no_mtg"]),
    default=None,
    help="Restrict to one tenure; omit for all three.",
)
@click.option(
    "--product",
    "-p",
    type=click.Choice(["acs5", "acs1"]),
    default=DEFAULT_PUMS_PRODUCT,
    show_default=True,
)
@click.option(
    "--include-suppressed",
    "include_suppressed",
    is_flag=True,
    help="Include suppressed cells (ratio may be null).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON array (same rows as GET /pums-burden-county-series).",
)
def cmd_pums_series(
    county: str | None,
    tenure: str | None,
    product: str,
    include_suppressed: bool,
    as_json: bool,
) -> None:
    """Multi-year PUMS overall-segment series (batch / chart source).

    Parallel to ``GET /pums-burden-county-series``. For one county + sparkline
    summary use ``burden``; this command returns the full filtered row list.
    """
    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        county_fips: str | None = None
        if county and county.strip():
            raw_co = county.strip()
            if _PUMA_5_RE.fullmatch(raw_co):
                county_fips = raw_co
            else:
                resolved = resolve_county_fips(conn, raw_co)
                if resolved is None:
                    raise click.UsageError("County could not be resolved.")
                county_fips, _cname = resolved

        raw_rows = list_pums_burden_county_series(
            conn,
            tenure=tenure,
            county_fips=county_fips,
            product=product,
            include_suppressed=include_suppressed,
        )

    rows = [PumsBurdenCountySeriesRow.model_validate(r) for r in raw_rows]
    if as_json:
        _json_dump([r.model_dump(mode="json") for r in rows])
        return

    if not rows:
        raise click.ClickException(
            "No PUMS county series rows for this filter. "
            "Is derived.pums_burden_county_segmented materialized?",
        )

    console = Console(highlight=False)
    title = f"PUMS county series ({product}) · {len(rows)} rows"
    table = Table(title=title, show_lines=False)
    table.add_column("year", justify="right")
    table.add_column("county")
    table.add_column("fips", style="cyan", no_wrap=True)
    table.add_column("tenure")
    table.add_column("ratio", justify="right")
    table.add_column("SE", justify="right")
    table.add_column("sup", justify="center")

    for r in rows:
        table.add_row(
            str(r.year),
            r.county_name,
            r.county_fips,
            r.tenure_class,
            _fmt_ratio_val(r.burden_ratio_p50),
            _fmt_ratio_val(r.burden_ratio_p50_se),
            "y" if r.suppressed else "",
        )

    console.print(table)
    console.print("[dim]Parallel to GET /pums-burden-county-series.[/dim]")


@main.command("pums-burden")
@click.argument("puma", required=False)
@click.option(
    "--dim",
    type=click.Choice(
        ["race", "hispanic", "citizenship", "age_band", "overall"],
    ),
    default=None,
    help="Segment dimension filter.",
)
@click.option(
    "--tenure",
    "-t",
    type=click.Choice(["renter", "owner_w_mtg", "owner_no_mtg"]),
    default=None,
)
@click.option(
    "--product",
    "-p",
    type=click.Choice(["acs5", "acs1"]),
    default=DEFAULT_PUMS_PRODUCT,
    show_default=True,
)
@click.option(
    "--include-suppressed",
    "include_suppressed",
    is_flag=True,
    help="Include suppressed cells (NULL ratios).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON (GET /pums-burden or /pums-burden/{puma}).",
)
def cmd_pums_burden(
    puma: str | None,
    dim: str | None,
    tenure: str | None,
    product: str,
    include_suppressed: bool,
    as_json: bool,
) -> None:
    """PUMS segmented burden, latest vintage: all NJ PUMAs or one 5-digit PUMA.

    No *puma*: ``GET /pums-burden``. With *puma*: ``GET /pums-burden/{puma}``.
    """
    puma_s = (puma or "").strip()
    if puma_s and not _PUMA_5_RE.fullmatch(puma_s):
        raise click.BadParameter(
            "expected 5-digit PUMA (e.g. 00300)",
            param_hint="PUMA",
        )

    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        if puma_s:
            raw_rows = list_pums_burden_for_puma(
                conn,
                puma=puma_s,
                dim=dim,
                tenure=tenure,
                include_suppressed=include_suppressed,
                product=product,
            )
        else:
            raw_rows = list_pums_burden_latest(
                conn,
                dim=dim,
                tenure=tenure,
                include_suppressed=include_suppressed,
                product=product,
            )

    rows = [PumsBurdenRow.model_validate(r) for r in raw_rows]
    if not rows and puma_s:
        raise click.ClickException(
            f"No PUMS burden data for PUMA {puma_s!r}.",
        )

    if as_json:
        _json_dump([r.model_dump(mode="json") for r in rows])
        return

    scope = f"PUMA {puma_s}" if puma_s else "all NJ PUMAs"
    title = f"PUMS burden ({product}) · {scope} · n={len(rows)}"
    console = Console(highlight=False)
    table = Table(title=title, show_lines=False)
    table.add_column("puma", style="cyan", no_wrap=True)
    table.add_column("tenure")
    table.add_column("dim")
    table.add_column("value")
    table.add_column("ratio", justify="right")
    table.add_column("sup", justify="center")

    for r in rows:
        table.add_row(
            r.puma,
            r.tenure_class,
            r.segment_dim,
            r.segment_value,
            _fmt_ratio_val(r.burden_ratio_p50),
            "y" if r.suppressed else "",
        )

    console.print(table)
    console.print("[dim]Parallel to GET /pums-burden or /pums-burden/{puma}.[/dim]")


@main.command("pums-burden-county")
@click.argument("county", required=False)
@click.option(
    "--dim",
    type=click.Choice(
        ["race", "hispanic", "citizenship", "age_band", "overall"],
    ),
    default=None,
    help="Segment dimension filter.",
)
@click.option(
    "--tenure",
    "-t",
    type=click.Choice(["renter", "owner_w_mtg", "owner_no_mtg"]),
    default=None,
)
@click.option(
    "--product",
    "-p",
    type=click.Choice(["acs5", "acs1"]),
    default=DEFAULT_PUMS_PRODUCT,
    show_default=True,
)
@click.option(
    "--include-suppressed",
    "include_suppressed",
    is_flag=True,
    help="Include suppressed cells (NULL ratios).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON (GET /pums-burden-county or /pums-burden-county/{fips}).",
)
def cmd_pums_burden_county(
    county: str | None,
    dim: str | None,
    tenure: str | None,
    product: str,
    include_suppressed: bool,
    as_json: bool,
) -> None:
    """PUMS burden at county grain (latest vintage): all counties or one county.

    No *county*: ``GET /pums-burden-county``. With *county* (name or 34xxx FIPS):
    ``GET /pums-burden-county/{county_fips}``.
    """
    target = (county or "").strip()
    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        county_fips: str | None = None
        county_label = "all NJ counties"
        if target:
            if _PUMA_5_RE.fullmatch(target):
                county_fips = target
                cref = list_counties(conn, state_code="NJ")
                cname = next(
                    (r["name"] for r in cref if r["county_fips"] == county_fips),
                    county_fips,
                )
                county_label = f"{cname} ({county_fips})"
            else:
                resolved = resolve_county_fips(conn, target)
                if resolved is None:
                    raise click.UsageError("County could not be resolved.")
                county_fips, cname = resolved
                county_label = f"{cname} ({county_fips})"

        if county_fips is not None:
            raw_rows = list_pums_burden_county_for_county(
                conn,
                county_fips=county_fips,
                dim=dim,
                tenure=tenure,
                include_suppressed=include_suppressed,
                product=product,
            )
        else:
            raw_rows = list_pums_burden_county_latest(
                conn,
                dim=dim,
                tenure=tenure,
                include_suppressed=include_suppressed,
                product=product,
            )

    rows = [PumsBurdenCountyRow.model_validate(r) for r in raw_rows]
    if not rows and county_fips is not None:
        raise click.ClickException(
            f"No PUMS burden data for county_fips {county_fips!r}.",
        )

    if as_json:
        _json_dump([r.model_dump(mode="json") for r in rows])
        return

    title = f"PUMS county burden ({product}) · {county_label} · n={len(rows)}"
    console = Console(highlight=False)
    table = Table(title=title, show_lines=False)
    table.add_column("county")
    table.add_column("fips", style="cyan", no_wrap=True)
    table.add_column("tenure")
    table.add_column("dim")
    table.add_column("value")
    table.add_column("ratio", justify="right")
    table.add_column("#pu", justify="right")
    table.add_column("sup", justify="center")

    for r in rows:
        table.add_row(
            r.county_name,
            r.county_fips,
            r.tenure_class,
            r.segment_dim,
            r.segment_value,
            _fmt_ratio_val(r.burden_ratio_p50),
            str(r.n_pumas_contributing),
            "y" if r.suppressed else "",
        )

    console.print(table)
    console.print(
        "[dim]Parallel to GET /pums-burden-county or /pums-burden-county/{fips}.[/dim]",
    )


@main.command("pums-burden-county-series")
@click.argument("county", required=False)
@click.option(
    "--tenure",
    "-t",
    type=click.Choice(["renter", "owner_w_mtg", "owner_no_mtg"]),
    default=None,
)
@click.option(
    "--product",
    "-p",
    type=click.Choice(["acs5", "acs1"]),
    default=DEFAULT_PUMS_PRODUCT,
    show_default=True,
)
@click.option(
    "--include-suppressed",
    "include_suppressed",
    is_flag=True,
    help="Include cells with weighted_n < 1000 (NULL ratios).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON array (GET /pums-burden-county-series).",
)
def cmd_pums_burden_county_series(
    county: str | None,
    tenure: str | None,
    product: str,
    include_suppressed: bool,
    as_json: bool,
) -> None:
    """Multi-year county burden ratios (``segment_dim='overall'`` only).

    Optional *county*: NJ name or 5-digit FIPS; omit for all counties.
    Parallel to ``GET /pums-burden-county-series``.
    """
    target = (county or "").strip()
    dsn = _require_dsn()

    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        county_fips: str | None = None
        if target:
            if _PUMA_5_RE.fullmatch(target):
                county_fips = target
            else:
                resolved = resolve_county_fips(conn, target)
                if resolved is None:
                    raise click.UsageError("County could not be resolved.")
                county_fips = resolved[0]

        raw_rows = list_pums_burden_county_series(
            conn,
            tenure=tenure,
            county_fips=county_fips,
            product=product,
            include_suppressed=include_suppressed,
        )

    rows = [PumsBurdenCountySeriesRow.model_validate(r) for r in raw_rows]

    if as_json:
        _json_dump([r.model_dump(mode="json") for r in rows])
        return

    title = f"PUMS county series ({product}) · n={len(rows)}"
    console = Console(highlight=False)
    table = Table(title=title, show_lines=False)
    table.add_column("year", justify="right")
    table.add_column("county")
    table.add_column("fips", style="cyan", no_wrap=True)
    table.add_column("tenure")
    table.add_column("ratio", justify="right")
    table.add_column("SE", justify="right")
    table.add_column("#w", justify="right")
    table.add_column("sup", justify="center")

    for r in rows:
        table.add_row(
            str(r.year),
            _trunc_cell(r.county_name, max_len=14),
            r.county_fips,
            r.tenure_class,
            _fmt_ratio_val(r.burden_ratio_p50),
            _fmt_ratio_val(r.burden_ratio_p50_se),
            str(r.weighted_n),
            "y" if r.suppressed else "",
        )

    console.print(table)
    console.print("[dim]Parallel to GET /pums-burden-county-series.[/dim]")


@main.command("acs-burden")
@click.argument("county_pos", metavar="[COUNTY]", required=False)
@click.option(
    "--county",
    "-c",
    help="County name, 34xxx FIPS, or any 5-digit county_fips (overrides positional).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON (envelope + sparkline metadata).",
)
@click.option(
    "--raw-json",
    "raw_json",
    is_flag=True,
    help="Print JSON array only (same objects as GET /burden/{county_fips}).",
)
def cmd_acs_burden(
    county_pos: str | None,
    county: str | None,
    as_json: bool,
    raw_json: bool,
) -> None:
    """ACS tabular burden ratios (``derived.housing_burden_ratio``, product acs5).

    Distinct from ``burden``: Census-published margins tables, not PUMS microdata.
    Shows tenure-specific ratios plus blended; sparkline follows blended series.
    """
    if as_json and raw_json:
        raise click.UsageError("Use either --json or --raw-json, not both.")

    target = (county or county_pos or "").strip()
    if not target:
        raise click.UsageError("Provide COUNTY (e.g. bergen) or --county.")

    if raw_json and _PUMA_5_RE.fullmatch(target) and not _nj_fips(target):
        raise click.BadParameter(
            "NJ county FIPS must be 5 digits prefixed with 34 (matches GET /burden/{county_fips}).",
            param_hint="COUNTY",
        )

    dsn = _require_dsn()

    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        if _PUMA_5_RE.fullmatch(target):
            county_fips = target
            cref = list_counties(conn, state_code="NJ")
            county_name = next(
                (r["name"] for r in cref if r["county_fips"] == county_fips),
                county_fips,
            )
        else:
            resolved = resolve_county_fips(conn, target)
            if resolved is None:
                raise click.UsageError("County required.")
            county_fips, county_name = resolved
        rows = list_burden_for_county(conn, county_fips=county_fips)

    if raw_json:
        if not _nj_fips(county_fips):
            raise click.BadParameter(
                "NJ county FIPS must be 5 digits prefixed with 34 "
                "(matches GET /burden/{county_fips}).",
                param_hint="COUNTY",
            )
        if not rows:
            raise click.ClickException(
                f"No housing burden data for county {county_fips!r}",
            )
        refs = [BurdenRow.model_validate(r) for r in rows]
        _json_dump([r.model_dump(mode="json") for r in refs])
        return

    if not rows:
        raise click.ClickException(
            f"No ACS housing burden rows for {county_name} ({county_fips}). "
            "Is derived.housing_burden_ratio materialized?",
        )

    rows.sort(key=lambda r: int(r["year"]))
    blended_series: list[float] = []
    for r in rows:
        b = r.get("blended_burden_ratio")
        if isinstance(b, (int, float, Decimal)):
            blended_series.append(float(b))
    if not blended_series:
        for r in rows:
            rr = r.get("renter_burden_ratio")
            if isinstance(rr, (int, float, Decimal)):
                blended_series.append(float(rr))
    latest = rows[-1]
    y_latest = int(latest["year"])
    spark = ascii_sparkline(blended_series)
    yr_lo, yr_hi = int(rows[0]["year"]), int(rows[-1]["year"])

    if as_json:
        _json_dump(
            {
                "metric": "acs_housing_burden_ratio",
                "product": "acs5",
                "county_fips": county_fips,
                "county_name": county_name,
                "year_range": [yr_lo, yr_hi],
                "n_years": len(rows),
                "sparkline_ascii": spark if spark else None,
                "series": rows,
            }
        )
        return

    console = Console(highlight=False)
    table = Table(title=None, show_header=False, box=None, padding=(0, 2))
    table.add_column("k", style="dim", justify="right")
    table.add_column("v")
    table.add_row("county", f"{county_name} ({county_fips})")
    table.add_row("source", "derived.housing_burden_ratio · product acs5")
    table.add_row("years", f"{yr_lo}-{yr_hi} ({len(rows)} vintages)")
    table.add_row("latest year", str(y_latest))
    table.add_row("renter ratio", _fmt_ratio_val(latest.get("renter_burden_ratio")))
    table.add_row("owner w/ mtg", _fmt_ratio_val(latest.get("owner_burden_w_mtg_ratio")))
    table.add_row("owner no mtg", _fmt_ratio_val(latest.get("owner_burden_no_mtg_ratio")))
    table.add_row("blended", _fmt_ratio_val(latest.get("blended_burden_ratio")))
    table.add_row("sparkline (blended)", spark if spark else "—")

    console.print(table)
    console.print(
        "[dim]HUD-style tenure-weighted blend where present; see /burden API for schema.[/dim]",
    )


@main.command("burden-latest")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON array (GET /burden; may be empty).",
)
def cmd_burden_latest(as_json: bool) -> None:
    """ACS 5-yr burden snapshot: latest vintage, all NJ counties (tabular).

    Parallel to ``GET /burden``: ``derived.housing_burden_ratio``, one row per
    county at the maximum available year (``--json`` matches an empty HTTP body
    when no rows exist).
    """
    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        raw_rows = list_burden_latest_year_nj(conn)

    rows = [BurdenRow.model_validate(r) for r in raw_rows]
    if as_json:
        _json_dump([r.model_dump(mode="json") for r in rows])
        return

    if not rows:
        raise click.ClickException(
            "No housing burden rows for latest NJ year. "
            "Is derived.housing_burden_ratio materialized?",
        )

    y = rows[0].year
    console = Console(highlight=False)
    table = Table(
        title=f"ACS housing burden (latest year {y}, all NJ counties)",
        show_lines=False,
    )
    table.add_column("county")
    table.add_column("fips", style="cyan", no_wrap=True)
    table.add_column("blended", justify="right")
    table.add_column("renter", justify="right")
    table.add_column("owner w/mtg", justify="right")

    for r in rows:
        table.add_row(
            r.county_name,
            r.county_fips,
            _fmt_ratio_val(r.blended_burden_ratio),
            _fmt_ratio_val(r.renter_burden_ratio),
            _fmt_ratio_val(r.owner_burden_w_mtg_ratio),
        )

    console.print(table)
    console.print("[dim]Parallel to GET /burden · product acs5.[/dim]")


@main.command("counties")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON array (same objects as GET /counties).",
)
def cmd_counties(as_json: bool) -> None:
    """NJ counties from ``ref.county`` (name order; UI dropdown source)."""
    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        rows = list_counties(conn, state_code="NJ")
    refs = [CountyRef.model_validate(r) for r in rows]

    if as_json:
        _json_dump([c.model_dump(mode="json") for c in refs])
        return

    console = Console(highlight=False)
    table = Table(title="NJ counties", show_lines=False)
    table.add_column("county_fips", style="cyan", no_wrap=True)
    table.add_column("name")
    for c in refs:
        table.add_row(c.county_fips, c.name)
    console.print(table)
    console.print("[dim]Parallel to GET /counties.[/dim]")


def _calendar_sort_ts(
    upcoming: list[dt.datetime],
    next_at: dt.datetime | None,
) -> float:
    if upcoming:
        return upcoming[0].timestamp()
    if next_at is not None:
        return next_at.timestamp()
    return float("inf")


@main.command("datasets")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON array (same objects as GET /assets).",
)
def cmd_datasets(as_json: bool) -> None:
    """Freshness roll-up for every dataset (same join as ``GET /assets``)."""
    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        raw_rows = list_assets(conn)

    summaries: list[AssetSummary] = []
    for r in raw_rows:
        state, age_h = compute_freshness_state(
            last_materialized_at=r.get("last_materialized_at"),
            expected_lag_hours=r.get("expected_lag_hours"),
        )
        summaries.append(
            AssetSummary(
                dataset_id=r["dataset_id"],
                cadence=r.get("cadence"),
                schedule_label=r.get("schedule_label"),
                expected_lag_hours=r.get("expected_lag_hours"),
                last_materialized_at=r.get("last_materialized_at"),
                last_rows_upserted=r.get("last_rows_upserted"),
                age_hours=age_h,
                freshness_state=state,
                n_warn_30d=r.get("n_warn_30d", 0),
                n_error_30d=r.get("n_error_30d", 0),
            ),
        )

    if as_json:
        _json_dump([s.model_dump(mode="json") for s in summaries])
        return

    console = Console(highlight=False)
    table = Table(title="Datasets", show_lines=False)
    table.add_column("dataset_id", style="cyan", no_wrap=True)
    table.add_column("cadence")
    table.add_column("state", justify="center")
    table.add_column("age_h")
    table.add_column("warn30", justify="right")
    table.add_column("err30", justify="right")

    for row in summaries:
        age_h = row.age_hours
        age_s = f"{float(age_h):.1f}" if age_h is not None else "—"
        table.add_row(
            row.dataset_id,
            row.cadence or "—",
            row.freshness_state,
            age_s,
            str(row.n_warn_30d),
            str(row.n_error_30d),
        )

    console.print(table)
    console.print("[dim]Matches GET /assets; uses ref.release_calendar + governance views.[/dim]")


@main.command("asset")
@click.argument("dataset_id")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON (same shape as GET /asset/{dataset_id}).",
)
def cmd_asset(dataset_id: str, as_json: bool) -> None:
    """Single dataset pane: cadence, freshness, last materialization (DES-style).

    ``dataset_id`` is ``schema.table`` (e.g. ``raw.fred_observation``). Matches
    ``GET /asset/{dataset_id}`` and ``GET /assets/{schema}/{table}``.
    """
    raw_id = dataset_id.strip()
    if not _DATASET_ID_RE.fullmatch(raw_id):
        raise click.BadParameter(
            "must look like schema.table (lowercase), e.g. raw.fred_observation",
            param_hint="DATASET_ID",
        )

    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        detail = _fetch_asset_detail(conn, raw_id)

    if detail is None:
        raise click.ClickException(f"Unknown dataset {raw_id!r}.")

    if as_json:
        _json_dump(detail.model_dump(mode="json"))
        return

    console = Console(highlight=False)
    table = Table(title=None, show_header=False, box=None, padding=(0, 2))
    table.add_column("k", style="dim", justify="right")
    table.add_column("v")
    age_s = (
        f"{float(detail.age_hours):.2f}" if detail.age_hours is not None else "—"
    )
    details = detail.last_materialization_details
    if details is None:
        det_s = "—"
    else:
        det_compact = json.dumps(details, default=str)
        det_s = det_compact if len(det_compact) <= 140 else f"{det_compact[:137]}..."

    table.add_row("dataset_id", detail.dataset_id)
    table.add_row("cadence", detail.cadence or "—")
    table.add_row("schedule_label", detail.schedule_label or "—")
    table.add_row(
        "expected_lag_h",
        str(detail.expected_lag_hours) if detail.expected_lag_hours is not None else "—",
    )
    table.add_row("freshness_state", detail.freshness_state)
    table.add_row("age_hours", age_s)
    table.add_row("last_materialized_at", str(detail.last_materialized_at or "—"))
    table.add_row("last_rows_upserted", str(detail.last_rows_upserted or "—"))
    table.add_row("n_warn_30d / n_error_30d", f"{detail.n_warn_30d} / {detail.n_error_30d}")
    table.add_row("last_materialization_details", det_s)
    console.print(table)
    console.print("[dim]Parallel to GET /asset/{dataset_id}.[/dim]")


@main.command("releases")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON array (same objects as GET /releases).",
)
def cmd_releases(as_json: bool) -> None:
    """Publication calendar seed rows from ``ref.release_calendar``.

    This is the static cadence/lag metadata only. For upcoming instants,
    materialization age, and overdue flags use ``calendar`` (``GET /release-calendar``).
    """
    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        rows = list_release_calendar(conn)
    refs = [ReleaseCalendarRow.model_validate(r) for r in rows]

    if as_json:
        _json_dump([r.model_dump(mode="json") for r in refs])
        return

    console = Console(highlight=False)
    table = Table(title="Release calendar (ref.release_calendar)", show_lines=False)
    table.add_column("source_id", style="cyan", no_wrap=True)
    table.add_column("cadence")
    table.add_column("tz")
    table.add_column("lag_h", justify="right")
    table.add_column("schedule")
    table.add_column("notes")

    for r in refs:
        notes = r.notes or "—"
        if len(notes) > 48:
            notes = f"{notes[:45]}..."
        sched = r.schedule_label
        if len(sched) > 36:
            sched = f"{sched[:33]}..."
        table.add_row(
            r.source_id,
            r.cadence,
            r.timezone,
            str(r.expected_lag_hours),
            sched,
            notes,
        )

    console.print(table)
    console.print("[dim]Parallel to GET /releases; use ``calendar`` for horizon + freshness.[/dim]")


@main.command("calendar")
@click.option(
    "--days",
    type=click.IntRange(1, 366),
    default=14,
    show_default=True,
    help="Forward window for upcoming release instants.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON (same envelope as GET /release-calendar).",
)
def cmd_calendar(days: int, as_json: bool) -> None:
    """Release calendar + materialization age (``GET /release-calendar`` logic)."""
    dsn = _require_dsn()
    as_of = dt.datetime.now(dt.UTC)

    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        detailed = list_release_calendar_detailed(conn)

    # Same row construction as ``serving.routes.releases.get_release_calendar``.
    sources_rows: list[ReleaseCalendarHorizonRow] = []
    for r in detailed:
        upcoming, next_at, ok = compute_release_calendar_row(
            r,
            now_utc=as_of,
            horizon_days=days,
        )
        state, age_h = compute_freshness_state(
            last_materialized_at=r.get("last_materialized_at"),
            expected_lag_hours=r.get("expected_lag_hours"),
            now=as_of,
        )
        overdue = state == "stale"
        sources_rows.append(
            ReleaseCalendarHorizonRow(
                source_id=r["source_id"],
                cadence=r["cadence"],
                schedule_label=r["schedule_label"],
                timezone=r["timezone"],
                expected_lag_hours=r["expected_lag_hours"],
                notes=r.get("notes"),
                last_materialized_at=r.get("last_materialized_at"),
                age_hours=age_h,
                freshness_state=state,
                overdue=overdue,
                upcoming_releases=upcoming,
                next_expected_at=next_at,
                schedule_computed=ok,
            ),
        )

    if as_json:
        panel = ReleaseCalendarPanel(
            as_of=as_of,
            horizon_days=days,
            sources=sources_rows,
        )
        _json_dump(panel.model_dump(mode="json"))
        return

    display_order = sorted(
        range(len(sources_rows)),
        key=lambda i: _calendar_sort_ts(
            sources_rows[i].upcoming_releases,
            sources_rows[i].next_expected_at,
        ),
    )

    console = Console(highlight=False)
    table = Table(title=f"Release calendar ({days}d forward)", show_lines=False)
    table.add_column("source_id", style="cyan", no_wrap=True)
    table.add_column("next (ET)")
    table.add_column("#win", justify="right")
    table.add_column("late", justify="center")

    for i in display_order:
        m = sources_rows[i]
        next_s = _fmt_et(m.next_expected_at) if m.schedule_computed and m.next_expected_at else "—"
        table.add_row(
            m.source_id,
            next_s,
            str(len(m.upcoming_releases)),
            "yes" if m.overdue else "",
        )

    console.print(table)
    console.print(
        f"[dim]as_of {_fmt_et(as_of)} · rows without a parsed schedule show — in next column[/dim]",
    )


@main.command("health")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON for scripts (jq-friendly).",
)
def cmd_health(as_json: bool) -> None:
    """Liveness probe (``GET /health`` SQL contract; JSON uses :class:`Health`)."""
    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        db_ok = select_one(conn) == 1
        n_errors = count_recent_errors(conn, hours=1) if db_ok else 0
    status = "ok" if (db_ok and n_errors == 0) else "degraded"
    ts = dt.datetime.now(dt.UTC)
    if as_json:
        payload = Health(
            status=status,
            db_reachable=db_ok,
            n_errors_last_1h=n_errors,
            timestamp=ts,
        )
        _json_dump(payload.model_dump(mode="json"))
        return

    console = Console(highlight=False)
    st_style = "green" if status == "ok" else "yellow"
    table = Table(title=None, show_header=False, box=None, padding=(0, 2))
    table.add_column("k", style="dim", justify="right")
    table.add_column("v")
    table.add_row("status", f"[{st_style}]{status}[/]")
    table.add_row("db_reachable", "yes" if db_ok else "no")
    table.add_row("n_errors_last_1h", str(n_errors))
    table.add_row("timestamp_utc", ts.replace(microsecond=0).isoformat())
    table.add_row("nj_terminal", CLI_VERSION)
    console.print(table)
    console.print(
        "[dim]JSON mode matches GET /health; human table adds nj_terminal only.[/dim]",
    )


@main.command("fec-summary")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON (same keys as GET /fec/summary).",
)
@click.option(
    "--fresh",
    is_flag=True,
    help="Invalidate the 5-minute in-process summary cache before querying.",
)
def cmd_fec_summary(as_json: bool, fresh: bool) -> None:
    """FEC cross-table counts for the latest loaded cycle (fraud UI header).

    Uses the same query layer as ``GET /fec/summary``, including the optional
    ``pg_class.reltuples`` fast path for large contribution tables.
    """
    dsn = _require_dsn()
    if fresh:
        clear_summary_cache()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        snap = get_summary(conn)

    if as_json:
        _json_dump(snap)
        return

    console = Console(highlight=False)
    table = Table(title="FEC summary (latest cycle)", show_header=False, box=None, padding=(0, 2))
    table.add_column("k", style="dim", justify="right")
    table.add_column("v")
    rows_kv = [
        ("cycle", snap.get("cycle") or "—"),
        ("candidates_total", snap.get("candidates_total")),
        ("candidates_nj", snap.get("candidates_nj")),
        ("committees_total", snap.get("committees_total")),
        ("committees_nj_domiciled", snap.get("committees_nj_domiciled")),
        ("contributions_total", snap.get("contributions_total")),
        ("contributions_nj_donor", snap.get("contributions_nj_donor")),
        (
            "contributions_to_nj_candidates",
            snap.get("contributions_to_nj_candidates"),
        ),
        (
            "cycles_available",
            ", ".join(snap.get("cycles_available") or []) or "—",
        ),
    ]
    for label, val in rows_kv:
        table.add_row(label, str(val))
    console.print(table)
    console.print(
        "[dim]Parallel to GET /fec/summary; use --fresh after a bulk load.[/dim]",
    )


@main.command("fec-cycles")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON array (same objects as GET /fec/cycles).",
)
def cmd_fec_cycles(as_json: bool) -> None:
    """Distinct election cycles in ``raw.fec_candidate`` (descending).

    Same as ``fec-enums cycles``. Parallel to ``GET /fec/cycles``.
    """
    _emit_fec_enum_panel("cycles", None, as_json)


@main.command("fec-enums")
@click.argument(
    "kind",
    type=click.Choice(["cycles", "states", "parties", "offices"], case_sensitive=False),
)
@click.option(
    "--cycle",
    default=None,
    help="Limit states, parties, or offices to one election cycle (ignored for cycles).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON array (same objects as the matching GET /fec/* endpoint).",
)
def cmd_fec_enums(kind: str, cycle: str | None, as_json: bool) -> None:
    """Distinct FEC field values for fraud-UI filters (cycles, states, parties, offices).

    Matches ``GET /fec/cycles``, ``/fec/states``, ``/fec/parties``, and ``/fec/offices``.
    """
    _emit_fec_enum_panel(kind, cycle, as_json)


@main.command("fec-metrics")
@click.option(
    "--catalog",
    "list_catalog",
    is_flag=True,
    help="List registered fraud metrics (static registry; no database).",
)
@click.option(
    "--cycle",
    default=None,
    help="Restrict flagged-row counts to this election cycle (e.g. 2024).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Machine-readable output.",
)
def cmd_fec_metrics(
    list_catalog: bool,
    cycle: str | None,
    as_json: bool,
) -> None:
    """Fraud metric catalog or per-signal flagged-row counts.

    Count mode mirrors ``GET /fec/metrics/_summary`` (one COUNT per registered view).
    Catalog mode mirrors ``GET /fec/metrics`` without hitting Postgres.
    """
    if list_catalog:
        specs = get_catalog()
        if as_json:
            _json_dump(
                [
                    {
                        "id": m.id,
                        "name": m.name,
                        "tier": m.tier,
                        "description": m.description,
                        "threshold_note": m.threshold_note,
                        "sort_default": m.sort_default,
                        "primary_key_cols": list(m.primary_key_cols),
                    }
                    for m in specs
                ]
            )
            return

        console = Console(highlight=False)
        table = Table(title="FEC fraud metrics (catalog)", show_lines=False)
        table.add_column("id", style="cyan", no_wrap=True)
        table.add_column("tier")
        table.add_column("name")
        for m in specs:
            table.add_row(m.id, m.tier, m.name)
        console.print(table)
        console.print("[dim]Static registry; same as GET /fec/metrics.[/dim]")
        return

    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        counts = metric_counts(conn, cycle=cycle)

    if as_json:
        _json_dump({"cycle": cycle or "", "counts": counts})
        return

    console = Console(highlight=False)
    cyc_lbl = cycle if cycle else "all cycles"
    mtable = Table(
        title=f"FEC metric flagged rows ({cyc_lbl})",
        show_lines=False,
    )
    mtable.add_column("metric_id", style="cyan", no_wrap=True)
    mtable.add_column("flagged_rows", justify="right")
    for mid in sorted(counts.keys(), key=lambda k: (-counts[k], k)):
        mtable.add_row(mid, str(counts[mid]))
    console.print(mtable)
    console.print(
        "[dim]Parallel to GET /fec/metrics/_summary; use --cycle to match the UI filter.[/dim]",
    )


def _fraud_metric_catalog_entry(spec: MetricSpec) -> FraudMetricCatalogEntry:
    """Return the public catalog row for *spec* (matches the metrics HTTP layer)."""
    return FraudMetricCatalogEntry(
        id=spec.id,
        name=spec.name,
        tier=spec.tier,
        description=spec.description,
        threshold_note=spec.threshold_note,
        sort_default=spec.sort_default,
        primary_key_cols=list(spec.primary_key_cols),
    )


def _metric_cell_pretty(v: object, max_len: int = 36) -> str:
    if v is None:
        return "—"
    s = ",".join(str(x) for x in v) if isinstance(v, list) else str(v)
    return s if len(s) <= max_len else f"{s[: max_len - 1]}…"


def _iter_metric_csv_bytes(columns: list[str], cursor: object) -> Iterator[bytes]:
    """Emit CSV bytes like :mod:`serving.routes.fec_metrics` (lists/array cells)."""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(columns)
    yield buf.getvalue().encode("utf-8")
    buf.seek(0)
    buf.truncate()

    for row in cursor:  # type: ignore[attr-defined]
        writer.writerow(
            [
                ""
                if v is None
                else ("{" + ",".join(str(x) for x in v) + "}" if isinstance(v, list) else v)
                for v in row
            ]
        )
        yield buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate()


def _write_metric_csv_stream(
    output: BinaryIO,
    *,
    cols: list[str],
    cursor: object,
) -> None:
    try:
        for chunk in _iter_metric_csv_bytes(cols, cursor):
            output.write(chunk)
    finally:
        cursor.close()  # type: ignore[attr-defined]


@main.command("fec-metric")
@click.argument("metric_id")
@click.option("--cycle", default=None)
@click.option("--sort-by", "sort_by", default=None)
@click.option(
    "--sort-dir",
    "sort_dir",
    type=click.Choice(["ASC", "DESC", "asc", "desc"]),
    default="DESC",
    show_default=True,
)
@click.option("--limit", default=100, type=click.IntRange(1, 1000), show_default=True)
@click.option("--offset", default=0, type=click.IntRange(0))
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print FraudMetricResult JSON (GET /fec/metrics/{metric_id}).",
)
@click.option(
    "--csv",
    "as_csv",
    is_flag=True,
    help="Stream CSV (GET /fec/metrics/{metric_id}/csv); ignores limit/offset.",
)
@click.option(
    "-o",
    "--output",
    "out",
    type=click.File("wb"),
    default="-",
    help="With --csv: destination (default: stdout).",
)
def cmd_fec_metric(
    metric_id: str,
    cycle: str | None,
    sort_by: str | None,
    sort_dir: str,
    limit: int,
    offset: int,
    as_json: bool,
    as_csv: bool,
    out: BinaryIO,
) -> None:
    """One fraud metric: paginated rows (JSON/human) or capped CSV export."""
    if as_json and as_csv:
        raise click.UsageError("Choose either --json or --csv, not both.")

    try:
        spec = get_metric(metric_id)
    except KeyError:
        known = ", ".join(m.id for m in get_catalog())
        raise click.ClickException(
            f"Unknown fraud metric {metric_id!r}. Available: {known}",
        ) from None

    sd = sort_dir.upper()
    dsn = _require_dsn()

    if as_csv:
        with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
            try:
                cols, cur = stream_metric(
                    conn,
                    metric_id=metric_id,
                    cycle=cycle,
                    sort_by=sort_by,
                    sort_dir=sd,
                )
            except KeyError as exc:
                raise click.ClickException(str(exc)) from None
            _write_metric_csv_stream(out, cols=cols, cursor=cur)
        return

    try:
        with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
            rows, total = list_metric(
                conn,
                metric_id=metric_id,
                cycle=cycle,
                sort_by=sort_by,
                sort_dir=sd,
                limit=limit,
                offset=offset,
            )
    except KeyError as exc:
        raise click.ClickException(str(exc)) from None

    payload = FraudMetricResult(
        metric=_fraud_metric_catalog_entry(spec),
        rows=rows,
        total_count=total,
        limit=limit,
        offset=offset,
    )

    if as_json:
        _json_dump(payload.model_dump(mode="json"))
        return

    console = Console(highlight=False)
    title = (
        f"{spec.name} ({metric_id}) · rows={len(rows)} · "
        f"total={total} · limit={limit} offset={offset}"
    )
    table = Table(title=title, show_lines=False)
    for col in spec.select_cols:
        table.add_column(col[:28], overflow="fold")

    for row in rows:
        table.add_row(
            *(_metric_cell_pretty(row.get(c)) for c in spec.select_cols),
        )

    console.print(table)
    console.print(
        f"[dim]Parallel to GET /fec/metrics/{metric_id}; use --csv for export.[/dim]",
    )


# ============================================================================
# fec-risk -- mirrors GET /fec/risk/entities (queue) and
# fec-risk-entity -- mirrors GET /fec/risk/entities/{kind}/{id} (panel)
#
# Both call serving.queries_fec_risk directly (no HTTP hop). Same contract
# as the FastAPI routes:
#   * KeyError from the query layer  -> click.ClickException (analyst-friendly)
#   * Bad entity_kind                -> click.UsageError    (Click exit 2)
#   * Missing PG_DSN                 -> _require_dsn        (exit 2)
#   * Empty result                   -> exit 0 with empty payload (queue)
#                                       or click.ClickException (panel 404)
# ============================================================================


def _fmt_score(s: float | Decimal | None) -> str:
    """Render risk_score for the human queue (right-aligned 6-char field)."""
    if s is None:
        return "—"
    return f"{float(s):6.2f}"


def _fmt_pct01(p: float | Decimal | None) -> str:
    """Render a [0, 1] percentile for the human queue/panel."""
    if p is None:
        return "—"
    return f"{float(p):.3f}"


def _fmt_signals_short(signals: list[str] | None, max_len: int = 40) -> str:
    """Comma-join + truncate a list of signal_ids for the queue's last column."""
    if not signals:
        return "—"
    s = ",".join(signals)
    return s if len(s) <= max_len else f"{s[: max_len - 1]}…"


@main.command("fec-risk")
@click.option(
    "--cycle",
    default=None,
    help="Restrict to one election cycle (e.g. 2024).",
)
@click.option(
    "--entity-kind",
    "entity_kind",
    type=click.Choice(sorted(VALID_ENTITY_KINDS)),
    default=None,
    help="Restrict to one entity kind (committee/candidate/treasurer/address/donor_cluster).",
)
@click.option(
    "--signal-id",
    "signal_id",
    default=None,
    help="Restrict to entities for which this signal_id fired (see fec-metrics --catalog).",
)
@click.option(
    "--min-score",
    "min_score",
    type=click.FloatRange(0.0, 100.0),
    default=None,
    help="Inclusive lower bound on risk_score [0, 100].",
)
@click.option(
    "--max-score",
    "max_score",
    type=click.FloatRange(0.0, 100.0),
    default=None,
    help="Inclusive upper bound on risk_score [0, 100].",
)
@click.option(
    "--sort-by",
    "sort_by",
    type=click.Choice(sorted(RISK_SORT_COLS)),
    default=None,
    help=f"Sort column. Default: {RISK_DEFAULT_SORT_BY}.",
)
@click.option(
    "--sort-dir",
    "sort_dir",
    type=click.Choice(["ASC", "DESC", "asc", "desc"]),
    default="DESC",
    show_default=True,
)
@click.option(
    "--limit",
    default=RISK_DEFAULT_LIMIT,
    type=click.IntRange(1, RISK_MAX_LIMIT),
    show_default=True,
)
@click.option("--offset", default=0, type=click.IntRange(0))
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print RiskQueueResponse JSON (parallel to GET /fec/risk/entities).",
)
def cmd_fec_risk(
    cycle: str | None,
    entity_kind: str | None,
    signal_id: str | None,
    min_score: float | None,
    max_score: float | None,
    sort_by: str | None,
    sort_dir: str,
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    """Risk-ranked entity queue (parallel to GET /fec/risk/entities).

    The queue is the L3a output: every entity that fired at least one
    fraud signal in the requested cycle, scored 0..100 and ranked DESC
    by default. Use ``--signal-id`` to drill into "everyone who fired
    treasurer_concentration", or ``--min-score`` to gate the queue to
    just leads worth an analyst's time.
    """
    sd = sort_dir.upper()
    dsn = _require_dsn()

    try:
        with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
            rows, total = list_risk_entities(
                conn,
                cycle=cycle,
                entity_kind=entity_kind,
                signal_id=signal_id,
                min_score=min_score,
                max_score=max_score,
                sort_by=sort_by,
                sort_dir=sd,
                limit=limit,
                offset=offset,
            )
    except KeyError as exc:
        # Whitelist failure (sort_by, entity_kind) or score-range failure
        # -- the same KeyError the HTTP layer translates to 400.
        raise click.ClickException(str(exc)) from None

    queue_rows = [RiskQueueRow.model_validate(r) for r in rows]
    payload = RiskQueueResponse(
        rows=queue_rows,
        total_count=total,
        limit=limit,
        offset=offset,
        filters={
            "cycle":       cycle,
            "entity_kind": entity_kind,
            "signal_id":   signal_id,
            "min_score":   min_score,
            "max_score":   max_score,
            "sort_by":     sort_by or RISK_DEFAULT_SORT_BY,
            "sort_dir":    sd,
        },
    )

    if as_json:
        _json_dump(payload.model_dump(mode="json"))
        return

    console = Console(highlight=False)
    cyc_lbl = cycle if cycle else "all cycles"
    filt_bits: list[str] = [f"cycle={cyc_lbl}"]
    if entity_kind:
        filt_bits.append(f"kind={entity_kind}")
    if signal_id:
        filt_bits.append(f"signal={signal_id}")
    if min_score is not None:
        filt_bits.append(f"score>={min_score:g}")
    if max_score is not None:
        filt_bits.append(f"score<={max_score:g}")
    title = (
        "FEC fraud-risk queue · " + " · ".join(filt_bits) +
        f" · rows={len(queue_rows)}/{total}"
    )

    table = Table(title=title, show_lines=False)
    table.add_column("cycle", no_wrap=True)
    table.add_column("kind",  no_wrap=True)
    table.add_column("entity_id", overflow="fold")
    table.add_column("score", justify="right")
    table.add_column("n_sig", justify="right")
    table.add_column("max_sev", justify="right")
    table.add_column("max_pct", justify="right")
    table.add_column("peer_bucket", overflow="fold")
    table.add_column("signals", overflow="fold")
    for r in queue_rows:
        table.add_row(
            r.cycle,
            r.entity_kind,
            _trunc_cell(r.entity_id, max_len=32),
            _fmt_score(r.risk_score),
            str(r.n_signals_fired),
            str(r.max_severity),
            _fmt_pct01(r.max_peer_percentile),
            _trunc_cell(r.primary_peer_bucket, max_len=24),
            _fmt_signals_short(r.signals_fired),
        )
    console.print(table)
    console.print(
        "[dim]Parallel to GET /fec/risk/entities. Drill into a row with "
        "fec-risk-entity <kind> <id>.[/dim]",
    )


def _fmt_phi(x: float | Decimal | None) -> str:
    """Render phi_contribution. The interesting range is 0.0 .. ~1.0."""
    if x is None:
        return "—"
    return f"{float(x):.4f}"


@main.command("fec-risk-entity")
@click.argument("entity_kind", type=click.Choice(sorted(VALID_ENTITY_KINDS)))
@click.argument("entity_id")
@click.option(
    "--cycle",
    default=None,
    help="Election cycle. Omit for the most recent observation.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print RiskEntityPanel JSON (parallel to GET /fec/risk/entities/{kind}/{id}).",
)
@click.option(
    "--csv",
    "as_csv",
    is_flag=True,
    help=(
        "Stream CSV (parallel to GET /fec/risk/entities/{kind}/{id}/csv); "
        "one row per fired signal."
    ),
)
@click.option(
    "-o",
    "--output",
    "out",
    type=click.File("wb"),
    default="-",
    help="With --csv: destination (default: stdout).",
)
def cmd_fec_risk_entity(
    entity_kind: str,
    entity_id: str,
    cycle: str | None,
    as_json: bool,
    as_csv: bool,
    out: BinaryIO,
) -> None:
    """Evidence panel for one entity (parallel to GET /fec/risk/entities/{kind}/{id}).

    Backs up the score with the per-signal observations that produced
    it: severity, peer percentile, peer bucket, raw value, the
    ``phi_contribution`` (raw additive term in the score's pre-EXP
    sum), and ``score_share_pct`` (this signal's share of the total
    raw_sum). Observations are sorted by ``score_share_pct DESC`` so
    the biggest contributors appear at the top.

    Use ``--csv`` for an analyst-friendly export (one row per fired
    signal, entity columns repeated on every row).
    """
    if as_json and as_csv:
        raise click.UsageError("Choose either --json or --csv, not both.")

    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        panel_dict = get_risk_entity(
            conn,
            entity_kind=entity_kind,
            entity_id=entity_id,
            cycle=cycle,
        )
    if panel_dict is None:
        raise click.ClickException(
            f"No fired signals for {entity_kind}/{entity_id}"
            + (f" in cycle {cycle}" if cycle else "")
            + ". Either the entity is below all thresholds, or it is "
            "unknown to the L1 layer.",
        )

    if as_csv:
        # Write CSV directly from the dict; no Pydantic roundtrip needed
        # (the column shape is the contract, not the model).
        rows = risk_evidence_csv_rows(panel_dict)
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(RISK_EVIDENCE_CSV_COLUMNS)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
        out.write(buf.getvalue().encode("utf-8"))
        return

    observations = [
        RiskSignalObservation.model_validate(o) for o in panel_dict["observations"]
    ]
    panel = RiskEntityPanel(
        cycle=panel_dict["cycle"],
        entity_kind=panel_dict["entity_kind"],
        entity_id=panel_dict["entity_id"],
        risk_score=panel_dict["risk_score"],
        n_signals_fired=panel_dict["n_signals_fired"],
        max_severity=panel_dict["max_severity"],
        max_peer_percentile=panel_dict["max_peer_percentile"],
        avg_peer_percentile=panel_dict["avg_peer_percentile"],
        primary_peer_bucket=panel_dict["primary_peer_bucket"],
        last_observation_at=panel_dict["last_observation_at"],
        observations=observations,
    )

    if as_json:
        _json_dump(panel.model_dump(mode="json"))
        return

    console = Console(highlight=False)
    header = (
        f"[bold]{panel.entity_id}[/bold] "
        f"({panel.entity_kind}) · cycle {panel.cycle} · "
        f"risk_score [bold]{panel.risk_score:.2f}[/bold] · "
        f"n_signals {panel.n_signals_fired} · "
        f"max_sev {panel.max_severity} · "
        f"max_pct {panel.max_peer_percentile:.3f} · "
        f"peer_bucket {panel.primary_peer_bucket}"
    )
    console.print(header)

    table = Table(
        title=f"Evidence panel ({len(observations)} observations)",
        show_lines=False,
    )
    table.add_column("signal_id", overflow="fold", no_wrap=False)
    table.add_column("sev", justify="right")
    table.add_column("peer_pct", justify="right")
    table.add_column("peer_bucket", overflow="fold")
    table.add_column("raw", justify="right")
    table.add_column("phi", justify="right")
    table.add_column("share%", justify="right")
    table.add_column("evidence_url", overflow="fold")
    for o in observations:
        table.add_row(
            o.signal_id,
            str(o.severity),
            _fmt_pct01(o.peer_percentile),
            _trunc_cell(o.peer_bucket, max_len=20),
            _fmt_phi(o.raw_value),
            _fmt_phi(o.phi_contribution),
            f"{o.score_share_pct:.1f}",
            _trunc_cell(o.evidence_url, max_len=40),
        )
    console.print(table)
    console.print(
        f"[dim]Parallel to GET /fec/risk/entities/{panel.entity_kind}/{panel.entity_id}. "
        "Observations sorted by score_share_pct DESC.[/dim]",
    )


def _fmt_money_amt(x: object) -> str:
    if x is None:
        return "—"
    if isinstance(x, (int, float, Decimal)):
        return f"{float(x):,.2f}"
    return str(x)


def _trunc_cell(s: str | None, max_len: int = 26) -> str:
    if s is None:
        return "—"
    return s if len(s) <= max_len else f"{s[: max_len - 1]}…"


def _iter_fec_csv_bytes(columns: list[str], cursor: object) -> Iterator[bytes]:
    """Emit CSV bytes like :mod:`serving.routes.fec_export` (server-side cursor)."""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(columns)
    yield buf.getvalue().encode("utf-8")
    buf.seek(0)
    buf.truncate()

    for row in cursor:  # type: ignore[attr-defined]
        writer.writerow(["" if v is None else v for v in row])
        yield buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate()


def _write_fec_csv_stream(
    output: BinaryIO,
    *,
    cols: list[str],
    cursor: object,
) -> None:
    try:
        for chunk in _iter_fec_csv_bytes(cols, cursor):
            output.write(chunk)
    finally:
        cursor.close()  # type: ignore[attr-defined]


@main.command("fec-money-nj")
@click.option("--cycle", default=None, help="Election cycle (e.g. 2024).")
@click.option("--cand-id", "cand_id", default=None)
@click.option("--party", default=None)
@click.option("--office", default=None)
@click.option("--donor-state", "donor_state", default=None)
@click.option("--donor-name-contains", "donor_name_contains", default=None)
@click.option("--min-amount", "min_amount", type=float, default=None)
@click.option("--max-amount", "max_amount", type=float, default=None)
@click.option("--start-date", "start_date", default=None)
@click.option("--end-date", "end_date", default=None)
@click.option(
    "--include-memo",
    "include_memo",
    is_flag=True,
    help="Include memo-coded rows (default: exclude, matching the HTTP API).",
)
@click.option("--sort-by", "sort_by", default="transaction_date")
@click.option(
    "--sort-dir",
    "sort_dir",
    type=click.Choice(["ASC", "DESC", "asc", "desc"]),
    default="DESC",
    show_default=True,
)
@click.option("--limit", default=100, type=click.IntRange(1, 1000), show_default=True)
@click.option("--offset", default=0, type=click.IntRange(0))
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON (:class:`FecPagedResponse` shape).",
)
def cmd_fec_money_nj(
    cycle: str | None,
    cand_id: str | None,
    party: str | None,
    office: str | None,
    donor_state: str | None,
    donor_name_contains: str | None,
    min_amount: float | None,
    max_amount: float | None,
    start_date: str | None,
    end_date: str | None,
    include_memo: bool,
    sort_by: str,
    sort_dir: str,
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    """Contributions to committees tied to NJ candidates (headline fraud table).

    Parallel to ``GET /fec/money-to-nj`` with the same filters and pagination.
    """
    dsn = _require_dsn()
    f = MoneyToNjFilters(
        cycle=cycle,
        cand_id=cand_id,
        party=party,
        office=office,
        donor_state=donor_state,
        donor_name_contains=donor_name_contains,
        min_amount=min_amount,
        max_amount=max_amount,
        start_date=start_date,
        end_date=end_date,
        exclude_memo=not include_memo,
    )
    sd = sort_dir.upper()

    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        raw_rows, total = list_money_to_nj(
            conn,
            f=f,
            sort_by=sort_by,
            sort_dir=sd,
            limit=limit,
            offset=offset,
        )

    rows = [FecMoneyToNjRow.model_validate(r) for r in raw_rows]

    if as_json:
        payload = FecPagedResponse(
            rows=[x.model_dump(mode="json") for x in rows],
            total_count=total,
            limit=limit,
            offset=offset,
        )
        _json_dump(payload.model_dump(mode="json"))
        return

    console = Console(highlight=False)
    title = (
        f"FEC money to NJ candidates · page rows={len(rows)} · "
        f"total={total} · limit={limit} offset={offset}"
    )
    table = Table(title=title, show_lines=False)
    table.add_column("date")
    table.add_column("$", justify="right")
    table.add_column("candidate")
    table.add_column("contributor")
    table.add_column("st")
    table.add_column("cycle", justify="right")

    for r in rows:
        d_str = str(r.transaction_date) if r.transaction_date else "—"
        table.add_row(
            d_str,
            _fmt_money_amt(r.transaction_amount),
            _trunc_cell(r.cand_name),
            _trunc_cell(r.contributor_name),
            (r.contributor_state or "—")[:2],
            r.cycle,
        )

    console.print(table)
    console.print("[dim]Parallel to GET /fec/money-to-nj.[/dim]")


@main.command("fec-contributions")
@click.option("--cycle", default=None, help="Election cycle (e.g. 2024).")
@click.option("--cmte-id", "cmte_id", default=None)
@click.option("--donor-state", "donor_state", default=None)
@click.option("--donor-name-contains", "donor_name_contains", default=None)
@click.option("--employer-contains", "employer_contains", default=None)
@click.option("--occupation-contains", "occupation_contains", default=None)
@click.option("--transaction-type", "transaction_type", default=None)
@click.option("--min-amount", "min_amount", type=float, default=None)
@click.option("--max-amount", "max_amount", type=float, default=None)
@click.option("--start-date", "start_date", default=None)
@click.option("--end-date", "end_date", default=None)
@click.option(
    "--include-memo",
    "include_memo",
    is_flag=True,
    help="Include memo-coded rows (default: exclude, matching the HTTP API).",
)
@click.option("--sort-by", "sort_by", default="transaction_date")
@click.option(
    "--sort-dir",
    "sort_dir",
    type=click.Choice(["ASC", "DESC", "asc", "desc"]),
    default="DESC",
    show_default=True,
)
@click.option("--limit", default=100, type=click.IntRange(1, 1000), show_default=True)
@click.option("--offset", default=0, type=click.IntRange(0))
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print JSON (:class:`FecPagedResponse` shape).",
)
def cmd_fec_contributions(
    cycle: str | None,
    cmte_id: str | None,
    donor_state: str | None,
    donor_name_contains: str | None,
    employer_contains: str | None,
    occupation_contains: str | None,
    transaction_type: str | None,
    min_amount: float | None,
    max_amount: float | None,
    start_date: str | None,
    end_date: str | None,
    include_memo: bool,
    sort_by: str,
    sort_dir: str,
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    """Paged individual contributions (``public.v_fec_contribution``).

    Parallel to ``GET /fec/contributions``.
    """
    dsn = _require_dsn()
    f = ContributionFilters(
        cycle=cycle,
        cmte_id=cmte_id,
        donor_state=donor_state,
        donor_name_contains=donor_name_contains,
        employer_contains=employer_contains,
        occupation_contains=occupation_contains,
        transaction_type=transaction_type,
        min_amount=min_amount,
        max_amount=max_amount,
        start_date=start_date,
        end_date=end_date,
        exclude_memo=not include_memo,
    )
    sd = sort_dir.upper()

    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        raw_rows, total = list_contributions(
            conn,
            f=f,
            sort_by=sort_by,
            sort_dir=sd,
            limit=limit,
            offset=offset,
        )

    rows = [FecContributionRow.model_validate(r) for r in raw_rows]

    if as_json:
        payload = FecPagedResponse(
            rows=[x.model_dump(mode="json") for x in rows],
            total_count=total,
            limit=limit,
            offset=offset,
        )
        _json_dump(payload.model_dump(mode="json"))
        return

    console = Console(highlight=False)
    title = (
        f"FEC contributions · page rows={len(rows)} · total={total} · limit={limit} offset={offset}"
    )
    table = Table(title=title, show_lines=False)
    table.add_column("date")
    table.add_column("$", justify="right")
    table.add_column("cmte")
    table.add_column("contributor")
    table.add_column("st")
    table.add_column("cycle", justify="right")

    for r in rows:
        d_str = str(r.transaction_date) if r.transaction_date else "—"
        table.add_row(
            d_str,
            _fmt_money_amt(r.transaction_amount),
            _trunc_cell(r.cmte_id, max_len=14),
            _trunc_cell(r.contributor_name),
            (r.contributor_state or "—")[:2],
            r.cycle,
        )

    console.print(table)
    console.print("[dim]Parallel to GET /fec/contributions.[/dim]")


@main.command("fec-candidates")
@click.option("--cycle", default=None)
@click.option("--state", default=None, help="cand_office_st (e.g. NJ).")
@click.option("--office", default=None, help="H / S / P")
@click.option("--party", default=None)
@click.option("--incumbent", default=None, help="I / C / O")
@click.option("--status", default=None, help="Candidate status code (e.g. C).")
@click.option("--name-contains", "name_contains", default=None)
@click.option("--sort-by", "sort_by", default="cand_name")
@click.option(
    "--sort-dir",
    "sort_dir",
    type=click.Choice(["ASC", "DESC", "asc", "desc"]),
    default="ASC",
    show_default=True,
)
@click.option("--limit", default=100, type=click.IntRange(1, 1000), show_default=True)
@click.option("--offset", default=0, type=click.IntRange(0))
@click.option("--json", "as_json", is_flag=True)
def cmd_fec_candidates(
    cycle: str | None,
    state: str | None,
    office: str | None,
    party: str | None,
    incumbent: str | None,
    status: str | None,
    name_contains: str | None,
    sort_by: str,
    sort_dir: str,
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    """Paged FEC candidates (``raw.fec_candidate``).

    Parallel to ``GET /fec/candidates``.
    """
    dsn = _require_dsn()
    f = CandidateFilters(
        cycle=cycle,
        state=state,
        office=office,
        party=party,
        incumbent=incumbent,
        status=status,
        name_contains=name_contains,
    )
    sd = sort_dir.upper()

    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        raw_rows, total = list_candidates(
            conn,
            f=f,
            sort_by=sort_by,
            sort_dir=sd,
            limit=limit,
            offset=offset,
        )

    rows = [FecCandidateRow.model_validate(r) for r in raw_rows]

    if as_json:
        payload = FecPagedResponse(
            rows=[x.model_dump(mode="json") for x in rows],
            total_count=total,
            limit=limit,
            offset=offset,
        )
        _json_dump(payload.model_dump(mode="json"))
        return

    console = Console(highlight=False)
    title = f"FEC candidates · rows={len(rows)} · total={total} · limit={limit} offset={offset}"
    table = Table(title=title, show_lines=False)
    table.add_column("cand_id", style="cyan", no_wrap=True)
    table.add_column("name")
    table.add_column("pty")
    table.add_column("off")
    table.add_column("st")
    table.add_column("dist")
    table.add_column("cycle", justify="right")

    for r in rows:
        table.add_row(
            r.cand_id,
            _trunc_cell(r.cand_name, max_len=28),
            r.cand_pty_affiliation or "—",
            r.cand_office or "—",
            r.cand_office_st or "—",
            r.cand_office_district or "—",
            r.cycle,
        )

    console.print(table)
    console.print("[dim]Parallel to GET /fec/candidates.[/dim]")


@main.command("fec-committees")
@click.option("--cycle", default=None)
@click.option("--state", default=None, help="Committee state (cmte_st).")
@click.option("--cmte-type", "cmte_type", default=None)
@click.option("--designation", default=None)
@click.option("--party", default=None)
@click.option("--org-type", "org_type", default=None)
@click.option("--name-contains", "name_contains", default=None)
@click.option(
    "--has-candidate",
    "has_candidate_opt",
    type=click.Choice(["yes", "no"]),
    default=None,
    help="yes=cand_id present; no=cand_id null; omit=no filter.",
)
@click.option("--sort-by", "sort_by", default="cmte_nm")
@click.option(
    "--sort-dir",
    "sort_dir",
    type=click.Choice(["ASC", "DESC", "asc", "desc"]),
    default="ASC",
    show_default=True,
)
@click.option("--limit", default=100, type=click.IntRange(1, 1000), show_default=True)
@click.option("--offset", default=0, type=click.IntRange(0))
@click.option("--json", "as_json", is_flag=True)
def cmd_fec_committees(
    cycle: str | None,
    state: str | None,
    cmte_type: str | None,
    designation: str | None,
    party: str | None,
    org_type: str | None,
    name_contains: str | None,
    has_candidate_opt: str | None,
    sort_by: str,
    sort_dir: str,
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    """Paged FEC committees (``raw.fec_committee``).

    Parallel to ``GET /fec/committees``.
    """
    has_candidate = None if has_candidate_opt is None else has_candidate_opt == "yes"

    dsn = _require_dsn()
    f = CommitteeFilters(
        cycle=cycle,
        state=state,
        cmte_type=cmte_type,
        designation=designation,
        party=party,
        org_type=org_type,
        name_contains=name_contains,
        has_candidate=has_candidate,
    )
    sd = sort_dir.upper()

    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        raw_rows, total = list_committees(
            conn,
            f=f,
            sort_by=sort_by,
            sort_dir=sd,
            limit=limit,
            offset=offset,
        )

    rows = [FecCommitteeRow.model_validate(r) for r in raw_rows]

    if as_json:
        payload = FecPagedResponse(
            rows=[x.model_dump(mode="json", by_alias=False) for x in rows],
            total_count=total,
            limit=limit,
            offset=offset,
        )
        _json_dump(payload.model_dump(mode="json"))
        return

    console = Console(highlight=False)
    title = f"FEC committees · rows={len(rows)} · total={total} · limit={limit} offset={offset}"
    table = Table(title=title, show_lines=False)
    table.add_column("cmte_id", style="cyan", no_wrap=True)
    table.add_column("name")
    table.add_column("st")
    table.add_column("type")
    table.add_column("cand_id")
    table.add_column("cycle", justify="right")

    for r in rows:
        table.add_row(
            r.cmte_id,
            _trunc_cell(r.committee_name, max_len=30),
            r.cmte_st or "—",
            r.cmte_tp or "—",
            _trunc_cell(r.cand_id, max_len=12) if r.cand_id else "—",
            r.cycle,
        )

    console.print(table)
    console.print("[dim]Parallel to GET /fec/committees.[/dim]")


@main.command("fec-candidate")
@click.argument("cand_id")
@click.option("--cycle", default=None)
@click.option("--json", "as_json", is_flag=True)
def cmd_fec_candidate(
    cand_id: str,
    cycle: str | None,
    as_json: bool,
) -> None:
    """Single FEC candidate with linked committees (``raw.fec_candidate``).

    Parallel to ``GET /fec/candidates/{cand_id}``.
    """
    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        raw = get_candidate_detail(conn, cand_id=cand_id, cycle=cycle)

    if raw is None:
        raise click.ClickException(f"candidate {cand_id} not found")

    committees_raw = raw.pop("linked_committees", []) or []
    committees = [FecCommitteeRow.model_validate(c) for c in committees_raw]
    detail = FecCandidateDetail.model_validate({**raw, "linked_committees": committees})

    if as_json:
        _json_dump(detail.model_dump(mode="json"))
        return

    console = Console(highlight=False)
    title = (
        f"{detail.cand_name or '—'} · {detail.cand_id} · "
        f"cycle {detail.cycle} · "
        f"{detail.cand_pty_affiliation or '—'} · "
        f"{detail.cand_office or '—'}/{detail.cand_office_st or '—'}"
    )
    console.print(title)
    addr_bits = [
        detail.cand_st1,
        detail.cand_st2,
        " ".join(x for x in (detail.cand_city, detail.cand_st, detail.cand_zip) if x),
    ]
    addr_line = ", ".join(x for x in addr_bits if x)
    if addr_line:
        console.print(f"[dim]{addr_line}[/dim]")

    sub = Table(
        title=f"Linked committees ({len(detail.linked_committees)})",
        show_lines=False,
    )
    sub.add_column("cmte_id", style="cyan", no_wrap=True)
    sub.add_column("name")
    sub.add_column("tp")
    sub.add_column("dsgn")
    sub.add_column("cycle", justify="right")
    for c in detail.linked_committees:
        sub.add_row(
            c.cmte_id,
            _trunc_cell(c.committee_name, max_len=32),
            c.cmte_tp or "—",
            c.cmte_dsgn or "—",
            c.cycle,
        )
    console.print(sub)
    console.print("[dim]Parallel to GET /fec/candidates/{cand_id}.[/dim]")


@main.command("fec-committee")
@click.argument("cmte_id")
@click.option("--cycle", default=None)
@click.option("--json", "as_json", is_flag=True)
def cmd_fec_committee(
    cmte_id: str,
    cycle: str | None,
    as_json: bool,
) -> None:
    """Single FEC committee with candidate + recent contributions.

    Parallel to ``GET /fec/committees/{cmte_id}``.
    """
    dsn = _require_dsn()
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        raw = get_committee_detail(conn, cmte_id=cmte_id, cycle=cycle)

    if raw is None:
        raise click.ClickException(f"committee {cmte_id} not found")

    cand_raw = raw.pop("linked_candidate", None)
    contribs_raw = raw.pop("recent_contributions", []) or []
    detail = FecCommitteeDetail.model_validate(
        {
            **raw,
            "linked_candidate": (FecCandidateRow.model_validate(cand_raw) if cand_raw else None),
            "recent_contributions": [FecContributionRow.model_validate(c) for c in contribs_raw],
        }
    )

    if as_json:
        _json_dump(detail.model_dump(mode="json", by_alias=False))
        return

    console = Console(highlight=False)
    title = (
        f"{detail.committee_name or '—'} · {detail.cmte_id} · "
        f"cycle {detail.cycle} · {detail.cmte_st or '—'}"
    )
    console.print(title)

    lc = detail.linked_candidate
    if lc:
        console.print(
            f"[dim]Candidate:[/dim] {lc.cand_name or '—'} "
            f"({lc.cand_id}) · {lc.cand_pty_affiliation or '—'} "
            f"{lc.cand_office or '—'}/{lc.cand_office_st or '—'}",
        )

    sub = Table(
        title=f"Recent contributions ({len(detail.recent_contributions)})",
        show_lines=False,
    )
    sub.add_column("date")
    sub.add_column("$", justify="right")
    sub.add_column("contributor")
    sub.add_column("st")
    sub.add_column("sub_id", no_wrap=True)
    for r in detail.recent_contributions:
        d_str = str(r.transaction_date) if r.transaction_date else "—"
        sub.add_row(
            d_str,
            _fmt_money_amt(r.transaction_amount),
            _trunc_cell(r.contributor_name),
            (r.contributor_state or "—")[:2],
            _trunc_cell(r.sub_id, max_len=18),
        )
    console.print(sub)
    console.print("[dim]Parallel to GET /fec/committees/{cmte_id}.[/dim]")


@main.command("fec-export-candidates")
@click.option("--cycle", default=None)
@click.option("--state", default=None, help="cand_office_st (e.g. NJ).")
@click.option("--office", default=None, help="H / S / P")
@click.option("--party", default=None)
@click.option("--incumbent", default=None, help="I / C / O")
@click.option("--status", default=None, help="Candidate status code (e.g. C).")
@click.option("--name-contains", "name_contains", default=None)
@click.option(
    "-o",
    "--output",
    "out",
    type=click.File("wb"),
    default="-",
    help=f"Destination (default: stdout). At most {MAX_EXPORT} rows (API cap).",
)
def cmd_fec_export_candidates(
    cycle: str | None,
    state: str | None,
    office: str | None,
    party: str | None,
    incumbent: str | None,
    status: str | None,
    name_contains: str | None,
    out: BinaryIO,
) -> None:
    """Stream ``raw.fec_candidate`` as CSV (same filters as the HTTP export)."""
    dsn = _require_dsn()
    f = CandidateFilters(
        cycle=cycle,
        state=state,
        office=office,
        party=party,
        incumbent=incumbent,
        status=status,
        name_contains=name_contains,
    )
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        cols, cur = stream_candidates(conn, f=f)
        _write_fec_csv_stream(out, cols=cols, cursor=cur)


@main.command("fec-export-committees")
@click.option("--cycle", default=None)
@click.option("--state", default=None, help="Committee state (cmte_st).")
@click.option("--cmte-type", "cmte_type", default=None)
@click.option("--designation", default=None)
@click.option("--party", default=None)
@click.option("--org-type", "org_type", default=None)
@click.option("--name-contains", "name_contains", default=None)
@click.option(
    "--has-candidate",
    "has_candidate_opt",
    type=click.Choice(["yes", "no"]),
    default=None,
    help="yes=cand_id present; no=cand_id null; omit=no filter.",
)
@click.option(
    "-o",
    "--output",
    "out",
    type=click.File("wb"),
    default="-",
    help=f"Destination (default: stdout). At most {MAX_EXPORT} rows (API cap).",
)
def cmd_fec_export_committees(
    cycle: str | None,
    state: str | None,
    cmte_type: str | None,
    designation: str | None,
    party: str | None,
    org_type: str | None,
    name_contains: str | None,
    has_candidate_opt: str | None,
    out: BinaryIO,
) -> None:
    """Stream ``raw.fec_committee`` as CSV (same filters as the HTTP export)."""
    has_candidate = None if has_candidate_opt is None else has_candidate_opt == "yes"
    dsn = _require_dsn()
    f = CommitteeFilters(
        cycle=cycle,
        state=state,
        cmte_type=cmte_type,
        designation=designation,
        party=party,
        org_type=org_type,
        name_contains=name_contains,
        has_candidate=has_candidate,
    )
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        cols, cur = stream_committees(conn, f=f)
        _write_fec_csv_stream(out, cols=cols, cursor=cur)


@main.command("fec-export-contributions")
@click.option("--cycle", default=None, help="Election cycle (e.g. 2024).")
@click.option("--cmte-id", "cmte_id", default=None)
@click.option("--donor-state", "donor_state", default=None)
@click.option("--donor-name-contains", "donor_name_contains", default=None)
@click.option("--employer-contains", "employer_contains", default=None)
@click.option("--occupation-contains", "occupation_contains", default=None)
@click.option("--transaction-type", "transaction_type", default=None)
@click.option("--min-amount", "min_amount", type=float, default=None)
@click.option("--max-amount", "max_amount", type=float, default=None)
@click.option("--start-date", "start_date", default=None)
@click.option("--end-date", "end_date", default=None)
@click.option(
    "--include-memo",
    "include_memo",
    is_flag=True,
    help="Include memo-coded rows (default: exclude, matching the HTTP export).",
)
@click.option(
    "-o",
    "--output",
    "out",
    type=click.File("wb"),
    default="-",
    help=f"Destination (default: stdout). At most {MAX_EXPORT} rows (API cap).",
)
def cmd_fec_export_contributions(
    cycle: str | None,
    cmte_id: str | None,
    donor_state: str | None,
    donor_name_contains: str | None,
    employer_contains: str | None,
    occupation_contains: str | None,
    transaction_type: str | None,
    min_amount: float | None,
    max_amount: float | None,
    start_date: str | None,
    end_date: str | None,
    include_memo: bool,
    out: BinaryIO,
) -> None:
    """Stream ``public.v_fec_contribution`` as CSV (same filters as the HTTP export)."""
    dsn = _require_dsn()
    f = ContributionFilters(
        cycle=cycle,
        cmte_id=cmte_id,
        donor_state=donor_state,
        donor_name_contains=donor_name_contains,
        employer_contains=employer_contains,
        occupation_contains=occupation_contains,
        transaction_type=transaction_type,
        min_amount=min_amount,
        max_amount=max_amount,
        start_date=start_date,
        end_date=end_date,
        exclude_memo=not include_memo,
    )
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        cols, cur = stream_contributions(conn, f=f)
        _write_fec_csv_stream(out, cols=cols, cursor=cur)


@main.command("fec-export-money-nj")
@click.option("--cycle", default=None, help="Election cycle (e.g. 2024).")
@click.option("--cand-id", "cand_id", default=None)
@click.option("--party", default=None)
@click.option("--office", default=None)
@click.option("--donor-state", "donor_state", default=None)
@click.option("--donor-name-contains", "donor_name_contains", default=None)
@click.option("--min-amount", "min_amount", type=float, default=None)
@click.option("--max-amount", "max_amount", type=float, default=None)
@click.option("--start-date", "start_date", default=None)
@click.option("--end-date", "end_date", default=None)
@click.option(
    "--include-memo",
    "include_memo",
    is_flag=True,
    help="Include memo-coded rows (default: exclude, matching the HTTP export).",
)
@click.option(
    "-o",
    "--output",
    "out",
    type=click.File("wb"),
    default="-",
    help=f"Destination (default: stdout). At most {MAX_EXPORT} rows (API cap).",
)
def cmd_fec_export_money_nj(
    cycle: str | None,
    cand_id: str | None,
    party: str | None,
    office: str | None,
    donor_state: str | None,
    donor_name_contains: str | None,
    min_amount: float | None,
    max_amount: float | None,
    start_date: str | None,
    end_date: str | None,
    include_memo: bool,
    out: BinaryIO,
) -> None:
    """Stream ``public.v_fec_money_to_nj_candidates`` as CSV (HTTP export parity)."""
    dsn = _require_dsn()
    f = MoneyToNjFilters(
        cycle=cycle,
        cand_id=cand_id,
        party=party,
        office=office,
        donor_state=donor_state,
        donor_name_contains=donor_name_contains,
        min_amount=min_amount,
        max_amount=max_amount,
        start_date=start_date,
        end_date=end_date,
        exclude_memo=not include_memo,
    )
    with psycopg.connect(dsn, application_name="nj_terminal_cli") as conn:
        cols, cur = stream_money_to_nj(conn, f=f)
        _write_fec_csv_stream(out, cols=cols, cursor=cur)


if __name__ == "__main__":
    main()
