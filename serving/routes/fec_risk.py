"""Tier 4 v3 -- entity-first fraud-risk read API.

Three endpoints:

  GET  /fec/risk/entities                       -> sorted, paginated queue
  GET  /fec/risk/entities/{kind}/{id}           -> full evidence panel (JSON)
  GET  /fec/risk/entities/{kind}/{id}/csv       -> evidence trail (CSV,
                                                   one row per observation)

All three read from ``derived.v_entity_fraud_risk`` (migrations 050-052).
The route layer is intentionally thin: it parses query parameters,
validates them, calls the query layer, and projects results into the
public Pydantic shapes. All business logic (whitelisting, score
decomposition) lives in :mod:`serving.queries_fec_risk`.

Error semantics
---------------
* Unknown ``entity_kind``                  -> 400 with the allowed set
* Bad ``sort_by``                          -> 400 with the allowed set
* Out-of-range / inverted score filters    -> 400 with explanation
* Entity has no fired signals (or doesn't exist) -> 404
* DB unavailable                           -> 503 (handled by serving.db)

These match the existing /fec/* endpoints' contract.

Why ``entity_kind`` is a path parameter on the panel route
----------------------------------------------------------
``entity_id`` is not globally unique across kinds. A treasurer named
"DOE, JOHN" can collide with a candidate ID called "DOE, JOHN" only if
ID schemes happen to overlap, but more importantly, the kind tells the
analyst (and the UI) what page to drill into next. Encoding it in the
URL also keeps the route cacheable per (kind, id) pair.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import StreamingResponse

from serving.db import borrow_connection
from serving.models import (
    RiskEntityPanel,
    RiskQueueResponse,
    RiskQueueRow,
    RiskSignalObservation,
)
from serving.queries_fec_risk import (
    DEFAULT_LIMIT,
    DEFAULT_SORT_BY,
    EVIDENCE_CSV_COLUMNS,
    MAX_LIMIT,
    SORT_COLS,
    VALID_ENTITY_KINDS,
    evidence_csv_rows,
    get_risk_entity,
    list_risk_entities,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

log = logging.getLogger(__name__)

router = APIRouter(prefix="/fec/risk", tags=["fec_risk"])


# ============================================================================
# GET /fec/risk/entities  -- sorted, paginated queue
# ============================================================================

@router.get(
    "/entities",
    response_model=RiskQueueResponse,
    summary="Risk-ranked entity queue (sortable, filterable, paginated)",
)
def get_risk_queue(  # noqa: D103 -- public-facing docstring lives in summary=
    cycle: str | None = Query(
        default=None,
        examples=["2024"],
        description="FEC election cycle. Omit for all cycles present.",
    ),
    entity_kind: str | None = Query(
        default=None,
        description=(
            "Restrict to one entity kind. One of: "
            "committee, candidate, treasurer, address, donor_cluster."
        ),
        examples=["candidate"],
    ),
    signal_id: str | None = Query(
        default=None,
        description=(
            "Restrict to entities for which the named signal fired. "
            "Use the IDs from /fec/metrics (e.g. 'treasurer_concentration')."
        ),
    ),
    min_score: float | None = Query(
        default=None,
        ge=0,
        le=100,
        description="Inclusive lower bound on risk_score (0..100).",
    ),
    max_score: float | None = Query(
        default=None,
        ge=0,
        le=100,
        description="Inclusive upper bound on risk_score (0..100).",
    ),
    sort_by: str | None = Query(
        default=None,
        description=(
            "Whitelisted column. Allowed: " + ", ".join(sorted(SORT_COLS)) +
            f". Default: {DEFAULT_SORT_BY}."
        ),
    ),
    sort_dir: str | None = Query(
        default="DESC",
        pattern="^(?i)(asc|desc)$",
    ),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> RiskQueueResponse:
    try:
        with borrow_connection() as conn:
            rows, total = list_risk_entities(
                conn,
                cycle=cycle,
                entity_kind=entity_kind,
                signal_id=signal_id,
                min_score=min_score,
                max_score=max_score,
                sort_by=sort_by,
                sort_dir=sort_dir,
                limit=limit,
                offset=offset,
            )
    except KeyError as exc:
        # Whitelist failure (sort_by, entity_kind) or score-range failure.
        # All map to 400.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    queue_rows = [RiskQueueRow.model_validate(r) for r in rows]
    return RiskQueueResponse(
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
            "sort_by":     sort_by or DEFAULT_SORT_BY,
            "sort_dir":    (sort_dir or "DESC").upper(),
        },
    )


# ============================================================================
# GET /fec/risk/entities/{kind}/{id}/csv  -- evidence trail (one row/obs)
#
# CRITICAL: This route MUST be declared before the JSON panel route. The
# panel uses ``{entity_id:path}`` to support entity_ids with embedded
# slashes (canonical addresses), and ``:path`` greedily consumes "/csv"
# as part of entity_id. FastAPI matches routes in declaration order, so
# the more specific ``.../csv`` suffix must win first.
# ============================================================================

def _evidence_csv_iter(
    columns: tuple[str, ...],
    rows: list[list[object]],
) -> Iterator[bytes]:
    """Yield CSV bytes (header + per-observation rows).

    Buffers in memory: a panel has at most ``len(catalog)`` rows
    (currently 8 Tier-A signals; even at 50 future signals this is
    trivial). No server-side cursor needed.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(columns)
    yield buf.getvalue().encode("utf-8")
    buf.seek(0)
    buf.truncate()

    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
        yield buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate()


@router.get(
    "/entities/{entity_kind}/{entity_id:path}/csv",
    response_class=StreamingResponse,
    summary="CSV export of one entity's evidence trail (one row per observation)",
)
def export_risk_entity_csv(  # noqa: D103 -- public-facing docstring lives in summary=
    entity_kind: str = Path(
        ...,
        description=(
            "One of: committee, candidate, treasurer, address, donor_cluster."
        ),
        examples=["candidate"],
    ),
    entity_id: str = Path(
        ...,
        description=(
            "Stable identifier; shape depends on entity_kind. Path "
            "supports embedded slashes via the :path converter."
        ),
    ),
    cycle: str | None = Query(
        default=None,
        examples=["2024"],
        description="FEC election cycle. Omit for the most recent observation.",
    ),
) -> StreamingResponse:
    # Validate entity_kind BEFORE any DB roundtrip (also matches the
    # JSON route's contract: bad kind -> 400, not 404).
    if entity_kind not in VALID_ENTITY_KINDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown entity_kind {entity_kind!r}. "
                f"Allowed: {sorted(VALID_ENTITY_KINDS)}"
            ),
        )

    # Fetch the panel synchronously so a 404 returns a clean JSON error,
    # not a half-streamed CSV with HTTP 200 followed by an exception.
    with borrow_connection() as conn:
        panel = get_risk_entity(
            conn,
            entity_kind=entity_kind,
            entity_id=entity_id,
            cycle=cycle,
        )
    if panel is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No fired signals for {entity_kind}/{entity_id}"
                + (f" in cycle {cycle}" if cycle else "")
                + ". Either the entity exists but is below all thresholds, "
                "or the entity is unknown to the L1 layer."
            ),
        )

    rows = evidence_csv_rows(panel)
    panel_cycle = panel["cycle"]
    suffix = f"_{panel_cycle}" if panel_cycle else ""
    # entity_id may contain commas/slashes; sanitize for the filename.
    safe_id = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in entity_id)
    filename = f"risk_{entity_kind}_{safe_id}{suffix}.csv"

    return StreamingResponse(
        _evidence_csv_iter(EVIDENCE_CSV_COLUMNS, rows),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ============================================================================
# GET /fec/risk/entities/{kind}/{id}  -- single-entity evidence panel (JSON)
#
# MUST be declared after the /csv route above. See the routing-order note
# at the top of the CSV section.
# ============================================================================

@router.get(
    "/entities/{entity_kind}/{entity_id:path}",
    response_model=RiskEntityPanel,
    summary="Evidence panel for one entity (signals + score decomposition)",
)
def get_risk_panel(  # noqa: D103 -- public-facing docstring lives in summary=
    entity_kind: str = Path(
        ...,
        description=(
            "One of: committee, candidate, treasurer, address, donor_cluster."
        ),
        examples=["candidate"],
    ),
    entity_id: str = Path(
        ...,
        description=(
            "Stable identifier; shape depends on entity_kind. Path "
            "supports embedded slashes (e.g. canonical addresses) via "
            "the :path converter, so quote responsibly."
        ),
    ),
    cycle: str | None = Query(
        default=None,
        examples=["2024"],
        description="FEC election cycle. Omit for the most recent observation.",
    ),
) -> RiskEntityPanel:
    if entity_kind not in VALID_ENTITY_KINDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown entity_kind {entity_kind!r}. "
                f"Allowed: {sorted(VALID_ENTITY_KINDS)}"
            ),
        )

    with borrow_connection() as conn:
        panel = get_risk_entity(
            conn,
            entity_kind=entity_kind,
            entity_id=entity_id,
            cycle=cycle,
        )
    if panel is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No fired signals for {entity_kind}/{entity_id}"
                + (f" in cycle {cycle}" if cycle else "")
                + ". Either the entity exists but is below all thresholds, "
                "or the entity is unknown to the L1 layer."
            ),
        )

    observations = [
        RiskSignalObservation.model_validate(o) for o in panel["observations"]
    ]
    return RiskEntityPanel(
        cycle=panel["cycle"],
        entity_kind=panel["entity_kind"],
        entity_id=panel["entity_id"],
        risk_score=panel["risk_score"],
        n_signals_fired=panel["n_signals_fired"],
        max_severity=panel["max_severity"],
        max_peer_percentile=panel["max_peer_percentile"],
        avg_peer_percentile=panel["avg_peer_percentile"],
        primary_peer_bucket=panel["primary_peer_bucket"],
        last_observation_at=panel["last_observation_at"],
        observations=observations,
    )
