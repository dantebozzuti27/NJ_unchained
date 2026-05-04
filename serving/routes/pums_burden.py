"""GET /pums-burden/{puma}: PUMS-derived demographic burden segmentation.

The Tier 3 analytical surface. Answers the headline question: "is X
demographic group more cost-burdened than Y in this PUMA?"

Endpoint shape
--------------
``GET /pums-burden/{puma}``
    Returns all (tenure_class, segment_dim, segment_value) cells for
    one NJ PUMA. ~25-50 rows per PUMA (3 tenures x 4 dims x ~7 values
    minus suppressed cells).

Optional filters:
    ?dim=race|hispanic|citizenship|age_band|overall
    ?tenure=renter|owner_w_mtg|owner_no_mtg
    ?include_suppressed=true|false (default: false)

Suppression default is ``include_suppressed=false`` because
suppressed cells have NULL ratios that are easy to mistake for
"this group is not burdened." Forcing the operator to opt in to
suppressed cells avoids that footgun.
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import APIRouter, HTTPException, Query, status

from serving.db import borrow_connection
from serving.models import PumsBurdenRow
from serving.queries import (
    DEFAULT_PUMS_PRODUCT,
    list_pums_burden_for_puma,
    list_pums_burden_latest,
)

router = APIRouter(tags=["pums_burden"])


_PUMA_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{5}$")

_VALID_DIMS: Final[frozenset[str]] = frozenset({
    "race", "hispanic", "citizenship", "age_band", "overall",
})

_VALID_TENURES: Final[frozenset[str]] = frozenset({
    "renter", "owner_w_mtg", "owner_no_mtg",
})

_VALID_PRODUCTS: Final[frozenset[str]] = frozenset({"acs1", "acs5"})


def _validate_product(product: str) -> str:
    if product not in _VALID_PRODUCTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid product={product!r}; expected one of "
                f"{sorted(_VALID_PRODUCTS)}. acs1 = 1-year (fresher, more "
                f"suppressed); acs5 = 5-year (larger sample, less suppressed)."
            ),
        )
    return product


@router.get(
    "/pums-burden",
    response_model=list[PumsBurdenRow],
    summary="PUMS-derived burden, latest vintage, all NJ PUMAs",
    description=(
        "Returns all derived.pums_burden_segmented rows for the "
        "latest published PUMS vintage. Filter via ?dim, ?tenure."
    ),
)
def list_pums_burden_all(
    dim: str | None = Query(
        default=None, description="Filter to one segment dimension."),
    tenure: str | None = Query(
        default=None, description="Filter to one tenure class."),
    product: str = Query(
        default=DEFAULT_PUMS_PRODUCT,
        description=(
            "ACS PUMS product. 'acs1' = 1-year (fresher data, more "
            "suppressed cells); 'acs5' = 5-year (larger sample, fewer "
            "suppressed cells). Default 'acs5' for headline analytics."
        ),
    ),
    *,
    include_suppressed: bool = Query(
        default=False,
        description="Include cells with weighted_n < 1000 (NULL ratios).",
    ),
) -> list[PumsBurdenRow]:
    """List PUMS-derived burden cells across all NJ PUMAs (latest vintage of *product*)."""
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
    _validate_product(product)
    with borrow_connection() as conn:
        rows = list_pums_burden_latest(
            conn, dim=dim, tenure=tenure,
            include_suppressed=include_suppressed, product=product,
        )
    return [PumsBurdenRow.model_validate(r) for r in rows]


@router.get(
    "/pums-burden/{puma}",
    response_model=list[PumsBurdenRow],
    summary="PUMS-derived burden segmentation for one NJ PUMA",
    description=(
        "Returns all (tenure_class, segment_dim, segment_value) cells "
        "for the requested PUMA in the latest published vintage. The "
        "BBG-terminal answer to 'who in this PUMA is being priced out'."
    ),
    responses={
        400: {"description": "Malformed PUMA code"},
        404: {"description": "No data for this PUMA in the latest vintage"},
    },
)
def get_pums_burden(
    puma: str,
    dim: str | None = Query(default=None),
    tenure: str | None = Query(default=None),
    product: str = Query(default=DEFAULT_PUMS_PRODUCT),
    *,
    include_suppressed: bool = Query(default=False),
) -> list[PumsBurdenRow]:
    """List PUMS-derived burden cells for one PUMA (latest vintage of *product*)."""
    if not _PUMA_RE.match(puma):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid PUMA code {puma!r}; expected 5 digits.",
        )
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
    _validate_product(product)
    with borrow_connection() as conn:
        rows = list_pums_burden_for_puma(
            conn, puma=puma, dim=dim, tenure=tenure,
            include_suppressed=include_suppressed, product=product,
        )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No PUMS burden data for PUMA {puma!r}.",
        )
    return [PumsBurdenRow.model_validate(r) for r in rows]
