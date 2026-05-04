"""GET /burden and /burden/{county_fips}: housing burden ratio queries."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, status

from serving.db import borrow_connection
from serving.models import BurdenRow
from serving.queries import (
    list_burden_for_county,
    list_burden_latest_year_nj,
)

router = APIRouter(tags=["burden"])


# NJ FIPS prefix is "34"; county portion is 3 digits. Strict validation
# at the route boundary rejects malformed inputs before they touch SQL.
_NJ_FIPS_RE = re.compile(r"^34\d{3}$")


@router.get(
    "/burden",
    response_model=list[BurdenRow],
    summary="Housing burden ratio: latest year, all 21 NJ counties",
)
def get_burden_latest() -> list[BurdenRow]:
    """Serve the dashboard query: most recent year, every NJ county."""
    with borrow_connection() as conn:
        rows = list_burden_latest_year_nj(conn)
    return [BurdenRow.model_validate(r) for r in rows]


@router.get(
    "/burden/{county_fips}",
    response_model=list[BurdenRow],
    summary="Housing burden ratio time series for one NJ county",
    responses={
        400: {"description": "Malformed county FIPS"},
        404: {"description": "No burden data for this county"},
    },
)
def get_burden_county(county_fips: str) -> list[BurdenRow]:
    """Time series for a single county (ACS 5-yr).

    Returns rows in chronological order. 404 if the county has no
    burden rows (suppressed estimates, or county not in NJ).
    """
    if not _NJ_FIPS_RE.match(county_fips):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid NJ county FIPS {county_fips!r}; "
                "expected 5 digits prefixed with '34'."
            ),
        )
    with borrow_connection() as conn:
        rows = list_burden_for_county(conn, county_fips=county_fips)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No housing burden data for county {county_fips!r}",
        )
    return [BurdenRow.model_validate(r) for r in rows]
