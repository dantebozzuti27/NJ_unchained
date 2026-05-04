"""GET /pums-burden-county/{county_fips}: PUMS-derived burden at county grain.

The county-grain mirror of /pums-burden. Backed by
``derived.pums_burden_county_segmented`` -- re-aggregated from raw
PUMS via the population-weighted PUMA-county crosswalk
(``ref.puma2020_county_xwalk``), NOT rolled up from the PUMA-grain
table (median-of-medians is statistically invalid).

Endpoint shape
--------------
``GET /pums-burden-county``
    Returns all (county, tenure, segment_dim, segment_value) cells for
    the latest published vintage across all 21 NJ counties. ~1500-2000
    rows total (21 counties x 3 tenures x ~25 segment values minus
    suppressed cells).

``GET /pums-burden-county/{county_fips}``
    Returns the cells for one NJ county. ~70-100 rows.

Why a separate endpoint vs. a roll-up via PUMA?
-----------------------------------------------
Two reasons.

(1) Methodologically: the median of (PUMA-1 median, PUMA-2 median)
    is NOT the median of the combined population. To get a
    statistically valid county median, you must aggregate the
    underlying weighted observations together -- which is what the
    derived table does. Rolling up the PUMA medians here would mean
    showing wrong numbers.

(2) Practically: county is the grain consumers care about. PUMA is
    a Census artifact. Most readers do not know the PUMA they live
    in but everyone knows their county.
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import APIRouter, HTTPException, Query, status

from serving.db import borrow_connection
from serving.models import PumsBurdenCountyRow
from serving.queries import (
    DEFAULT_PUMS_PRODUCT,
    list_pums_burden_county_for_county,
    list_pums_burden_county_latest,
)

router = APIRouter(tags=["pums_burden_county"])


_COUNTY_FIPS_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{5}$")

_VALID_DIMS: Final[frozenset[str]] = frozenset({
    "race", "hispanic", "citizenship", "age_band", "overall",
})

_VALID_TENURES: Final[frozenset[str]] = frozenset({
    "renter", "owner_w_mtg", "owner_no_mtg",
})

_VALID_PRODUCTS: Final[frozenset[str]] = frozenset({"acs1", "acs5"})


def _validate_filters(
    *, dim: str | None, tenure: str | None, product: str,
) -> None:
    """Reject malformed dim/tenure/product with 400. Raises HTTPException."""
    if dim is not None and dim not in _VALID_DIMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid dim={dim!r}; expected one of {sorted(_VALID_DIMS)}"
            ),
        )
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


@router.get(
    "/pums-burden-county",
    response_model=list[PumsBurdenCountyRow],
    summary="PUMS-derived burden at COUNTY grain, latest vintage, all NJ counties",
    description=(
        "Returns all derived.pums_burden_county_segmented rows for the "
        "latest published PUMS vintage. Filter via ?dim, ?tenure. "
        "Suppressed cells (weighted_n < 1000) excluded by default."
    ),
)
def list_pums_burden_county_all(
    dim: str | None = Query(
        default=None, description="Filter to one segment dimension."),
    tenure: str | None = Query(
        default=None, description="Filter to one tenure class."),
    product: str = Query(
        default=DEFAULT_PUMS_PRODUCT,
        description="acs1 = 1-year (fresh, suppressed); acs5 = 5-year (default).",
    ),
    *,
    include_suppressed: bool = Query(
        default=False,
        description="Include cells with weighted_n < 1000 (NULL ratios).",
    ),
) -> list[PumsBurdenCountyRow]:
    """List county-grain PUMS burden cells across all NJ counties (latest of *product*)."""
    _validate_filters(dim=dim, tenure=tenure, product=product)
    with borrow_connection() as conn:
        rows = list_pums_burden_county_latest(
            conn, dim=dim, tenure=tenure,
            include_suppressed=include_suppressed, product=product,
        )
    return [PumsBurdenCountyRow.model_validate(r) for r in rows]


@router.get(
    "/pums-burden-county/{county_fips}",
    response_model=list[PumsBurdenCountyRow],
    summary="PUMS-derived burden at COUNTY grain for one NJ county",
    description=(
        "Returns all (tenure_class, segment_dim, segment_value) cells "
        "for the requested county in the latest published vintage. "
        "The county-grain answer to 'who in this county is being "
        "priced out'."
    ),
    responses={
        400: {"description": "Malformed county_fips code"},
        404: {"description": "No data for this county in the latest vintage"},
    },
)
def get_pums_burden_county(
    county_fips: str,
    dim: str | None = Query(default=None),
    tenure: str | None = Query(default=None),
    product: str = Query(default=DEFAULT_PUMS_PRODUCT),
    *,
    include_suppressed: bool = Query(default=False),
) -> list[PumsBurdenCountyRow]:
    """List county-grain PUMS burden cells for one county (latest vintage of *product*)."""
    if not _COUNTY_FIPS_RE.match(county_fips):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid county_fips {county_fips!r}; expected 5 digits.",
        )
    _validate_filters(dim=dim, tenure=tenure, product=product)
    with borrow_connection() as conn:
        rows = list_pums_burden_county_for_county(
            conn, county_fips=county_fips, dim=dim, tenure=tenure,
            include_suppressed=include_suppressed, product=product,
        )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No PUMS burden data for county_fips {county_fips!r}.",
        )
    return [PumsBurdenCountyRow.model_validate(r) for r in rows]
