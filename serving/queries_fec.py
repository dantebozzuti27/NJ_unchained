"""SQL query layer for the FEC (civic-integrity) read API.

Lives in its own module rather than ``serving/queries.py`` because the
FEC surface area is large (4 tables, 5 views, ~6 filterable predicates
per table) and self-contained: nothing else in the platform reads from
``raw.fec_*`` or ``public.v_fec_*``.

Design notes
------------

1. EVERY FILTER IS AN OPTIONAL KEYWORD ARGUMENT. A None means "no
   constraint on this column". The SQL builder skips the predicate
   entirely so missing filters do not change query plans (a WHERE
   col = NULL would be falsy in three-valued logic and silently
   return zero rows, which would be a horrible UX trap).

2. PARAMETERIZED EVERYTHING. We never f-string a user-supplied value
   into SQL. ``psycopg.sql.SQL`` composes the final query with
   ``Identifier``s for column / table names where dynamic identifiers
   are unavoidable (sort column), and parameter placeholders for all
   values.

3. SORT COLUMN IS WHITELISTED. The sort_by parameter must be one of
   the explicitly-allowed columns per endpoint. An attacker passing
   ``sort_by='1; DROP TABLE'`` is rejected with KeyError before the
   identifier is composed -- defense in depth even though psycopg's
   Identifier would also reject the literal.

4. SAME (LIST, COUNT) PATTERN EVERYWHERE. Each list_X function
   returns a tuple ``(rows, total_count)`` where total_count is the
   COUNT(*) of the same WHERE clause without LIMIT/OFFSET. The UI
   needs total_count to render pagination controls.

5. TOTAL_COUNT IS A SECOND ROUND TRIP. We could fold it into the
   list query with a window function (``COUNT(*) OVER ()``) but that
   forces the planner to materialize the full result set even for a
   small page. Two queries with the same WHERE clause are cheaper
   under a sane index plan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from psycopg import sql
from psycopg.rows import dict_row

if TYPE_CHECKING:
    import psycopg

log = logging.getLogger(__name__)


# ============================================================================
# Pagination defaults
# ============================================================================

DEFAULT_LIMIT: Final[int] = 100
MAX_LIMIT:     Final[int] = 1000   # for JSON list endpoints
MAX_EXPORT:    Final[int] = 100_000  # for CSV streaming endpoints


def _clamp_limit(limit: int | None, *, hard_cap: int = MAX_LIMIT) -> int:
    """Return *limit* clamped to [1, hard_cap], defaulting to DEFAULT_LIMIT."""
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), hard_cap))


def _clamp_offset(offset: int | None) -> int:
    """Return *offset* clamped to >= 0."""
    return 0 if offset is None or offset < 0 else int(offset)


# ============================================================================
# Filter spec dataclasses
# ============================================================================
#
# A filter spec is a typed bag of optional predicates. Routes build one
# from query parameters; query functions consume it. This indirection
# lets us add a new predicate without touching every list function and
# also makes it trivial to share a spec between the JSON list endpoint
# and the CSV export endpoint.
# ============================================================================


@dataclass(frozen=True)
class CandidateFilters:
    """Optional filters for raw.fec_candidate queries."""

    cycle:           str | None = None
    state:           str | None = None  # cand_office_st
    office:          str | None = None  # H/S/P
    party:           str | None = None  # cand_pty_affiliation
    incumbent:       str | None = None  # cand_ici (I/C/O)
    status:          str | None = None  # cand_status (C/F/N/P/W)
    name_contains:   str | None = None  # ILIKE on cand_name


@dataclass(frozen=True)
class CommitteeFilters:
    """Optional filters for raw.fec_committee queries."""

    cycle:           str | None = None
    state:           str | None = None  # cmte_st (where committee is domiciled)
    cmte_type:       str | None = None  # cmte_tp
    designation:     str | None = None  # cmte_dsgn
    party:           str | None = None  # cmte_pty_affiliation
    org_type:        str | None = None  # org_tp
    name_contains:   str | None = None
    has_candidate:   bool | None = None  # cand_id IS NOT NULL


@dataclass(frozen=True)
class ContributionFilters:
    """Optional filters for raw.fec_contribution queries.

    Most filters apply to columns that are TEXT in raw; date and amount
    filters operate against the cooked public.v_fec_contribution view
    (transaction_date as DATE, transaction_amt as NUMERIC) so the
    callers do not need to know which raw layer the predicate lives on.
    """

    cycle:                  str | None = None
    cmte_id:                str | None = None
    donor_state:            str | None = None  # state column
    donor_name_contains:    str | None = None
    employer_contains:      str | None = None
    occupation_contains:    str | None = None
    transaction_type:       str | None = None
    min_amount:             float | None = None
    max_amount:             float | None = None
    start_date:             str | None = None  # YYYY-MM-DD
    end_date:               str | None = None  # YYYY-MM-DD
    exclude_memo:           bool = True        # default: hide MEMO_CD='X' rows


@dataclass(frozen=True)
class MoneyToNjFilters:
    """Optional filters for public.v_fec_money_to_nj_candidates queries."""

    cycle:                  str | None = None
    cand_id:                str | None = None
    party:                  str | None = None  # cand_pty_affiliation
    office:                 str | None = None  # cand_office
    donor_state:            str | None = None
    donor_name_contains:    str | None = None
    min_amount:             float | None = None
    max_amount:             float | None = None
    start_date:             str | None = None
    end_date:               str | None = None
    exclude_memo:           bool = True


# ============================================================================
# WHERE-clause builders
# ============================================================================


def _add_eq(
    parts: list[sql.Composable], args: list[Any], col: str, val: Any,
) -> None:
    """Append a `col = %s` predicate iff *val* is non-empty."""
    if val is None or val == "":
        return
    parts.append(sql.SQL("{c} = %s").format(c=sql.Identifier(col)))
    args.append(val)


def _add_ilike(
    parts: list[sql.Composable], args: list[Any], col: str, val: str | None,
) -> None:
    """Append a `col ILIKE %s` predicate iff *val* is non-empty."""
    if not val:
        return
    parts.append(sql.SQL("{c} ILIKE %s").format(c=sql.Identifier(col)))
    args.append(f"%{val}%")


def _add_cmp(
    parts: list[sql.Composable],
    args: list[Any],
    col: str,
    op: str,
    val: float | str | None,
) -> None:
    """Append a `col <op> %s` predicate iff *val* is not None.

    *op* is one of '>=', '<=', '>', '<'; never user-supplied.
    """
    if val is None or val == "":
        return
    parts.append(sql.SQL("{c} " + op + " %s").format(c=sql.Identifier(col)))
    args.append(val)


def _finalize_where(parts: list[sql.Composable]) -> sql.Composable:
    """Join *parts* into a single WHERE clause (or empty SQL if none)."""
    if not parts:
        return sql.SQL("")
    return sql.SQL("WHERE ") + sql.SQL(" AND ").join(parts)


def _build_candidate_where(
    f: CandidateFilters,
) -> tuple[sql.Composable, list[Any]]:
    parts: list[sql.Composable] = []
    args:  list[Any] = []
    _add_eq(parts, args, "cycle",                f.cycle)
    _add_eq(parts, args, "cand_office_st",       f.state)
    _add_eq(parts, args, "cand_office",          f.office)
    _add_eq(parts, args, "cand_pty_affiliation", f.party)
    _add_eq(parts, args, "cand_ici",             f.incumbent)
    _add_eq(parts, args, "cand_status",          f.status)
    _add_ilike(parts, args, "cand_name",         f.name_contains)
    return _finalize_where(parts), args


def _build_committee_where(
    f: CommitteeFilters,
) -> tuple[sql.Composable, list[Any]]:
    parts: list[sql.Composable] = []
    args:  list[Any] = []
    _add_eq(parts, args, "cycle",                f.cycle)
    _add_eq(parts, args, "cmte_st",              f.state)
    _add_eq(parts, args, "cmte_tp",              f.cmte_type)
    _add_eq(parts, args, "cmte_dsgn",            f.designation)
    _add_eq(parts, args, "cmte_pty_affiliation", f.party)
    _add_eq(parts, args, "org_tp",               f.org_type)
    _add_ilike(parts, args, "cmte_nm",           f.name_contains)
    if f.has_candidate is True:
        parts.append(sql.SQL("cand_id IS NOT NULL"))
    elif f.has_candidate is False:
        parts.append(sql.SQL("cand_id IS NULL"))
    return _finalize_where(parts), args


def _build_contribution_where(
    f: ContributionFilters,
) -> tuple[sql.Composable, list[Any]]:
    """Build the WHERE clause against public.v_fec_contribution.

    The cooked view exposes parsed transaction_date (DATE) and
    is_memo (BOOL); the raw table only has MMDDYYYY string + memo_cd.
    Querying against the view keeps every predicate's type honest.
    """
    parts: list[sql.Composable] = []
    args:  list[Any] = []
    _add_eq(parts, args, "cycle",              f.cycle)
    _add_eq(parts, args, "cmte_id",            f.cmte_id)
    _add_eq(parts, args, "contributor_state",  f.donor_state)
    _add_eq(parts, args, "transaction_type",   f.transaction_type)
    _add_ilike(parts, args, "contributor_name",       f.donor_name_contains)
    _add_ilike(parts, args, "contributor_employer",   f.employer_contains)
    _add_ilike(parts, args, "contributor_occupation", f.occupation_contains)
    _add_cmp(parts, args, "transaction_amount", ">=", f.min_amount)
    _add_cmp(parts, args, "transaction_amount", "<=", f.max_amount)
    _add_cmp(parts, args, "transaction_date",   ">=", f.start_date)
    _add_cmp(parts, args, "transaction_date",   "<=", f.end_date)
    if f.exclude_memo:
        parts.append(sql.SQL("NOT is_memo"))
    return _finalize_where(parts), args


def _build_money_to_nj_where(
    f: MoneyToNjFilters,
) -> tuple[sql.Composable, list[Any]]:
    parts: list[sql.Composable] = []
    args:  list[Any] = []
    _add_eq(parts, args, "cycle",                f.cycle)
    _add_eq(parts, args, "cand_id",              f.cand_id)
    _add_eq(parts, args, "cand_pty_affiliation", f.party)
    _add_eq(parts, args, "cand_office",          f.office)
    _add_eq(parts, args, "contributor_state",    f.donor_state)
    _add_ilike(parts, args, "contributor_name",  f.donor_name_contains)
    _add_cmp(parts, args, "transaction_amount", ">=", f.min_amount)
    _add_cmp(parts, args, "transaction_amount", "<=", f.max_amount)
    _add_cmp(parts, args, "transaction_date",   ">=", f.start_date)
    _add_cmp(parts, args, "transaction_date",   "<=", f.end_date)
    if f.exclude_memo:
        parts.append(sql.SQL("NOT is_memo"))
    return _finalize_where(parts), args


# ============================================================================
# Sort whitelists
# ============================================================================
#
# Per-endpoint whitelist of sortable columns. The route layer translates
# the request's sort_by string to one of these via dict lookup; an
# unrecognized value raises KeyError, which the route maps to 400.
# ============================================================================

_CANDIDATE_SORT_COLS: Final[dict[str, str]] = {
    "cand_name":            "cand_name",
    "cand_id":              "cand_id",
    "cand_office":          "cand_office",
    "cand_office_st":       "cand_office_st",
    "cand_pty_affiliation": "cand_pty_affiliation",
    "cycle":                "cycle",
}

_COMMITTEE_SORT_COLS: Final[dict[str, str]] = {
    "cmte_id":              "cmte_id",
    "cmte_nm":              "cmte_nm",
    "cmte_st":              "cmte_st",
    "cmte_tp":              "cmte_tp",
    "cmte_dsgn":            "cmte_dsgn",
    "cycle":                "cycle",
}

_CONTRIBUTION_SORT_COLS: Final[dict[str, str]] = {
    "transaction_date":     "transaction_date",
    "transaction_amount":   "transaction_amount",
    "contributor_name":     "contributor_name",
    "contributor_state":    "contributor_state",
    "cmte_id":              "cmte_id",
    "sub_id":               "sub_id",
}

_MONEY_TO_NJ_SORT_COLS: Final[dict[str, str]] = {
    "transaction_date":     "transaction_date",
    "transaction_amount":   "transaction_amount",
    "cand_name":            "cand_name",
    "contributor_name":     "contributor_name",
    "contributor_state":    "contributor_state",
    "cycle":                "cycle",
}


def _resolve_sort(
    whitelist: dict[str, str], sort_by: str | None, sort_dir: str | None,
) -> tuple[sql.Composable, sql.Composable]:
    """Return (ORDER BY identifier, direction SQL).

    Defaults to the FIRST column in the whitelist (so each endpoint
    declares its preferred sort by ordering its dict). Direction is
    capped to ASC|DESC; anything else falls back to DESC for amount/
    date-style sorts and ASC for ID/name-style.
    """
    column = whitelist[sort_by] if sort_by in whitelist else next(iter(whitelist.values()))
    dir_norm = (sort_dir or "").upper()
    direction_sql = sql.SQL("DESC") if dir_norm == "DESC" else sql.SQL("ASC")
    return sql.Identifier(column), direction_sql


# ============================================================================
# /fec/cycles, /fec/states, /fec/parties, /fec/offices
# ============================================================================


def list_distinct_cycles(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """All cycles present in raw.fec_candidate (descending)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT cycle AS value, COUNT(*) AS count "
            "FROM   raw.fec_candidate "
            "GROUP  BY cycle "
            "ORDER  BY cycle DESC"
        )
        return list(cur.fetchall())


def _run_distinct_query(
    conn: psycopg.Connection, *, column: str, cycle: str | None,
) -> list[dict[str, Any]]:
    """Run the canonical 'distinct values + counts' query for *column*.

    Wrapped as a single function (rather than a builder + caller) so
    psycopg's execute() type-overloads see a concrete tuple at the
    call site -- otherwise mypy fails on the Composable/list[Any]
    variance combination.
    """
    args: tuple[Any, ...] = (cycle,) if cycle else ()
    extra = sql.SQL(" AND cycle = %s") if cycle else sql.SQL("")
    query = sql.SQL(
        "SELECT {col} AS value, COUNT(*) AS count "
        "FROM   raw.fec_candidate "
        "WHERE  {col} IS NOT NULL AND {col} <> ''{extra} "
        "GROUP  BY {col} "
        "ORDER  BY count DESC, {col}",
    ).format(col=sql.Identifier(column), extra=extra)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, args)
        return list(cur.fetchall())


def list_distinct_states(
    conn: psycopg.Connection, *, cycle: str | None = None,
) -> list[dict[str, Any]]:
    """Distinct cand_office_st values, optionally scoped to a cycle."""
    return _run_distinct_query(conn, column="cand_office_st", cycle=cycle)


def list_distinct_parties(
    conn: psycopg.Connection, *, cycle: str | None = None,
) -> list[dict[str, Any]]:
    """Distinct cand_pty_affiliation values."""
    return _run_distinct_query(conn, column="cand_pty_affiliation", cycle=cycle)


def list_distinct_offices(
    conn: psycopg.Connection, *, cycle: str | None = None,
) -> list[dict[str, Any]]:
    """Distinct cand_office values (H/S/P)."""
    return _run_distinct_query(conn, column="cand_office", cycle=cycle)


# ============================================================================
# /fec/summary
# ============================================================================


# In-process TTL cache for the summary payload. Justification:
#
#   /fec/summary is the dashboard header. With raw.fec_contribution at
#   ~58M rows it is dominated by two slow predicates:
#     * COUNT(*) on the full table (no index helps; ~15s seq scan)
#     * COUNT(*) WHERE state='NJ' AND cycle=... (~26s index-only scan
#       until the heap gets vacuumed, which currently can't run because
#       PG's parallel maintenance workers exhaust /dev/shm on this box).
#
#   Live recomputation per page-load is unacceptable. Materializing a
#   refreshable snapshot table is the proper long-term answer (Tier 4
#   v3 will land it via a Dagster asset). For now we compute the answer
#   ONCE per process per ~5 minutes and serve it from memory.
#
#   The 5-minute TTL is a deliberate trade: the answer changes only on
#   raw.fec_* loads (rare, scheduled), so 5 minutes of staleness is
#   imperceptible. We surface freshness via a 'computed_at' field on
#   the response so debug clients can verify cache age.
_SUMMARY_CACHE: dict[str, Any] = {"value": None, "expires_at": 0.0}
_SUMMARY_TTL_S: Final[float] = 300.0


def clear_summary_cache() -> None:
    """Invalidate the in-process summary cache.

    Called by tests that mutate raw.fec_* between requests against the
    same Python process; in production the TTL alone is sufficient and
    nothing needs to call this.
    """
    _SUMMARY_CACHE["value"] = None
    _SUMMARY_CACHE["expires_at"] = 0.0


def _empty_summary() -> dict[str, Any]:
    return {
        "cycle":                          "",
        "candidates_total":               0,
        "candidates_nj":                  0,
        "committees_total":               0,
        "committees_nj_domiciled":        0,
        "contributions_total":            0,
        "contributions_nj_donor":         0,
        "contributions_to_nj_candidates": 0,
        "cycles_available":               [],
    }


def get_summary(conn: psycopg.Connection) -> dict[str, Any]:
    """Snapshot for the fraud-UI dashboard header.

    Returns cross-table counts scoped to the most recent cycle, served
    from a process-local 5-minute cache. Returns zeros + empty cycle
    if no FEC data is loaded yet so the UI can render a clean empty
    state without the cache populating.

    Performance contract
    --------------------
    * Cache hit: < 1 ms (dict lookup).
    * Cache miss with empty raw.fec_*: < 50 ms (small COUNTs).
    * Cache miss with 50M+ contributions: 5-30s (one-shot, then
      cached). The unrestricted total uses pg_class.reltuples
      (an analyzer estimate, accurate to within fractions of a
      percent post-ANALYZE) instead of an exact COUNT(*); the
      NJ-scoped count remains exact because it touches a fraction
      of the table via the (state, cycle) index.
    """
    import time as _time
    now = _time.time()
    cached = _SUMMARY_CACHE["value"]
    if cached is not None and _SUMMARY_CACHE["expires_at"] > now:
        # mypy: cached is Any out of dict[str, Any]; we know it's a dict by construction.
        assert isinstance(cached, dict)
        return cached

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT COALESCE(MAX(cycle), '') AS cycle FROM raw.fec_candidate")
        row = cur.fetchone()
        cycle = (row["cycle"] if row else "") or ""

    if not cycle:
        out = _empty_summary()
        _SUMMARY_CACHE["value"] = out
        _SUMMARY_CACHE["expires_at"] = now + _SUMMARY_TTL_S
        return out

    # Small-table counts (cn/cm; ~10K + ~21K rows) are cheap; do them
    # in one round-trip via a single SELECT with multiple subqueries.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM raw.fec_candidate WHERE cycle = %s)
                    AS candidates_total,
                (SELECT COUNT(*) FROM raw.fec_candidate
                  WHERE cycle = %s AND cand_office_st = 'NJ')
                    AS candidates_nj,
                (SELECT COUNT(*) FROM raw.fec_committee WHERE cycle = %s)
                    AS committees_total,
                (SELECT COUNT(*) FROM raw.fec_committee
                  WHERE cycle = %s AND cmte_st = 'NJ')
                    AS committees_nj_domiciled
            """,
            (cycle, cycle, cycle, cycle),
        )
        small_counts = cur.fetchone() or {}

    # contributions_total: planner estimate via pg_class.reltuples
    # for production-sized tables; exact COUNT for small tables and as
    # a fallback when ANALYZE has never run (reltuples sentinel = -1).
    #
    # Why the threshold and fallback:
    #   * On a freshly-loaded test DB with 5 synthetic rows, reltuples
    #     is -1 (sentinel). Exact COUNT is the right answer there.
    #   * On the production-scale table (~58M rows), exact COUNT does
    #     a 15s seq scan that we can't afford on every cache miss; the
    #     planner estimate is accurate to <1% and matches what every
    #     other query in the system already uses.
    #
    # The 1M cutoff is generous: anything below that does an exact
    # count in <1s; anything above is dominated by the estimate's
    # speed advantage.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(reltuples::bigint, -1) "
            "FROM pg_class WHERE relname='fec_contribution'",
        )
        rt = cur.fetchone()
        reltuples = int(rt[0]) if rt else -1
        if reltuples < 0 or reltuples < 1_000_000:
            cur.execute("SELECT COUNT(*) FROM raw.fec_contribution")
            cnt_row = cur.fetchone()
            contributions_total = int(cnt_row[0]) if cnt_row else 0
        else:
            contributions_total = reltuples

    # contributions_nj_donor: hits the (state, cycle) index; selective
    # enough to be tolerable (~3% of the table, 1-30s depending on
    # vacuum state). Caching makes the cost a one-shot per 5 min.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM raw.fec_contribution "
            "WHERE cycle = %s AND state = 'NJ'",
            (cycle,),
        )
        rn = cur.fetchone()
        contributions_nj_donor = int(rn[0]) if rn else 0

    # money-to-NJ view: small (~120K rows even on a presidential cycle)
    # because it joins through fec_committee.cand_id; fast.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM public.v_fec_money_to_nj_candidates "
            "WHERE cycle = %s",
            (cycle,),
        )
        rm = cur.fetchone()
        contributions_to_nj_candidates = int(rm[0]) if rm else 0

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT DISTINCT cycle FROM raw.fec_candidate ORDER BY cycle DESC",
        )
        cycles = [r["cycle"] for r in cur.fetchall()]

    out = {
        "cycle":                          cycle,
        "cycles_available":               cycles,
        "contributions_total":            contributions_total,
        "contributions_nj_donor":         contributions_nj_donor,
        "contributions_to_nj_candidates": contributions_to_nj_candidates,
        **{k: int(v or 0) for k, v in small_counts.items()},
    }
    _SUMMARY_CACHE["value"] = out
    _SUMMARY_CACHE["expires_at"] = now + _SUMMARY_TTL_S
    return out


# ============================================================================
# /fec/candidates
# ============================================================================

_CANDIDATE_SELECT = sql.SQL(
    "cycle, cand_id, cand_name, cand_pty_affiliation, "
    "cand_office, cand_office_st, cand_office_district, "
    "cand_ici, cand_status, cand_pcc"
)


def list_candidates(
    conn: psycopg.Connection,
    *,
    f: CandidateFilters,
    sort_by: str | None,
    sort_dir: str | None,
    limit: int | None,
    offset: int | None,
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, total_count) for the candidate list query.

    total_count is the COUNT(*) of the WHERE clause without LIMIT/OFFSET,
    so the UI can render pagination controls without a second request.
    """
    where_sql, args = _build_candidate_where(f)
    sort_col, sort_direction = _resolve_sort(_CANDIDATE_SORT_COLS, sort_by, sort_dir)
    n_limit  = _clamp_limit(limit)
    n_offset = _clamp_offset(offset)

    list_query = sql.SQL(
        "SELECT {cols} FROM raw.fec_candidate {where} "
        "ORDER BY {sort_col} {sort_dir} NULLS LAST "
        "LIMIT %s OFFSET %s",
    ).format(
        cols=_CANDIDATE_SELECT, where=where_sql,
        sort_col=sort_col, sort_dir=sort_direction,
    )
    count_query = sql.SQL(
        "SELECT COUNT(*) FROM raw.fec_candidate {where}",
    ).format(where=where_sql)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(list_query, [*args, n_limit, n_offset])
        rows = list(cur.fetchall())
    with conn.cursor() as cur:
        cur.execute(count_query, args)
        row = cur.fetchone()
        total = int(row[0]) if row else 0
    return rows, total


def get_candidate_detail(
    conn: psycopg.Connection,
    *,
    cand_id: str,
    cycle: str | None = None,
) -> dict[str, Any] | None:
    """Return the candidate row + linked committees, or None if not found.

    If *cycle* is None we return the most recent cycle for the cand_id;
    otherwise we scope to that cycle exactly.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        if cycle:
            cur.execute(
                """
                SELECT cycle, cand_id, cand_name, cand_pty_affiliation,
                       cand_office, cand_office_st, cand_office_district,
                       cand_ici, cand_status, cand_pcc,
                       cand_st1, cand_st2, cand_city, cand_st, cand_zip,
                       cand_election_yr
                FROM   raw.fec_candidate
                WHERE  cand_id = %s AND cycle = %s
                """,
                (cand_id, cycle),
            )
        else:
            cur.execute(
                """
                SELECT cycle, cand_id, cand_name, cand_pty_affiliation,
                       cand_office, cand_office_st, cand_office_district,
                       cand_ici, cand_status, cand_pcc,
                       cand_st1, cand_st2, cand_city, cand_st, cand_zip,
                       cand_election_yr
                FROM   raw.fec_candidate
                WHERE  cand_id = %s
                ORDER BY cycle DESC
                LIMIT 1
                """,
                (cand_id,),
            )
        row = cur.fetchone()
    if not row:
        return None

    eff_cycle = row["cycle"]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT cycle, cmte_id, cmte_nm AS committee_name,
                   tres_nm AS treasurer_name,
                   cmte_st, cmte_dsgn, cmte_tp, cmte_pty_affiliation, cand_id
            FROM   raw.fec_committee
            WHERE  cand_id = %s AND cycle = %s
            ORDER  BY cmte_dsgn, cmte_id
            """,
            (cand_id, eff_cycle),
        )
        committees = list(cur.fetchall())

    row["linked_committees"] = committees
    return row


# ============================================================================
# /fec/committees
# ============================================================================

_COMMITTEE_SELECT = sql.SQL(
    "cycle, cmte_id, "
    "cmte_nm AS committee_name, tres_nm AS treasurer_name, "
    "cmte_st, cmte_dsgn, cmte_tp, cmte_pty_affiliation, cand_id"
)


def list_committees(
    conn: psycopg.Connection,
    *,
    f: CommitteeFilters,
    sort_by: str | None,
    sort_dir: str | None,
    limit: int | None,
    offset: int | None,
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, total_count) for the committee list query."""
    where_sql, args = _build_committee_where(f)
    sort_col, sort_direction = _resolve_sort(_COMMITTEE_SORT_COLS, sort_by, sort_dir)
    n_limit  = _clamp_limit(limit)
    n_offset = _clamp_offset(offset)

    list_query = sql.SQL(
        "SELECT {cols} FROM raw.fec_committee {where} "
        "ORDER BY {sort_col} {sort_dir} NULLS LAST "
        "LIMIT %s OFFSET %s",
    ).format(
        cols=_COMMITTEE_SELECT, where=where_sql,
        sort_col=sort_col, sort_dir=sort_direction,
    )
    count_query = sql.SQL(
        "SELECT COUNT(*) FROM raw.fec_committee {where}",
    ).format(where=where_sql)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(list_query, [*args, n_limit, n_offset])
        rows = list(cur.fetchall())
    with conn.cursor() as cur:
        cur.execute(count_query, args)
        row = cur.fetchone()
        total = int(row[0]) if row else 0
    return rows, total


def get_committee_detail(
    conn: psycopg.Connection,
    *,
    cmte_id: str,
    cycle: str | None = None,
) -> dict[str, Any] | None:
    """Return the committee row + linked candidate (if any) + recent contribs."""
    with conn.cursor(row_factory=dict_row) as cur:
        if cycle:
            cur.execute(
                """
                SELECT cycle, cmte_id,
                       cmte_nm AS committee_name,
                       tres_nm AS treasurer_name,
                       cmte_st1, cmte_st2, cmte_city, cmte_st, cmte_zip,
                       cmte_dsgn, cmte_tp, cmte_pty_affiliation,
                       cmte_filing_freq, org_tp,
                       connected_org_nm, cand_id
                FROM   raw.fec_committee
                WHERE  cmte_id = %s AND cycle = %s
                """,
                (cmte_id, cycle),
            )
        else:
            cur.execute(
                """
                SELECT cycle, cmte_id,
                       cmte_nm AS committee_name,
                       tres_nm AS treasurer_name,
                       cmte_st1, cmte_st2, cmte_city, cmte_st, cmte_zip,
                       cmte_dsgn, cmte_tp, cmte_pty_affiliation,
                       cmte_filing_freq, org_tp,
                       connected_org_nm, cand_id
                FROM   raw.fec_committee
                WHERE  cmte_id = %s
                ORDER  BY cycle DESC
                LIMIT 1
                """,
                (cmte_id,),
            )
        row = cur.fetchone()
    if not row:
        return None

    eff_cycle = row["cycle"]
    cand_id = row.get("cand_id")
    linked_candidate: dict[str, Any] | None = None
    if cand_id:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT cycle, cand_id, cand_name, cand_pty_affiliation,
                       cand_office, cand_office_st, cand_office_district,
                       cand_ici, cand_status, cand_pcc
                FROM   raw.fec_candidate
                WHERE  cand_id = %s AND cycle = %s
                """,
                (cand_id, eff_cycle),
            )
            linked_candidate = cur.fetchone()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT cycle, sub_id, cmte_id,
                   contributor_name, contributor_city, contributor_state,
                   contributor_zip, contributor_employer, contributor_occupation,
                   contributor_entity_type, transaction_type,
                   transaction_primary_general, transaction_amount,
                   transaction_date, is_memo
            FROM   public.v_fec_contribution
            WHERE  cmte_id = %s AND cycle = %s
            ORDER  BY transaction_date DESC NULLS LAST
            LIMIT 25
            """,
            (cmte_id, eff_cycle),
        )
        recent = list(cur.fetchall())
    row["linked_candidate"]    = linked_candidate
    row["recent_contributions"] = recent
    return row


# ============================================================================
# /fec/contributions
# ============================================================================

_CONTRIBUTION_SELECT = sql.SQL(
    "cycle, sub_id, cmte_id, "
    "contributor_name, contributor_city, contributor_state, contributor_zip, "
    "contributor_employer, contributor_occupation, contributor_entity_type, "
    "transaction_type, transaction_primary_general, "
    "transaction_amount, transaction_date, is_memo"
)


def list_contributions(
    conn: psycopg.Connection,
    *,
    f: ContributionFilters,
    sort_by: str | None,
    sort_dir: str | None,
    limit: int | None,
    offset: int | None,
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, total_count) for the contributions list query."""
    where_sql, args = _build_contribution_where(f)
    sort_col, sort_direction = _resolve_sort(_CONTRIBUTION_SORT_COLS, sort_by, sort_dir)
    n_limit  = _clamp_limit(limit)
    n_offset = _clamp_offset(offset)

    list_query = sql.SQL(
        "SELECT {cols} FROM public.v_fec_contribution {where} "
        "ORDER BY {sort_col} {sort_dir} NULLS LAST "
        "LIMIT %s OFFSET %s",
    ).format(
        cols=_CONTRIBUTION_SELECT, where=where_sql,
        sort_col=sort_col, sort_dir=sort_direction,
    )
    count_query = sql.SQL(
        "SELECT COUNT(*) FROM public.v_fec_contribution {where}",
    ).format(where=where_sql)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(list_query, [*args, n_limit, n_offset])
        rows = list(cur.fetchall())
    with conn.cursor() as cur:
        cur.execute(count_query, args)
        row = cur.fetchone()
        total = int(row[0]) if row else 0
    return rows, total


# ============================================================================
# /fec/money-to-nj
# ============================================================================

_MONEY_TO_NJ_SELECT = sql.SQL(
    "cycle, sub_id, cand_id, cand_name, "
    "cand_office, cand_office_district, cand_pty_affiliation, "
    "cmte_id, committee_name, cmte_dsgn, "
    "contributor_name, contributor_city, contributor_state, "
    "contributor_zip, contributor_employer, contributor_occupation, "
    "contributor_entity_type, "
    "transaction_type, transaction_primary_general, "
    "transaction_amount, transaction_date, is_memo"
)


def list_money_to_nj(
    conn: psycopg.Connection,
    *,
    f: MoneyToNjFilters,
    sort_by: str | None,
    sort_dir: str | None,
    limit: int | None,
    offset: int | None,
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, total_count) for the money-to-NJ-candidates view."""
    where_sql, args = _build_money_to_nj_where(f)
    sort_col, sort_direction = _resolve_sort(_MONEY_TO_NJ_SORT_COLS, sort_by, sort_dir)
    n_limit  = _clamp_limit(limit)
    n_offset = _clamp_offset(offset)

    list_query = sql.SQL(
        "SELECT {cols} FROM public.v_fec_money_to_nj_candidates {where} "
        "ORDER BY {sort_col} {sort_dir} NULLS LAST "
        "LIMIT %s OFFSET %s",
    ).format(
        cols=_MONEY_TO_NJ_SELECT, where=where_sql,
        sort_col=sort_col, sort_dir=sort_direction,
    )
    count_query = sql.SQL(
        "SELECT COUNT(*) FROM public.v_fec_money_to_nj_candidates {where}",
    ).format(where=where_sql)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(list_query, [*args, n_limit, n_offset])
        rows = list(cur.fetchall())
    with conn.cursor() as cur:
        cur.execute(count_query, args)
        row = cur.fetchone()
        total = int(row[0]) if row else 0
    return rows, total


# ============================================================================
# CSV streaming queries
# ============================================================================
#
# CSV exports run against a server-side cursor (named cursor) so the
# full result set is not buffered in client memory. The cursor is
# torn down by the consumer's `with` block in the route layer.
# ============================================================================


def stream_candidates(
    conn: psycopg.Connection,
    *,
    f: CandidateFilters,
    cap: int = MAX_EXPORT,
) -> tuple[list[str], Any]:
    """Yield (header_columns, server-side-cursor) for streaming candidates."""
    where_sql, args = _build_candidate_where(f)
    cols = [
        "cycle", "cand_id", "cand_name", "cand_pty_affiliation",
        "cand_office", "cand_office_st", "cand_office_district",
        "cand_ici", "cand_status", "cand_pcc",
        "cand_st1", "cand_st2", "cand_city", "cand_st", "cand_zip",
        "cand_election_yr",
    ]
    query = sql.SQL(
        "SELECT {cols} FROM raw.fec_candidate {where} "
        "ORDER BY cycle DESC, cand_id "
        "LIMIT %s",
    ).format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        where=where_sql,
    )
    cur = conn.cursor(name="export_candidates")
    cur.itersize = 5000
    cur.execute(query, [*args, cap])
    return cols, cur


def stream_committees(
    conn: psycopg.Connection,
    *,
    f: CommitteeFilters,
    cap: int = MAX_EXPORT,
) -> tuple[list[str], Any]:
    """Yield (header_columns, server-side-cursor) for streaming committees."""
    where_sql, args = _build_committee_where(f)
    cols = [
        "cycle", "cmte_id", "cmte_nm", "tres_nm",
        "cmte_st1", "cmte_st2", "cmte_city", "cmte_st", "cmte_zip",
        "cmte_dsgn", "cmte_tp", "cmte_pty_affiliation",
        "cmte_filing_freq", "org_tp", "connected_org_nm", "cand_id",
    ]
    query = sql.SQL(
        "SELECT {cols} FROM raw.fec_committee {where} "
        "ORDER BY cycle DESC, cmte_id "
        "LIMIT %s",
    ).format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        where=where_sql,
    )
    cur = conn.cursor(name="export_committees")
    cur.itersize = 5000
    cur.execute(query, [*args, cap])
    return cols, cur


def stream_contributions(
    conn: psycopg.Connection,
    *,
    f: ContributionFilters,
    cap: int = MAX_EXPORT,
) -> tuple[list[str], Any]:
    """Yield (header_columns, server-side-cursor) for streaming contributions."""
    where_sql, args = _build_contribution_where(f)
    cols = [
        "cycle", "sub_id", "cmte_id",
        "contributor_name", "contributor_city", "contributor_state",
        "contributor_zip", "contributor_employer", "contributor_occupation",
        "contributor_entity_type",
        "transaction_type", "transaction_primary_general",
        "transaction_amount", "transaction_date", "is_memo",
    ]
    query = sql.SQL(
        "SELECT {cols} FROM public.v_fec_contribution {where} "
        "ORDER BY transaction_date DESC NULLS LAST, sub_id "
        "LIMIT %s",
    ).format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        where=where_sql,
    )
    cur = conn.cursor(name="export_contributions")
    cur.itersize = 5000
    cur.execute(query, [*args, cap])
    return cols, cur


def stream_money_to_nj(
    conn: psycopg.Connection,
    *,
    f: MoneyToNjFilters,
    cap: int = MAX_EXPORT,
) -> tuple[list[str], Any]:
    """Yield (header_columns, server-side-cursor) for streaming money-to-NJ."""
    where_sql, args = _build_money_to_nj_where(f)
    cols = [
        "cycle", "sub_id", "cand_id", "cand_name",
        "cand_office", "cand_office_district", "cand_pty_affiliation",
        "cmte_id", "committee_name", "cmte_dsgn",
        "contributor_name", "contributor_city", "contributor_state",
        "contributor_zip", "contributor_employer", "contributor_occupation",
        "contributor_entity_type",
        "transaction_type", "transaction_primary_general",
        "transaction_amount", "transaction_date", "is_memo",
    ]
    query = sql.SQL(
        "SELECT {cols} FROM public.v_fec_money_to_nj_candidates {where} "
        "ORDER BY transaction_date DESC NULLS LAST, sub_id "
        "LIMIT %s",
    ).format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        where=where_sql,
    )
    cur = conn.cursor(name="export_money_to_nj")
    cur.itersize = 5000
    cur.execute(query, [*args, cap])
    return cols, cur
