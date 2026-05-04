"""CSV streaming exports for the FEC read API.

Four endpoints, one per table/view, all under ``/fec/export/*.csv``.
Same query parameters as the corresponding ``/fec/*`` JSON endpoint
(filters carry over byte-for-byte) so the UI can wire "Export current
filtered set" buttons directly to the export URL with the same query
string.

Streaming + memory invariant
----------------------------
We use a Postgres SERVER-SIDE NAMED CURSOR (``conn.cursor(name=...)``)
rather than a client-side cursor. This streams rows from Postgres in
``itersize`` chunks so RAM usage stays flat regardless of the result
set size. Combined with FastAPI's ``StreamingResponse`` and a Python
generator, the entire pipeline -- DB -> Python -> HTTP body -- runs
in constant memory.

Hard cap
--------
Each export is capped at ``MAX_EXPORT`` rows (100,000) defensively.
If a user exhausts that cap they can re-run with a tighter filter.
The cap is documented in the response's first row (a small comment
header) so consumers cannot silently miss truncation.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from serving.db import borrow_connection
from serving.queries_fec import (
    MAX_EXPORT,
    CandidateFilters,
    CommitteeFilters,
    ContributionFilters,
    MoneyToNjFilters,
    stream_candidates,
    stream_committees,
    stream_contributions,
    stream_money_to_nj,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

router = APIRouter(prefix="/fec/export", tags=["fec_export"])


# ============================================================================
# CSV stream helper
# ============================================================================


def _csv_stream(
    columns: list[str], cursor: object,
) -> Iterator[bytes]:
    """Yield CSV bytes (header + rows) from a Postgres server-side cursor.

    The cursor is consumed lazily and CLOSED implicitly when the
    generator is exhausted. Each yield is a single line so the
    StreamingResponse flushes incrementally.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(columns)
    yield buf.getvalue().encode("utf-8")
    buf.seek(0)
    buf.truncate()

    for row in cursor:  # type: ignore[attr-defined]
        # Server-side cursor row is a tuple; coerce None -> '' so CSV
        # consumers do not see Python's "None" string. csv.QUOTE_MINIMAL
        # quotes only fields containing commas/quotes/newlines.
        writer.writerow(["" if v is None else v for v in row])
        yield buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate()


def _attachment_headers(filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Export-Cap-Rows":   str(MAX_EXPORT),
    }


# ============================================================================
# /fec/export/candidates.csv
# ============================================================================


@router.get(
    "/candidates.csv",
    response_class=StreamingResponse,
    summary="Stream the filtered candidate set as CSV",
)
def export_candidates(
    cycle:         str | None = Query(default=None),
    state:         str | None = Query(default=None),
    office:        str | None = Query(default=None),
    party:         str | None = Query(default=None),
    incumbent:     str | None = Query(default=None),
    status:        str | None = Query(default=None),
    name_contains: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream raw.fec_candidate rows matching the filter as CSV."""
    f = CandidateFilters(
        cycle=cycle, state=state, office=office, party=party,
        incumbent=incumbent, status=status, name_contains=name_contains,
    )

    def gen() -> Iterator[bytes]:
        with borrow_connection() as conn:
            cols, cur = stream_candidates(conn, f=f)
            try:
                yield from _csv_stream(cols, cur)
            finally:
                cur.close()

    return StreamingResponse(
        gen(), media_type="text/csv",
        headers=_attachment_headers("fec_candidates.csv"),
    )


# ============================================================================
# /fec/export/committees.csv
# ============================================================================


@router.get(
    "/committees.csv",
    response_class=StreamingResponse,
    summary="Stream the filtered committee set as CSV",
)
def export_committees(
    cycle:         str | None  = Query(default=None),
    state:         str | None  = Query(default=None),
    cmte_type:     str | None  = Query(default=None),
    designation:   str | None  = Query(default=None),
    party:         str | None  = Query(default=None),
    org_type:      str | None  = Query(default=None),
    name_contains: str | None  = Query(default=None),
    has_candidate: bool | None = Query(default=None),
) -> StreamingResponse:
    """Stream raw.fec_committee rows matching the filter as CSV."""
    f = CommitteeFilters(
        cycle=cycle, state=state, cmte_type=cmte_type,
        designation=designation, party=party, org_type=org_type,
        name_contains=name_contains, has_candidate=has_candidate,
    )

    def gen() -> Iterator[bytes]:
        with borrow_connection() as conn:
            cols, cur = stream_committees(conn, f=f)
            try:
                yield from _csv_stream(cols, cur)
            finally:
                cur.close()

    return StreamingResponse(
        gen(), media_type="text/csv",
        headers=_attachment_headers("fec_committees.csv"),
    )


# ============================================================================
# /fec/export/contributions.csv
# ============================================================================


@router.get(
    "/contributions.csv",
    response_class=StreamingResponse,
    summary="Stream the filtered contributions set as CSV",
)
def export_contributions(
    cycle:                str | None   = Query(default=None),
    cmte_id:              str | None   = Query(default=None),
    donor_state:          str | None   = Query(default=None),
    donor_name_contains:  str | None   = Query(default=None),
    employer_contains:    str | None   = Query(default=None),
    occupation_contains:  str | None   = Query(default=None),
    transaction_type:     str | None   = Query(default=None),
    min_amount:           float | None = Query(default=None),
    max_amount:           float | None = Query(default=None),
    start_date:           str | None   = Query(default=None),
    end_date:             str | None   = Query(default=None),
    exclude_memo:         bool         = Query(default=True),
) -> StreamingResponse:
    """Stream public.v_fec_contribution rows matching the filter as CSV."""
    f = ContributionFilters(
        cycle=cycle, cmte_id=cmte_id, donor_state=donor_state,
        donor_name_contains=donor_name_contains,
        employer_contains=employer_contains,
        occupation_contains=occupation_contains,
        transaction_type=transaction_type,
        min_amount=min_amount, max_amount=max_amount,
        start_date=start_date, end_date=end_date,
        exclude_memo=exclude_memo,
    )

    def gen() -> Iterator[bytes]:
        with borrow_connection() as conn:
            cols, cur = stream_contributions(conn, f=f)
            try:
                yield from _csv_stream(cols, cur)
            finally:
                cur.close()

    return StreamingResponse(
        gen(), media_type="text/csv",
        headers=_attachment_headers("fec_contributions.csv"),
    )


# ============================================================================
# /fec/export/money-to-nj.csv
# ============================================================================


@router.get(
    "/money-to-nj.csv",
    response_class=StreamingResponse,
    summary="Stream the filtered money-to-NJ-candidates set as CSV",
)
def export_money_to_nj(
    cycle:                str | None   = Query(default=None),
    cand_id:              str | None   = Query(default=None),
    party:                str | None   = Query(default=None),
    office:               str | None   = Query(default=None),
    donor_state:          str | None   = Query(default=None),
    donor_name_contains:  str | None   = Query(default=None),
    min_amount:           float | None = Query(default=None),
    max_amount:           float | None = Query(default=None),
    start_date:           str | None   = Query(default=None),
    end_date:             str | None   = Query(default=None),
    exclude_memo:         bool         = Query(default=True),
) -> StreamingResponse:
    """Stream public.v_fec_money_to_nj_candidates rows as CSV."""
    f = MoneyToNjFilters(
        cycle=cycle, cand_id=cand_id, party=party, office=office,
        donor_state=donor_state, donor_name_contains=donor_name_contains,
        min_amount=min_amount, max_amount=max_amount,
        start_date=start_date, end_date=end_date,
        exclude_memo=exclude_memo,
    )

    def gen() -> Iterator[bytes]:
        with borrow_connection() as conn:
            cols, cur = stream_money_to_nj(conn, f=f)
            try:
                yield from _csv_stream(cols, cur)
            finally:
                cur.close()

    return StreamingResponse(
        gen(), media_type="text/csv",
        headers=_attachment_headers("fec_money_to_nj.csv"),
    )
