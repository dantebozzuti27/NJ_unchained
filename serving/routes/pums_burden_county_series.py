"""GET /pums-burden-county-series: multi-year time-series for the UI.

Sibling to /pums-burden-county that returns ALL years (not just the
latest) for the segment_dim='overall' rollup. This is the read surface
the front-end uses to draw a county's burden-ratio trajectory.

Endpoint shape
--------------
``GET /pums-burden-county-series``
    Returns one row per (year, product, county_fips, tenure_class) for
    the overall segment. Filter via ``?tenure``, ``?county_fips``,
    ``?product``. With no filters: ~5 years * 21 counties * 3 tenures
    = ~315 rows for acs5, ~252 rows for acs1 (acs1 has no 2020 data).

Why a separate endpoint vs. a ?include_history flag on /pums-burden-county
-------------------------------------------------------------------------
The existing /pums-burden-county endpoint pins to the latest (year,
product) tuple via a ``WITH latest`` CTE. Adding a history flag would
fork its query path and double its test surface. A dedicated endpoint
with a tighter response shape (no per-segment columns) is simpler to
reason about and cheaper on the wire for the UI's most common request.
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import APIRouter, HTTPException, Query, status

from serving.db import borrow_connection
from serving.models import PumsBurdenCountySeriesRow
from serving.queries import (
    DEFAULT_PUMS_PRODUCT,
    list_pums_burden_county_series,
)

router = APIRouter(tags=["pums_burden_county_series"])


_COUNTY_FIPS_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{5}$")

_VALID_TENURES: Final[frozenset[str]] = frozenset({
    "renter", "owner_w_mtg", "owner_no_mtg",
})

_VALID_PRODUCTS: Final[frozenset[str]] = frozenset({"acs1", "acs5"})


@router.get(
    "/pums-burden-county-series",
    response_model=list[PumsBurdenCountySeriesRow],
    summary="Multi-year time series of county burden ratio (overall segment)",
    description=(
        "Returns one row per (year, product, county, tenure) for the "
        "segment_dim='overall' rollup, across ALL available years. "
        "Front-end consumers group by (county_fips, tenure_class) and "
        "plot each group as a line with 1.645*SE error bands."
    ),
    responses={
        400: {"description": "Malformed tenure / product / county_fips"},
    },
)
def get_pums_burden_county_series(
    tenure: str | None = Query(
        default=None,
        description="Filter to one tenure class.",
    ),
    county_fips: str | None = Query(
        default=None,
        description="Filter to one NJ county (5-digit FIPS).",
    ),
    product: str = Query(
        default=DEFAULT_PUMS_PRODUCT,
        description="acs1 = 1-year (fresh, suppressed); acs5 = 5-year (default).",
    ),
    *,
    include_suppressed: bool = Query(
        default=False,
        description="Include cells with weighted_n < 1000 (NULL ratios).",
    ),
) -> list[PumsBurdenCountySeriesRow]:
    """List multi-year overall-burden series for NJ counties."""
    if tenure is not None and tenure not in _VALID_TENURES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid tenure={tenure!r}; expected one of "
                f"{sorted(_VALID_TENURES)}"
            ),
        )
    if product not in _VALID_PRODUCTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid product={product!r}; expected one of "
                f"{sorted(_VALID_PRODUCTS)}."
            ),
        )
    if county_fips is not None and not _COUNTY_FIPS_RE.match(county_fips):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid county_fips {county_fips!r}; expected 5 digits.",
        )
    with borrow_connection() as conn:
        rows = list_pums_burden_county_series(
            conn,
            tenure=tenure,
            county_fips=county_fips,
            product=product,
            include_suppressed=include_suppressed,
        )
    return [PumsBurdenCountySeriesRow.model_validate(r) for r in rows]
