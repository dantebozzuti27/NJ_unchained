"""FEC (civic-integrity / fraud) read API.

Eleven endpoints under ``/fec/*``:

* ``/fec/summary``                 -- cross-table snapshot for the dashboard
* ``/fec/cycles``                  -- distinct cycles
* ``/fec/states``                  -- distinct cand_office_st values
* ``/fec/parties``                 -- distinct cand_pty_affiliation values
* ``/fec/offices``                 -- distinct cand_office values (H/S/P)
* ``/fec/candidates``              -- filterable + paginated list
* ``/fec/candidates/{cand_id}``    -- detail + linked committees
* ``/fec/committees``              -- filterable + paginated list
* ``/fec/committees/{cmte_id}``    -- detail + candidate + recent contribs
* ``/fec/contributions``           -- filterable + paginated list
                                      (against public.v_fec_contribution)
* ``/fec/money-to-nj``             -- headline view, filterable + paginated

Every list endpoint returns a :class:`FecPagedResponse` envelope with
total_count, so the UI can render pagination without a second
network round-trip.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Query

from serving.db import borrow_connection
from serving.models import (
    FecCandidateDetail,
    FecCandidateRow,
    FecCommitteeDetail,
    FecCommitteeRow,
    FecContributionRow,
    FecEnumValue,
    FecMoneyToNjRow,
    FecPagedResponse,
    FecSummary,
)
from serving.queries_fec import (
    CandidateFilters,
    CommitteeFilters,
    ContributionFilters,
    MoneyToNjFilters,
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
)

router = APIRouter(prefix="/fec", tags=["fec"])


# ============================================================================
# /fec/summary -- header snapshot
# ============================================================================


@router.get(
    "/summary",
    response_model=FecSummary,
    summary="Cross-table snapshot for the fraud-UI dashboard header",
    description=(
        "Returns counts for the most recent loaded cycle: candidates "
        "(total + NJ), committees (total + NJ-domiciled), contributions "
        "(total + NJ-donor + to-NJ-candidate). Returns zeros + empty "
        "cycle when no FEC data has been loaded yet so the UI can "
        "render a clean empty state."
    ),
)
def get_fec_summary() -> FecSummary:
    """Return the fraud-UI dashboard summary."""
    with borrow_connection() as conn:
        snapshot = get_summary(conn)
    return FecSummary.model_validate(snapshot)


# ============================================================================
# /fec/cycles, /fec/states, /fec/parties, /fec/offices -- enum dropdowns
# ============================================================================


@router.get(
    "/cycles",
    response_model=list[FecEnumValue],
    summary="Distinct cycles available in raw.fec_candidate (DESC)",
)
def get_cycles() -> list[FecEnumValue]:
    """Return the distinct cycles for filter dropdowns."""
    with borrow_connection() as conn:
        rows = list_distinct_cycles(conn)
    return [FecEnumValue.model_validate(r) for r in rows]


@router.get(
    "/states",
    response_model=list[FecEnumValue],
    summary="Distinct cand_office_st values (optionally cycle-scoped)",
)
def get_states(
    cycle: str | None = Query(default=None, examples=["2024"]),
) -> list[FecEnumValue]:
    """Distinct candidate office states (NJ, NY, ...)."""
    with borrow_connection() as conn:
        rows = list_distinct_states(conn, cycle=cycle)
    return [FecEnumValue.model_validate(r) for r in rows]


@router.get(
    "/parties",
    response_model=list[FecEnumValue],
    summary="Distinct cand_pty_affiliation values (optionally cycle-scoped)",
)
def get_parties(
    cycle: str | None = Query(default=None),
) -> list[FecEnumValue]:
    """Distinct party affiliations (DEM, REP, IND, ...)."""
    with borrow_connection() as conn:
        rows = list_distinct_parties(conn, cycle=cycle)
    return [FecEnumValue.model_validate(r) for r in rows]


@router.get(
    "/offices",
    response_model=list[FecEnumValue],
    summary="Distinct cand_office values (H, S, P)",
)
def get_offices(
    cycle: str | None = Query(default=None),
) -> list[FecEnumValue]:
    """Distinct office codes (H=House, S=Senate, P=President)."""
    with borrow_connection() as conn:
        rows = list_distinct_offices(conn, cycle=cycle)
    return [FecEnumValue.model_validate(r) for r in rows]


# ============================================================================
# /fec/candidates
# ============================================================================


@router.get(
    "/candidates",
    response_model=FecPagedResponse,
    summary="Filterable + paginated candidate list",
)
def get_candidates(
    cycle:         str | None = Query(default=None, examples=["2024"]),
    state:         str | None = Query(default=None, examples=["NJ"]),
    office:        str | None = Query(default=None, examples=["S"]),
    party:         str | None = Query(default=None, examples=["DEM"]),
    incumbent:     str | None = Query(default=None, examples=["I"]),
    status:        str | None = Query(default=None, examples=["C"]),
    name_contains: str | None = Query(default=None, examples=["BOOKER"]),
    sort_by:       str | None = Query(default="cand_name"),
    sort_dir:      str | None = Query(default="ASC", pattern="^(?i)(asc|desc)$"),
    limit:         int        = Query(default=100, ge=1, le=1000),
    offset:        int        = Query(default=0,   ge=0),
) -> FecPagedResponse:
    """Return one page of raw.fec_candidate rows with total_count."""
    f = CandidateFilters(
        cycle=cycle, state=state, office=office, party=party,
        incumbent=incumbent, status=status, name_contains=name_contains,
    )
    with borrow_connection() as conn:
        rows, total = list_candidates(
            conn, f=f, sort_by=sort_by, sort_dir=sort_dir,
            limit=limit, offset=offset,
        )
    return FecPagedResponse(
        rows=[FecCandidateRow.model_validate(r).model_dump() for r in rows],
        total_count=total, limit=limit, offset=offset,
    )


@router.get(
    "/candidates/{cand_id}",
    response_model=FecCandidateDetail,
    summary="Single candidate + linked committees",
    responses={404: {"description": "Candidate not found"}},
)
def get_candidate(
    cand_id: str = Path(..., examples=["S4NJ00466"]),
    cycle:   str | None = Query(default=None, examples=["2024"]),
) -> FecCandidateDetail:
    """Return one candidate + every committee it has filed for in *cycle*.

    If *cycle* is omitted, picks the most recent cycle the candidate
    appears in.
    """
    with borrow_connection() as conn:
        row = get_candidate_detail(conn, cand_id=cand_id, cycle=cycle)
    if row is None:
        raise HTTPException(status_code=404, detail=f"candidate {cand_id} not found")
    committees_raw = row.pop("linked_committees", []) or []
    committees = [FecCommitteeRow.model_validate(c) for c in committees_raw]
    return FecCandidateDetail.model_validate({
        **row, "linked_committees": committees,
    })


# ============================================================================
# /fec/committees
# ============================================================================


@router.get(
    "/committees",
    response_model=FecPagedResponse,
    summary="Filterable + paginated committee list",
)
def get_committees(
    cycle:          str | None  = Query(default=None),
    state:          str | None  = Query(default=None, examples=["NJ"]),
    cmte_type:      str | None  = Query(default=None, examples=["P"]),
    designation:    str | None  = Query(default=None, examples=["P"]),
    party:          str | None  = Query(default=None, examples=["DEM"]),
    org_type:       str | None  = Query(default=None),
    name_contains:  str | None  = Query(default=None),
    has_candidate:  bool | None = Query(default=None),
    sort_by:        str | None  = Query(default="cmte_nm"),
    sort_dir:       str | None  = Query(default="ASC", pattern="^(?i)(asc|desc)$"),
    limit:          int         = Query(default=100, ge=1, le=1000),
    offset:         int         = Query(default=0,   ge=0),
) -> FecPagedResponse:
    """Return one page of raw.fec_committee rows with total_count."""
    f = CommitteeFilters(
        cycle=cycle, state=state, cmte_type=cmte_type,
        designation=designation, party=party, org_type=org_type,
        name_contains=name_contains, has_candidate=has_candidate,
    )
    with borrow_connection() as conn:
        rows, total = list_committees(
            conn, f=f, sort_by=sort_by, sort_dir=sort_dir,
            limit=limit, offset=offset,
        )
    return FecPagedResponse(
        rows=[FecCommitteeRow.model_validate(r).model_dump(by_alias=False) for r in rows],
        total_count=total, limit=limit, offset=offset,
    )


@router.get(
    "/committees/{cmte_id}",
    response_model=FecCommitteeDetail,
    summary="Single committee + linked candidate (if any) + 25 recent contribs",
    responses={404: {"description": "Committee not found"}},
)
def get_committee(
    cmte_id: str = Path(..., examples=["C00540500"]),
    cycle:   str | None = Query(default=None),
) -> FecCommitteeDetail:
    """Return one committee with affiliated candidate + recent contributions."""
    with borrow_connection() as conn:
        row = get_committee_detail(conn, cmte_id=cmte_id, cycle=cycle)
    if row is None:
        raise HTTPException(status_code=404, detail=f"committee {cmte_id} not found")
    cand_raw = row.pop("linked_candidate", None)
    contribs_raw = row.pop("recent_contributions", []) or []
    return FecCommitteeDetail.model_validate({
        **row,
        "linked_candidate": (
            FecCandidateRow.model_validate(cand_raw) if cand_raw else None
        ),
        "recent_contributions": [
            FecContributionRow.model_validate(c) for c in contribs_raw
        ],
    })


# ============================================================================
# /fec/contributions
# ============================================================================


@router.get(
    "/contributions",
    response_model=FecPagedResponse,
    summary=(
        "Filterable + paginated individual-contribution list "
        "(public.v_fec_contribution; MEMO_CD='X' rows excluded by default)"
    ),
)
def get_contributions(
    cycle:                str | None   = Query(default=None),
    cmte_id:              str | None   = Query(default=None),
    donor_state:          str | None   = Query(default=None, examples=["NJ"]),
    donor_name_contains:  str | None   = Query(default=None),
    employer_contains:    str | None   = Query(default=None),
    occupation_contains:  str | None   = Query(default=None),
    transaction_type:     str | None   = Query(default=None),
    min_amount:           float | None = Query(default=None),
    max_amount:           float | None = Query(default=None),
    start_date:           str | None   = Query(default=None, examples=["2024-01-01"]),
    end_date:             str | None   = Query(default=None, examples=["2024-12-31"]),
    exclude_memo:         bool         = Query(default=True),
    sort_by:              str | None   = Query(default="transaction_date"),
    sort_dir:             str | None   = Query(default="DESC", pattern="^(?i)(asc|desc)$"),
    limit:                int          = Query(default=100, ge=1, le=1000),
    offset:               int          = Query(default=0,   ge=0),
) -> FecPagedResponse:
    """Return one page of public.v_fec_contribution rows with total_count."""
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
    with borrow_connection() as conn:
        rows, total = list_contributions(
            conn, f=f, sort_by=sort_by, sort_dir=sort_dir,
            limit=limit, offset=offset,
        )
    return FecPagedResponse(
        rows=[FecContributionRow.model_validate(r).model_dump() for r in rows],
        total_count=total, limit=limit, offset=offset,
    )


# ============================================================================
# /fec/money-to-nj
# ============================================================================


@router.get(
    "/money-to-nj",
    response_model=FecPagedResponse,
    summary=(
        "Filterable + paginated rows of public.v_fec_money_to_nj_candidates "
        "(every contribution to a committee affiliated with a NJ candidate)"
    ),
)
def get_money_to_nj(
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
    sort_by:              str | None   = Query(default="transaction_date"),
    sort_dir:             str | None   = Query(default="DESC", pattern="^(?i)(asc|desc)$"),
    limit:                int          = Query(default=100, ge=1, le=1000),
    offset:               int          = Query(default=0,   ge=0),
) -> FecPagedResponse:
    """Return one page of the headline NJ-money view with total_count."""
    f = MoneyToNjFilters(
        cycle=cycle, cand_id=cand_id, party=party, office=office,
        donor_state=donor_state, donor_name_contains=donor_name_contains,
        min_amount=min_amount, max_amount=max_amount,
        start_date=start_date, end_date=end_date,
        exclude_memo=exclude_memo,
    )
    with borrow_connection() as conn:
        rows, total = list_money_to_nj(
            conn, f=f, sort_by=sort_by, sort_dir=sort_dir,
            limit=limit, offset=offset,
        )
    return FecPagedResponse(
        rows=[FecMoneyToNjRow.model_validate(r).model_dump() for r in rows],
        total_count=total, limit=limit, offset=offset,
    )
