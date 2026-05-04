"""Query layer for the Tier 4 v2 fraud-detection metric API.

Each fraud signal is a derived view in Postgres (``derived.fec_*``) and
a static catalog entry here. The catalog tells the serving layer:

* which view backs the signal,
* which columns are sortable (whitelisted to defeat SQL injection via
  ORDER BY column names),
* which sort column to default to,
* and which entity-identity columns the UI should treat as drill-down
  links into the existing /fec/candidates/{id} or
  /fec/committees/{id} detail pages.

Why one module instead of a per-signal module
---------------------------------------------
Each metric is ~5 lines of metadata + an existing view. A registry is
cheaper than 8+ near-identical modules, and it makes the catalog
endpoint trivial: serialize the registry. Adding a metric is one entry
plus one migration -- no API/UI changes required for the 8/8 use case
where the new metric uses the same schema (severity_score + columns).

The Tier B (contribution) signals will use the same registry once
migration 041 lands; only the catalog list grows.

Cycle filtering
---------------
Every metric view exposes a ``cycle`` column. The query layer applies
``WHERE cycle = %s`` when a cycle filter is given. Otherwise the
metric is computed across all cycles present, and the row's own
cycle column is the disambiguator. This keeps the API symmetric with
``/fec/candidates`` etc. (cycle is always optional).

Pagination
----------
Same envelope as ``serving.queries_fec.list_*`` -- (rows, total_count)
tuple, with total_count computed as a separate COUNT(*) query rather
than via ``COUNT(*) OVER ()`` to avoid materializing the full result.

Sort safety
-----------
``sort_by`` is whitelisted PER SIGNAL in ``MetricSpec.sort_cols``.
An attacker passing ``sort_by="1; DROP TABLE foo"`` is rejected with
``KeyError`` before any SQL is composed. Defense in depth even though
``psycopg.sql.Identifier`` would also reject the literal at compose
time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from psycopg import sql
from psycopg.rows import dict_row

if TYPE_CHECKING:
    import psycopg

log = logging.getLogger(__name__)


# ============================================================================
# Pagination
# ============================================================================

DEFAULT_LIMIT: Final[int] = 100
MAX_LIMIT:     Final[int] = 1000
MAX_EXPORT:    Final[int] = 100_000


def _clamp_limit(limit: int | None, *, hard_cap: int = MAX_LIMIT) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), hard_cap))


def _clamp_offset(offset: int | None) -> int:
    return 0 if offset is None or offset < 0 else int(offset)


def _resolve_sort_dir(sort_dir: str | None) -> sql.SQL:
    direction = (sort_dir or "DESC").upper()
    if direction not in {"ASC", "DESC"}:
        direction = "DESC"
    return sql.SQL(direction)


# ============================================================================
# Catalog
# ============================================================================

@dataclass(frozen=True)
class MetricSpec:
    """Static metadata for one fraud-detection metric.

    A spec is the catalog entry plus the SQL-side info the route layer
    needs (view name, sortable columns, default sort). Splitting these
    out from the public Pydantic model avoids leaking SQL identifiers
    over the API.
    """

    id:               str
    name:             str
    tier:             str        # "structural" | "contribution"
    view:             str        # qualified view name, e.g. "derived.fec_xxx"
    description:      str
    threshold_note:   str | None
    sort_cols:        frozenset[str]
    sort_default:     str
    primary_key_cols: tuple[str, ...]
    select_cols:      tuple[str, ...] = field(default_factory=tuple)


# Why a hand-built dict instead of decorator-driven discovery:
# the metric set is small (8 today, ~20 long term) and the registry
# is the canonical source of truth -- a flat literal is the most
# review-friendly form.
_METRIC_CATALOG: Final[dict[str, MetricSpec]] = {
    "treasurer_concentration": MetricSpec(
        id="treasurer_concentration",
        name="Treasurer concentration",
        tier="structural",
        view="derived.fec_treasurer_concentration",
        description=(
            "Treasurers (the human signing off on a committee's filings) "
            "ranked by the number of distinct committees they manage in "
            "one cycle. Professional treasurer firms legitimately handle "
            "5-15 committees; concentrations above ~15 are leads for "
            "shell-network triage."
        ),
        threshold_note=(
            "n_committees >= 15: lead. n_committees in [5, 14]: likely "
            "professional treasurer. n_committees < 5: routine."
        ),
        sort_cols=frozenset({
            "n_committees", "n_states", "severity_score",
            "tres_nm_canonical", "cycle",
        }),
        sort_default="n_committees",
        primary_key_cols=("tres_nm_canonical",),
        select_cols=(
            "cycle", "tres_nm_canonical", "n_committees", "n_states",
            "severity_score", "committee_ids", "parties_seen",
        ),
    ),

    "candidate_no_pcc": MetricSpec(
        id="candidate_no_pcc",
        name="Candidate without Principal Campaign Committee",
        tier="structural",
        view="derived.fec_candidate_no_pcc",
        description=(
            "Candidates who have not declared a Principal Campaign "
            "Committee. Required for candidates who cross the $5k "
            "raise/spend threshold. Below-threshold candidates are "
            "legitimately here; persistently active (cand_status='C') "
            "candidates with no PCC are the leads."
        ),
        threshold_note=(
            "Filter cand_status='C' (active) and cand_office in "
            "('S','H','P') for highest-signal subset."
        ),
        sort_cols=frozenset({
            "cand_id", "cand_name", "cycle",
            "cand_office_st", "cand_office", "cand_status",
        }),
        sort_default="cand_office_st",
        primary_key_cols=("cand_id",),
        select_cols=(
            "cycle", "cand_id", "cand_name", "cand_pty_affiliation",
            "cand_office", "cand_office_st", "cand_office_district",
            "cand_status", "cand_ici", "severity_score",
        ),
    ),

    "candidate_broken_pcc": MetricSpec(
        id="candidate_broken_pcc",
        name="Candidate references missing committee",
        tier="structural",
        view="derived.fec_candidate_broken_pcc",
        description=(
            "Candidates whose declared PCC ID does not exist in "
            "raw.fec_committee for the same cycle. Referential gap; "
            "expected near-zero. Persistent non-zero counts indicate "
            "stale FEC data or ingestion drift."
        ),
        threshold_note="Any non-zero count is worth a triage pass.",
        sort_cols=frozenset({
            "cand_id", "cand_name", "missing_cmte_id",
            "cand_office_st", "cycle",
        }),
        sort_default="cand_office_st",
        primary_key_cols=("cand_id",),
        select_cols=(
            "cycle", "cand_id", "cand_name", "cand_office_st",
            "missing_cmte_id", "cand_status", "severity_score",
        ),
    ),

    "candidate_multiple_pccs": MetricSpec(
        id="candidate_multiple_pccs",
        name="Candidate with multiple PCCs",
        tier="structural",
        view="derived.fec_candidate_multiple_pccs",
        description=(
            "Candidates linked to multiple committees with cmte_dsgn='P' "
            "in one cycle. Regulation says exactly 1; >1 indicates a "
            "successor-committee transition, a filing error, or a race "
            "condition in FEC ingestion."
        ),
        threshold_note=(
            "n_pccs == 2: usually successor transition. n_pccs >= 3: "
            "filing anomaly worth manual review."
        ),
        sort_cols=frozenset({
            "n_pccs", "severity_score", "cand_id", "cand_name", "cycle",
        }),
        sort_default="n_pccs",
        primary_key_cols=("cand_id",),
        select_cols=(
            "cycle", "cand_id", "cand_name", "cand_office_st",
            "cand_office", "n_pccs", "severity_score",
            "pcc_ids", "pcc_names",
        ),
    ),

    "committee_address_clusters": MetricSpec(
        id="committee_address_clusters",
        name="Committee address clusters",
        tier="structural",
        view="derived.fec_committee_address_clusters",
        description=(
            "Street addresses hosting >= 3 distinct committees in one "
            "cycle. Joint campaign HQs are legitimate; PO boxes hosting "
            "dozens of committees are shell-network signatures."
        ),
        threshold_note=(
            "n_committees >= 30 and address starts with 'PO BOX': "
            "high-priority lead."
        ),
        sort_cols=frozenset({
            "n_committees", "severity_score", "address_canonical",
            "city_canonical", "state", "zip_canonical", "cycle",
        }),
        sort_default="n_committees",
        primary_key_cols=("address_canonical", "zip_canonical"),
        select_cols=(
            "cycle", "address_canonical", "city_canonical", "state",
            "zip_canonical", "n_committees", "severity_score",
            "committee_ids", "parties_seen",
        ),
    ),

    "committee_name_collisions": MetricSpec(
        id="committee_name_collisions",
        name="Committee name collisions",
        tier="structural",
        view="derived.fec_committee_name_collisions",
        description=(
            "Different committee IDs registered under the same canonical "
            "name within one cycle. Causes name confusion in donor "
            "tracking systems and may indicate near-duplicate filings."
        ),
        threshold_note=(
            "n_committee_ids >= 3: investigate for filing duplicates. "
            "Cross-state collisions are usually unrelated organizations."
        ),
        sort_cols=frozenset({
            "n_committee_ids", "severity_score", "cmte_nm_canonical", "cycle",
        }),
        sort_default="n_committee_ids",
        primary_key_cols=("cmte_nm_canonical",),
        select_cols=(
            "cycle", "cmte_nm_canonical", "n_committee_ids",
            "severity_score", "committee_ids", "states_seen",
        ),
    ),

    "candidate_namesakes": MetricSpec(
        id="candidate_namesakes",
        name="Candidate name collisions (within cycle)",
        tier="structural",
        view="derived.fec_candidate_namesakes",
        description=(
            "Same canonical candidate name registered under multiple "
            "cand_ids in the same cycle, state, and office. Same-state "
            "same-office namesakes are leads for duplicate-filing or "
            "impersonation review; cross-district namesakes are usually "
            "coincidental."
        ),
        threshold_note=(
            "n_cand_ids >= 2 and same office: investigate. House "
            "candidates with common names are usually unrelated."
        ),
        sort_cols=frozenset({
            "n_cand_ids", "severity_score", "cand_name_canonical",
            "cand_office_st", "cand_office", "cycle",
        }),
        sort_default="n_cand_ids",
        primary_key_cols=("cand_name_canonical", "cand_office_st"),
        select_cols=(
            "cycle", "cand_name_canonical", "cand_office_st",
            "cand_office", "n_cand_ids", "severity_score",
            "candidate_ids", "parties_seen",
        ),
    ),

    "treasurer_is_candidate": MetricSpec(
        id="treasurer_is_candidate",
        name="Treasurer is the candidate",
        tier="structural",
        view="derived.fec_treasurer_is_candidate",
        description=(
            "Committees whose listed treasurer name matches the linked "
            "candidate's name (canonical match). Self-treasurer is "
            "common for small local campaigns but worth surfacing -- "
            "it collapses the audit chain (no independent oversight)."
        ),
        threshold_note=(
            "Filter cmte_dsgn='P' (PCC) and cmte_tp='S'/'H'/'P' for "
            "federal-office self-treasurer subset."
        ),
        sort_cols=frozenset({
            "cycle", "cand_id", "cmte_id", "committee_name",
            "treasurer_name", "cand_office_st", "cand_office",
        }),
        sort_default="cand_office_st",
        primary_key_cols=("cmte_id", "cand_id"),
        select_cols=(
            "cycle", "cmte_id", "committee_name", "treasurer_name",
            "cmte_dsgn", "cmte_tp", "cmte_st",
            "cand_id", "cand_name", "cand_office", "cand_office_st",
            "severity_score",
        ),
    ),
}


def get_catalog() -> list[MetricSpec]:
    """Return all registered metrics, ordered by tier then id (stable)."""
    # Stable ordering matters: the UI's signal selector uses this order,
    # and analysts memorize positions. Tier first (structural before
    # contribution), then alphabetical id within tier.
    return sorted(
        _METRIC_CATALOG.values(),
        key=lambda m: (0 if m.tier == "structural" else 1, m.id),
    )


def get_metric(metric_id: str) -> MetricSpec:
    """Look up a metric by id; raise KeyError if not registered."""
    if metric_id not in _METRIC_CATALOG:
        raise KeyError(f"Unknown fraud metric: {metric_id!r}")
    return _METRIC_CATALOG[metric_id]


# ============================================================================
# Summary endpoint helpers
# ============================================================================

def metric_counts(
    conn: psycopg.Connection,
    *,
    cycle: str | None = None,
) -> dict[str, int]:
    """Return total flagged-row counts for every registered metric.

    Issued as N small COUNT queries rather than one giant UNION ALL.
    Each view is independently planned and answered in single-digit ms;
    the round-trip cost dominates and is bounded at ~8 queries today.
    Overall the endpoint settles in ~50 ms regardless of catalog size.
    If the catalog grows past ~30 metrics we'll switch to a single
    UNION ALL with COUNT(*) per view.
    """
    out: dict[str, int] = {}
    for metric in _METRIC_CATALOG.values():
        view = sql.Identifier(*metric.view.split("."))
        if cycle:
            query = sql.SQL("SELECT COUNT(*) FROM {v} WHERE cycle = %s").format(v=view)
            args: tuple[Any, ...] = (cycle,)
        else:
            query = sql.SQL("SELECT COUNT(*) FROM {v}").format(v=view)
            args = ()
        with conn.cursor() as cur:
            cur.execute(query, args)
            row = cur.fetchone()
            out[metric.id] = int(row[0]) if row else 0
    return out


# ============================================================================
# Per-metric paginated list
# ============================================================================

def list_metric(
    conn: psycopg.Connection,
    *,
    metric_id: str,
    cycle: str | None,
    sort_by: str | None,
    sort_dir: str | None,
    limit: int | None,
    offset: int | None,
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, total_count) for one metric.

    All filtering knobs except ``cycle`` live in the metric view itself
    (it has already encoded the predicate that defines "flagged"). The
    serving layer only adds cycle scoping + sort + pagination.
    """
    metric = get_metric(metric_id)

    # Whitelist sort column. A KeyError here is a 400 in the route.
    requested_sort = sort_by or metric.sort_default
    if requested_sort not in metric.sort_cols:
        raise KeyError(
            f"Sort column {requested_sort!r} not allowed for metric "
            f"{metric_id!r}. Allowed: {sorted(metric.sort_cols)}",
        )

    sort_col       = sql.Identifier(requested_sort)
    sort_direction = _resolve_sort_dir(sort_dir)
    n_limit        = _clamp_limit(limit)
    n_offset       = _clamp_offset(offset)
    view_ident     = sql.Identifier(*metric.view.split("."))

    cols_sql = (
        sql.SQL(", ").join(sql.Identifier(c) for c in metric.select_cols)
        if metric.select_cols
        else sql.SQL("*")
    )

    if cycle:
        where_sql = sql.SQL("WHERE cycle = %s")
        args: list[Any] = [cycle]
    else:
        where_sql = sql.SQL("")
        args = []

    list_query = sql.SQL(
        "SELECT {cols} FROM {v} {where} "
        "ORDER BY {sort_col} {sort_dir} NULLS LAST "
        "LIMIT %s OFFSET %s",
    ).format(
        cols=cols_sql, v=view_ident, where=where_sql,
        sort_col=sort_col, sort_dir=sort_direction,
    )
    count_query = sql.SQL("SELECT COUNT(*) FROM {v} {where}").format(
        v=view_ident, where=where_sql,
    )

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(list_query, [*args, n_limit, n_offset])
        rows = list(cur.fetchall())
    with conn.cursor() as cur:
        cur.execute(count_query, args)
        row = cur.fetchone()
        total = int(row[0]) if row else 0
    return rows, total


# ============================================================================
# CSV streaming for a metric
# ============================================================================

def stream_metric(
    conn: psycopg.Connection,
    *,
    metric_id: str,
    cycle: str | None,
    sort_by: str | None,
    sort_dir: str | None,
    cap: int = MAX_EXPORT,
) -> tuple[list[str], Any]:
    """Yield (header_columns, server-side cursor) for streaming a metric.

    The cursor MUST be consumed and closed by the caller (the route
    layer wraps it in a ``with`` block via ``StreamingResponse``).
    """
    metric = get_metric(metric_id)
    requested_sort = sort_by or metric.sort_default
    if requested_sort not in metric.sort_cols:
        raise KeyError(
            f"Sort column {requested_sort!r} not allowed for metric "
            f"{metric_id!r}. Allowed: {sorted(metric.sort_cols)}",
        )

    sort_col       = sql.Identifier(requested_sort)
    sort_direction = _resolve_sort_dir(sort_dir)
    view_ident     = sql.Identifier(*metric.view.split("."))
    cols_list      = list(metric.select_cols) if metric.select_cols else []
    cols_sql = (
        sql.SQL(", ").join(sql.Identifier(c) for c in cols_list)
        if cols_list
        else sql.SQL("*")
    )

    if cycle:
        where_sql = sql.SQL("WHERE cycle = %s")
        args: list[Any] = [cycle]
    else:
        where_sql = sql.SQL("")
        args = []

    query = sql.SQL(
        "SELECT {cols} FROM {v} {where} "
        "ORDER BY {sort_col} {sort_dir} NULLS LAST "
        "LIMIT %s",
    ).format(
        cols=cols_sql, v=view_ident, where=where_sql,
        sort_col=sort_col, sort_dir=sort_direction,
    )

    cur = conn.cursor(name=f"export_metric_{metric_id}")
    cur.itersize = 5000
    cur.execute(query, [*args, cap])
    return cols_list, cur
