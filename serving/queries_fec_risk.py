"""Query layer for the Tier 4 v3 fraud-risk surface.

This module backs two HTTP routes:

* ``GET /fec/risk/entities``                     -> sorted, paginated queue
* ``GET /fec/risk/entities/{kind}/{id}``         -> full evidence panel

Both read from ``derived.v_entity_fraud_risk`` (migration 052), which is
the canonical fraud-risk read surface. The view already carries:

* ``risk_score``         -- the L3a rule-based score (0..100)
* the L2 entity-feature aggregates
  (``n_signals_fired``, ``max_severity``, ``max_peer_percentile``,
   ``avg_peer_percentile``, ``primary_peer_bucket``, ...)
* the per-entity parallel arrays
  (``signals_fired``, ``severities``, ``peer_percentiles``,
   ``peer_buckets``, ``raw_values``, ``evidence_urls``)

Module responsibilities (the route layer is a thin shell on top):

1.  Whitelist + safely compose ``ORDER BY`` for the queue.
2.  Apply filters (``cycle``, ``entity_kind``, ``signal_id``,
    ``min_score``, ``max_score``) -- always parameterised, never
    string-interpolated.
3.  Pivot the panel's parallel arrays into a deterministic list of
    per-signal observations and compute the L3a per-signal score
    decomposition in Python (the math is the same as
    ``derived.fraud_risk_score`` -- we duplicate it here purely so the
    panel can attribute the score to individual signals; the view
    remains the source of truth for the total).

Why pivot in Python instead of SQL
----------------------------------
The arrays per entity are small (typically 1-5 signals; the L1 dispatcher
caps observation cardinality at one row per (cycle, entity_kind,
entity_id, signal_id)). Round-tripping a single panel as parallel arrays
costs less than ``LATERAL UNNEST`` plus another query, and the score
decomposition (``phi_s = severity_s * max(0, percentile_s - 0.95)^2``)
is easier to test in Python than in PL/pgSQL.

Sort safety
-----------
``sort_by`` is whitelisted via :data:`SORT_COLS`. Anything else raises
``KeyError`` before any SQL is composed. Defense in depth even though
``psycopg.sql.Identifier`` would also reject the literal at compose time.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Final

from psycopg import sql
from psycopg.rows import dict_row

if TYPE_CHECKING:
    import psycopg

log = logging.getLogger(__name__)


# ============================================================================
# Constants -- mirror the L1 CHECK constraint and migration 052's scoring
# formula. Keep this file in sync with both.
# ============================================================================

#: L1 ``entity_kind`` domain. This MUST stay identical to the
#: ``fraud_signal_observation_entity_kind_check`` CHECK constraint on
#: ``derived.fraud_signal_observation``. That constraint is the single
#: source of truth; this frozenset is a serving-layer mirror so the API
#: can reject unknown kinds with a 400 before touching Postgres.
#:
#: The domain has been widened five times; cite each migration so the
#: lineage is auditable:
#:   * mig 050 -- committee, candidate, treasurer, address, donor_cluster
#:   * mig 058 -- + contractor   (federal-award recipient on LEIE)
#:   * mig 059 -- + donor        (individual donor on LEIE)
#:   * mig 098 -- + nj_state_candidate (NJ-state roster on LEIE)
#:   * mig 101 -- + provider     (NPI-keyed; LEIE x CMS Part D overlap)
#:
#: ``test_serving_fec_risk.py`` asserts (against a live DB) that this set
#: equals the parsed CHECK constraint domain, so any future widening that
#: forgets to update this constant fails CI instead of drifting silently.
VALID_ENTITY_KINDS: Final[frozenset[str]] = frozenset({
    "committee",
    "candidate",
    "treasurer",
    "address",
    "donor_cluster",
    "contractor",
    "donor",
    "nj_state_candidate",
    "provider",
    "employer",
})

#: Whitelisted sort columns for the queue endpoint. ``risk_score`` is the
#: default since the queue's purpose is "highest risk first". The other
#: columns let the UI offer secondary sorts (most signals, most extreme
#: peer outlier, most severe single signal) without giving the SQL layer
#: an open string.
SORT_COLS: Final[frozenset[str]] = frozenset({
    "risk_score",
    "n_signals_fired",
    "max_severity",
    "max_peer_percentile",
    "avg_peer_percentile",
    "last_observation_at",
    "entity_id",
})

DEFAULT_SORT_BY: Final[str] = "risk_score"

#: Pagination knobs match the rest of the FEC read surface so the UI can
#: reuse the same page-control component across queue + metric tabs.
DEFAULT_LIMIT: Final[int] = 100
MAX_LIMIT:     Final[int] = 1000

#: L3a scoring constants. KEEP IN SYNC WITH ``derived.fraud_risk_score`` in
#: migration 052. If you change them in SQL, change them here too.
SCORE_GAMMA: Final[int]   = 2     # phi_s = severity_s * max(0, p - 0.95)^GAMMA
SCORE_K:     Final[int]   = 50    # score = 100 * (1 - exp(-K * raw_sum))
SCORE_PCT_FLOOR: Final[float] = 0.95  # signals below this percentile contribute 0


# ============================================================================
# Helpers
# ============================================================================

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


def _validate_entity_kind(entity_kind: str | None) -> None:
    """Raise KeyError if ``entity_kind`` is set but not in the L1 domain.

    A ``None`` value means "all kinds" and is allowed. Empty string is a
    user error and is also rejected.
    """
    if entity_kind is None:
        return
    if entity_kind == "" or entity_kind not in VALID_ENTITY_KINDS:
        raise KeyError(
            f"Unknown entity_kind {entity_kind!r}. "
            f"Allowed: {sorted(VALID_ENTITY_KINDS)}",
        )


def _validate_score_range(
    min_score: float | None,
    max_score: float | None,
) -> None:
    """Reject obviously-broken score filters with KeyError (-> 400).

    ``risk_score`` is bounded to [0, 100] by the SQL function. Filters
    that lie outside this range, or that invert (min > max), are user
    errors and we want a 4xx, not a silent empty result set.
    """
    for name, value in (("min_score", min_score), ("max_score", max_score)):
        if value is not None and (value < 0 or value > 100):
            raise KeyError(
                f"{name}={value} out of range; risk_score is bounded to [0, 100].",
            )
    if min_score is not None and max_score is not None and min_score > max_score:
        raise KeyError(
            f"min_score={min_score} > max_score={max_score} (empty range).",
        )


# ============================================================================
# Queue: list_risk_entities
# ============================================================================

#: Columns returned in queue rows. Intentionally NOT the parallel arrays
#: -- the queue is a "scan and pick" surface; arrays belong to the
#: per-entity panel where the analyst is willing to pay the bytes.
_QUEUE_COLS: Final[tuple[str, ...]] = (
    "cycle",
    "entity_kind",
    "entity_id",
    "risk_score",
    "n_signals_fired",
    "max_severity",
    "max_peer_percentile",
    "avg_peer_percentile",
    "primary_peer_bucket",
    "signals_fired",
    "last_observation_at",
)


def list_risk_entities(
    conn: psycopg.Connection,
    *,
    cycle: str | None = None,
    entity_kind: str | None = None,
    signal_id: str | None = None,
    min_score: float | None = None,
    max_score: float | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(rows, total_count)`` for the risk queue.

    Filters are AND-composed. ``signal_id`` matches entities for which
    the signal *fired* in the cycle (uses the array containment operator
    ``%s = ANY(signals_fired)``). All filters are parameterised.

    Raises ``KeyError`` for:
    * unknown ``sort_by`` (whitelist failure)
    * unknown ``entity_kind``
    * out-of-range or inverted score filters

    The route layer translates these to HTTP 400.
    """
    requested_sort = sort_by or DEFAULT_SORT_BY
    if requested_sort not in SORT_COLS:
        raise KeyError(
            f"Sort column {requested_sort!r} not allowed. "
            f"Allowed: {sorted(SORT_COLS)}",
        )
    _validate_entity_kind(entity_kind)
    _validate_score_range(min_score, max_score)

    sort_col       = sql.Identifier(requested_sort)
    sort_direction = _resolve_sort_dir(sort_dir)
    n_limit        = _clamp_limit(limit)
    n_offset       = _clamp_offset(offset)

    where_clauses: list[sql.Composable] = []
    args: list[Any] = []
    if cycle is not None:
        where_clauses.append(sql.SQL("cycle = %s"))
        args.append(cycle)
    if entity_kind is not None:
        where_clauses.append(sql.SQL("entity_kind = %s"))
        args.append(entity_kind)
    if signal_id is not None:
        # ``signals_fired`` is sorted ascending by the L2 view, but
        # ANY() is order-independent, so this is safe.
        where_clauses.append(sql.SQL("%s = ANY(signals_fired)"))
        args.append(signal_id)
    if min_score is not None:
        where_clauses.append(sql.SQL("risk_score >= %s"))
        args.append(min_score)
    if max_score is not None:
        where_clauses.append(sql.SQL("risk_score <= %s"))
        args.append(max_score)

    where_sql = (
        sql.SQL("WHERE ") + sql.SQL(" AND ").join(where_clauses)
        if where_clauses
        else sql.SQL("")
    )
    cols_sql = sql.SQL(", ").join(sql.Identifier(c) for c in _QUEUE_COLS)

    list_query = sql.SQL(
        "SELECT {cols} FROM derived.v_entity_fraud_risk {where} "
        "ORDER BY {sort_col} {sort_dir} NULLS LAST, "
        "          entity_kind ASC, entity_id ASC "
        "LIMIT %s OFFSET %s",
    ).format(
        cols=cols_sql,
        where=where_sql,
        sort_col=sort_col,
        sort_dir=sort_direction,
    )
    count_query = sql.SQL(
        "SELECT COUNT(*) FROM derived.v_entity_fraud_risk {where}",
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
# Panel: get_risk_entity
# ============================================================================

def _phi(severity: int, percentile: float) -> float:
    """Per-signal raw contribution to ``raw_sum``.

    ``phi_s = severity_s * max(0, percentile_s - SCORE_PCT_FLOOR)^SCORE_GAMMA``

    Mirrors the SUM term inside ``derived.fraud_risk_score``. Signals
    below the percentile floor (typically p95) contribute zero -- this
    is the explicit policy that "an entity barely above median in its
    peer group is not a risk lead, no matter how severe the signal type".
    """
    excess = max(0.0, float(percentile) - SCORE_PCT_FLOOR)
    return float(severity) * (excess ** SCORE_GAMMA)


def _decompose_score(
    signal_ids: list[str],
    severities: list[int],
    percentiles: list[float],
) -> tuple[list[dict[str, Any]], float]:
    """Compute per-signal phi + share-of-score and the total raw_sum.

    Returns a ``(per_signal, raw_sum)`` pair. Each per-signal dict has:

    * ``phi_contribution``    -- raw additive contribution
    * ``score_share_pct``     -- phi_s / raw_sum * 100, or 0.0 when
                                  raw_sum == 0 (i.e. no signal exceeds
                                  the percentile floor)

    The route layer zips this with the rest of the panel arrays into
    a list of per-signal observations.
    """
    n = len(signal_ids)
    if not (n == len(severities) == len(percentiles)):
        raise ValueError(
            f"Parallel-array length mismatch: signals={n}, "
            f"severities={len(severities)}, percentiles={len(percentiles)}",
        )

    phi_values = [_phi(severities[i], percentiles[i]) for i in range(n)]
    raw_sum = sum(phi_values)

    per_signal: list[dict[str, Any]] = []
    for i in range(n):
        phi_i = phi_values[i]
        share = (phi_i / raw_sum * 100.0) if raw_sum > 0 else 0.0
        per_signal.append({
            "phi_contribution": phi_i,
            "score_share_pct":  share,
        })
    return per_signal, raw_sum


def get_risk_entity(
    conn: psycopg.Connection,
    *,
    entity_kind: str,
    entity_id: str,
    cycle: str | None = None,
) -> dict[str, Any] | None:
    """Return the full evidence panel for one (entity_kind, entity_id, cycle).

    If ``cycle`` is omitted, returns the most recent cycle for that
    entity (by ``last_observation_at``). Returns ``None`` if the entity
    has no fired signals (which is the case for ~all entities, since
    the L1 layer only stores observations for entities that triggered
    at least one signal).

    Raises ``KeyError`` for an unknown ``entity_kind``.
    """
    _validate_entity_kind(entity_kind)
    if not entity_id:
        # Empty entity_id is never a hit -- the route would 404, and we
        # don't want to roundtrip Postgres to learn that.
        return None

    base_query = (
        "SELECT cycle, entity_kind, entity_id, risk_score, "
        "       n_signals_fired, max_severity, max_peer_percentile, "
        "       avg_peer_percentile, primary_peer_bucket, "
        "       signals_fired, severities, peer_percentiles, peer_buckets, "
        "       raw_values, evidence_urls, last_observation_at "
        "FROM derived.v_entity_fraud_risk "
        "WHERE entity_kind = %s AND entity_id = %s "
    )
    args: list[Any] = [entity_kind, entity_id]
    if cycle is not None:
        base_query += "AND cycle = %s "
        args.append(cycle)
    # Stable tiebreak when no cycle is requested: latest observation
    # wins, then highest score, then highest cycle string.
    base_query += "ORDER BY last_observation_at DESC, risk_score DESC, cycle DESC LIMIT 1"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(base_query, args)
        row = cur.fetchone()
    if row is None:
        return None

    # Pivot parallel arrays into a list of per-signal observation dicts
    # AND attach the score decomposition. The arrays come back as Python
    # lists from psycopg already; their order is "ARRAY_AGG ... ORDER BY
    # signal_id" per the L2 view definition (migration 050).
    signal_ids:  list[str]   = list(row["signals_fired"])
    severities:  list[int]   = [int(s) for s in row["severities"]]
    percentiles: list[float] = [float(p) for p in row["peer_percentiles"]]
    buckets:     list[str]   = list(row["peer_buckets"])
    raw_values:  list[Any]   = list(row["raw_values"])
    evidence:    list[str]   = list(row["evidence_urls"])

    decomposition, raw_sum = _decompose_score(signal_ids, severities, percentiles)

    observations: list[dict[str, Any]] = []
    for i, sid in enumerate(signal_ids):
        observations.append({
            "signal_id":         sid,
            "severity":          severities[i],
            "peer_percentile":   percentiles[i],
            "peer_bucket":       buckets[i],
            "raw_value":         raw_values[i] if i < len(raw_values) else None,
            "evidence_url":      evidence[i],
            "phi_contribution":  decomposition[i]["phi_contribution"],
            "score_share_pct":   decomposition[i]["score_share_pct"],
        })

    # Sort observations by score share descending so the panel renders
    # "biggest contributors first" without UI logic. Stable tiebreak on
    # signal_id keeps the order deterministic when no signal exceeds
    # the percentile floor.
    observations.sort(
        key=lambda o: (-o["score_share_pct"], o["signal_id"]),
    )

    # Sanity: the sum of phi_contributions should reproduce raw_sum and
    # the final score should match what the SQL function computed. We
    # don't fail loud here (the SQL function is the source of truth);
    # we log a warning if they diverge by more than rounding tolerance.
    expected_score = 100.0 * (1.0 - math.exp(-SCORE_K * raw_sum))
    expected_score = max(0.0, min(100.0, expected_score))
    if abs(expected_score - float(row["risk_score"])) > 0.05:
        log.warning(
            "score_decomposition.divergence entity=%s/%s cycle=%s "
            "py=%.4f sql=%.4f",
            entity_kind, entity_id, row["cycle"],
            expected_score, float(row["risk_score"]),
        )

    return {
        "cycle":               row["cycle"],
        "entity_kind":         row["entity_kind"],
        "entity_id":           row["entity_id"],
        "risk_score":          float(row["risk_score"]),
        "n_signals_fired":     int(row["n_signals_fired"]),
        "max_severity":        int(row["max_severity"]),
        "max_peer_percentile": float(row["max_peer_percentile"]),
        "avg_peer_percentile": float(row["avg_peer_percentile"]),
        "primary_peer_bucket": row["primary_peer_bucket"],
        "last_observation_at": row["last_observation_at"],
        "observations":        observations,
    }


# ============================================================================
# Evidence-trail CSV: per-observation rows
# ============================================================================

#: Stable column order for the per-observation CSV. The first 9 columns
#: are entity-level (repeated on every row); the last 8 are
#: observation-level. We deliberately repeat the entity columns so the
#: CSV is self-contained when an analyst pivots / filters by signal in
#: Excel without context. KEEP IN SYNC with the route + CLI.
EVIDENCE_CSV_COLUMNS: Final[tuple[str, ...]] = (
    "cycle",
    "entity_kind",
    "entity_id",
    "risk_score",
    "n_signals_fired",
    "max_severity",
    "max_peer_percentile",
    "avg_peer_percentile",
    "primary_peer_bucket",
    "last_observation_at",
    "signal_id",
    "severity",
    "peer_percentile",
    "peer_bucket",
    "raw_value",
    "phi_contribution",
    "score_share_pct",
    "evidence_url",
)


def evidence_csv_rows(panel: dict[str, Any]) -> list[list[Any]]:
    """Flatten a panel dict (from :func:`get_risk_entity`) into CSV rows.

    One row per observation. The entity-level columns are repeated on
    every row so consumers can group / filter without joining back to
    the panel. ``last_observation_at`` is rendered as ISO-8601 with a
    trailing ``Z`` when timezone-aware -- the same shape Pydantic's
    ``model_dump(mode="json")`` produces for our datetimes.

    Returns an empty list if the panel has no observations (which is
    impossible by construction: ``get_risk_entity`` returns ``None``
    when no observation rows exist).
    """
    cycle = panel["cycle"]
    entity_kind = panel["entity_kind"]
    entity_id = panel["entity_id"]
    risk_score = float(panel["risk_score"])
    n_signals_fired = int(panel["n_signals_fired"])
    max_severity = int(panel["max_severity"])
    max_pct = float(panel["max_peer_percentile"])
    avg_pct = float(panel["avg_peer_percentile"])
    primary_bucket = panel["primary_peer_bucket"]
    last_obs_at = panel["last_observation_at"]
    last_obs_iso = (
        last_obs_at.isoformat() if hasattr(last_obs_at, "isoformat")
        else str(last_obs_at)
    )

    rows: list[list[Any]] = []
    for o in panel["observations"]:
        rows.append([
            cycle,
            entity_kind,
            entity_id,
            risk_score,
            n_signals_fired,
            max_severity,
            max_pct,
            avg_pct,
            primary_bucket,
            last_obs_iso,
            o["signal_id"],
            int(o["severity"]),
            float(o["peer_percentile"]),
            o["peer_bucket"],
            (None if o["raw_value"] is None else float(o["raw_value"])),
            float(o["phi_contribution"]),
            float(o["score_share_pct"]),
            o["evidence_url"],
        ])
    return rows
