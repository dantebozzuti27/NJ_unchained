"""LCA -> ``derived.lca_wage_by_county_yr_visa`` aggregator.

Reads ``raw.lca_disclosure`` (filtered to ``case_status = 'CERTIFIED'``)
joined to ``ref.v_zip_known_counties`` (the NJ-only HUD crosswalk view),
groups by ``(county_id, fiscal_year, visa_class)``, and writes
``derived.lca_wage_by_county_yr_visa`` with weighted percentiles.

Vintage-binding rule
--------------------
For each LCA row with ``fiscal_year = FY``, the crosswalk vintage used is
the **latest** ``ref.zip_county`` row whose
``vintage_year <= FY`` (and within that year, the latest quarter).

This is deterministic and reproducible: a recompute against the same
database state produces byte-identical output, so ``input_vintage_hash``
is stable and ``derived.lca_wage_by_county_yr_visa`` rows can be deduped
on ``(county_id, fiscal_year, visa_class, formula_version,
input_vintage_hash)``.

Suppression
-----------
The aggregator computes percentiles unconditionally. The CHECK
constraint on ``derived.lca_wage_by_county_yr_visa`` (see migration 011)
refuses rows where ``n_unweighted_certs < 10`` unless the four percentile
columns are NULL. This module honors that contract by NULL-ing the
percentile columns for thin cells before INSERT.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from derived._stats import weighted_percentile

if TYPE_CHECKING:
    import psycopg

log = logging.getLogger(__name__)

DEFAULT_FORMULA_VERSION: Final[str] = "1.0.0-baseline"
SUPPRESSION_MIN_N: Final[int] = 10


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ObservationRow:
    """One LCA row joined to one ZIP-county allocation."""

    county_id: str
    fiscal_year: int
    visa_class: str
    bus_ratio: float
    total_workers: float | None
    annualized_wage_from: float | None
    annualized_pw: float | None


@dataclass(frozen=True)
class AggregateRow:
    """One row of ``derived.lca_wage_by_county_yr_visa`` ready to INSERT."""

    county_id: str
    fiscal_year: int
    visa_class: str
    n_unweighted_certs: int
    n_certs_weighted: float
    n_workers_weighted: float
    median_annualized_wage_from: float | None
    p25_annualized_wage_from: float | None
    p75_annualized_wage_from: float | None
    median_prevailing_wage: float | None
    formula_version: str
    input_vintage_hash: str


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Vintage-binding subquery: for each (zip5, fiscal_year), return the
# latest (vintage_year, vintage_quarter) <= fiscal_year. Computed as
# vintage_year * 10 + vintage_quarter for cheap MAX comparison.
_QUERY_OBSERVATIONS = """
WITH lca AS (
    SELECT
        case_number, worksite_idx, fiscal_year, visa_class,
        worksite_postal_code, total_workers,
        annualized_wage_from, annualized_pw
    FROM raw.lca_disclosure
    WHERE case_status = 'CERTIFIED'
      AND worksite_postal_code IS NOT NULL
),
zip_vintage AS (
    SELECT
        lca.case_number, lca.worksite_idx, lca.fiscal_year,
        lca.worksite_postal_code AS zip5,
        MAX(zc.vintage_year * 10 + zc.vintage_quarter) AS vintage_key
    FROM lca
    JOIN ref.v_zip_known_counties zc
      ON zc.zip5 = lca.worksite_postal_code
     AND zc.vintage_year <= lca.fiscal_year
    GROUP BY lca.case_number, lca.worksite_idx, lca.fiscal_year,
             lca.worksite_postal_code
),
allocation AS (
    SELECT
        lca.case_number, lca.worksite_idx, lca.fiscal_year, lca.visa_class,
        lca.total_workers,
        lca.annualized_wage_from, lca.annualized_pw,
        zc.county_id, zc.bus_ratio, zc.vintage_year, zc.vintage_quarter
    FROM lca
    JOIN zip_vintage zv
      ON zv.case_number = lca.case_number
     AND zv.worksite_idx = lca.worksite_idx
     AND zv.fiscal_year = lca.fiscal_year
    JOIN ref.v_zip_known_counties zc
      ON zc.zip5 = lca.worksite_postal_code
     AND (zc.vintage_year * 10 + zc.vintage_quarter) = zv.vintage_key
    WHERE zc.bus_ratio > 0
)
SELECT
    county_id, fiscal_year, visa_class,
    bus_ratio, total_workers,
    annualized_wage_from, annualized_pw
FROM allocation
ORDER BY county_id, fiscal_year, visa_class
"""

# Vintage hash: sha256 over a stable representation of the inputs that
# fed the aggregation. Two recomputes from the same DB state produce the
# same hash; if any underlying raw or ref row changes, the hash changes.
_QUERY_VINTAGE_HASH = """
SELECT
    'raw.lca_disclosure: '
    || coalesce(max(ingested_at::text), '<empty>')
    || ' / rows=' || count(*)
    || ' / sha_concat=' || coalesce(string_agg(source_sha256, ',' ORDER BY source_sha256), '')
FROM raw.lca_disclosure
"""

_QUERY_VINTAGE_HASH_HUD = """
SELECT
    'ref.zip_county: '
    || coalesce(max(ingested_at::text), '<empty>')
    || ' / rows=' || count(*)
    || ' / sha_concat='
    || coalesce(string_agg(distinct source_sha256, ',' ORDER BY source_sha256), '')
FROM ref.zip_county
"""


def _compute_input_vintage_hash(connection: psycopg.Connection) -> str:
    """SHA-256 of a deterministic summary of the raw + ref inputs.

    The summary captures: max ingested_at, row count, and concatenated
    sorted source_sha256 values for both ``raw.lca_disclosure`` and
    ``ref.zip_county``. This is sensitive to any data change but
    insensitive to ordering.
    """
    parts: list[str] = []
    for query in (_QUERY_VINTAGE_HASH, _QUERY_VINTAGE_HASH_HUD):
        cur = connection.execute(query)
        row = cur.fetchone()
        parts.append("" if row is None else str(row[0]))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_groups(
    observations: list[_ObservationRow],
    *,
    formula_version: str,
    input_vintage_hash: str,
) -> list[AggregateRow]:
    """Aggregate observation rows into ``AggregateRow`` per (county, FY, visa).

    Suppression: cells with fewer than :data:`SUPPRESSION_MIN_N` unweighted
    observations have all four percentile columns NULL-ed. The CHECK
    constraint on the destination table will reject any row that violates
    this contract; we honor it preemptively.
    """
    groups: dict[tuple[str, int, str], list[_ObservationRow]] = {}
    for obs in observations:
        groups.setdefault((obs.county_id, obs.fiscal_year, obs.visa_class), []).append(obs)

    out: list[AggregateRow] = []
    for (county_id, fy, visa), rows in groups.items():
        n = len(rows)
        n_certs_w = sum(r.bus_ratio for r in rows)
        n_workers_w = sum(
            (r.total_workers or 0.0) * r.bus_ratio for r in rows
        )

        if n >= SUPPRESSION_MIN_N:
            wage_values = [r.annualized_wage_from for r in rows]
            wage_weights = [r.bus_ratio for r in rows]
            pw_values = [r.annualized_pw for r in rows]

            median_wage = weighted_percentile(wage_values, wage_weights, 0.50)
            p25_wage = weighted_percentile(wage_values, wage_weights, 0.25)
            p75_wage = weighted_percentile(wage_values, wage_weights, 0.75)
            median_pw = weighted_percentile(pw_values, wage_weights, 0.50)
        else:
            median_wage = p25_wage = p75_wage = median_pw = None

        out.append(AggregateRow(
            county_id=county_id,
            fiscal_year=fy,
            visa_class=visa,
            n_unweighted_certs=n,
            n_certs_weighted=n_certs_w,
            n_workers_weighted=n_workers_w,
            median_annualized_wage_from=median_wage,
            p25_annualized_wage_from=p25_wage,
            p75_annualized_wage_from=p75_wage,
            median_prevailing_wage=median_pw,
            formula_version=formula_version,
            input_vintage_hash=input_vintage_hash,
        ))
    out.sort(key=lambda r: (r.county_id, r.fiscal_year, r.visa_class))
    return out


# ---------------------------------------------------------------------------
# Postgres I/O
# ---------------------------------------------------------------------------


def fetch_observations(connection: psycopg.Connection) -> list[_ObservationRow]:
    """Materialize the LCA-x-HUD join into Python rows."""
    cur = connection.execute(_QUERY_OBSERVATIONS)
    return [
        _ObservationRow(
            county_id=row[0],
            fiscal_year=int(row[1]),
            visa_class=row[2],
            bus_ratio=float(row[3]),
            total_workers=None if row[4] is None else float(row[4]),
            annualized_wage_from=None if row[5] is None else float(row[5]),
            annualized_pw=None if row[6] is None else float(row[6]),
        )
        for row in cur.fetchall()
    ]


def write_aggregate_rows(
    connection: psycopg.Connection,
    rows: list[AggregateRow],
) -> int:
    """INSERT (with conflict-do-nothing on (PK)) the AggregateRows.

    The destination PK is (county_id, fiscal_year, visa_class,
    formula_version, input_vintage_hash). Re-running with identical
    inputs produces identical hashes and no-ops via ON CONFLICT.
    """
    if not rows:
        return 0
    insert_sql = """
        INSERT INTO derived.lca_wage_by_county_yr_visa (
            county_id, fiscal_year, visa_class,
            n_unweighted_certs, n_certs_weighted, n_workers_weighted,
            median_annualized_wage_from, p25_annualized_wage_from,
            p75_annualized_wage_from, median_prevailing_wage,
            formula_version, input_vintage_hash, method, data_quality
        ) VALUES (
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, 'lca_bus_ratio_allocation', 'computed'
        )
        ON CONFLICT (county_id, fiscal_year, visa_class,
                     formula_version, input_vintage_hash) DO NOTHING
    """
    payload = [
        (
            r.county_id, r.fiscal_year, r.visa_class,
            r.n_unweighted_certs, r.n_certs_weighted, r.n_workers_weighted,
            r.median_annualized_wage_from, r.p25_annualized_wage_from,
            r.p75_annualized_wage_from, r.median_prevailing_wage,
            r.formula_version, r.input_vintage_hash,
        )
        for r in rows
    ]
    with connection.cursor() as cur:
        cur.executemany(insert_sql, payload)
        return cur.rowcount or 0


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


def run_aggregation(
    connection: psycopg.Connection,
    *,
    formula_version: str = DEFAULT_FORMULA_VERSION,
) -> tuple[int, str]:
    """End-to-end: fetch, aggregate, write. Returns (rows_written, vintage_hash)."""
    vintage_hash = _compute_input_vintage_hash(connection)
    observations = fetch_observations(connection)
    log.info("Fetched %d LCA-x-HUD observation rows.", len(observations))
    aggregated = aggregate_groups(
        observations,
        formula_version=formula_version,
        input_vintage_hash=vintage_hash,
    )
    log.info("Aggregated to %d (county, FY, visa) cells.", len(aggregated))
    n_written = write_aggregate_rows(connection, aggregated)
    return n_written, vintage_hash
