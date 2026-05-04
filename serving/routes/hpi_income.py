"""GET /hpi/{county_fips}/series and /income/{county_fips}/series.

Two read surfaces over the existing ``derived.f_fhfa_hpi_indexed`` and
``derived.f_acs_mhi_real`` SQL functions. Both share the same NJ FIPS
validation and 404-on-empty contract as :mod:`serving.routes.burden`,
so the dashboard can plot HPI vs. income vs. burden on a common
county axis without per-endpoint quirks.

Why a single module: HPI and income are two faces of the same
"county-year time series" UI surface. Keeping them together avoids
the temptation to drift their pagination, validation, or error
contracts apart. They have NO shared SQL though -- each calls a
distinct SQL function -- so coupling here is purely the route shape.

The HPI base year defaults to 2000 (the project canonical, see
:data:`serving.queries.HPI_DEFAULT_BASE_YEAR`). The income base year
defaults to ``min(max(dollar_year), max(cpi_year))`` -- the most
recent year for which CPI deflation is computable end-to-end.
Computed at request time by :func:`resolve_default_income_base_year`
because both inputs change with each ingest.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query, status

from serving.db import borrow_connection
from serving.models import HpiCountyRow, IncomeCountyRow
from serving.queries import (
    HPI_DEFAULT_BASE_YEAR,
    list_hpi_county_series,
    list_income_county_series,
    resolve_default_income_base_year,
)

router = APIRouter(tags=["hpi", "income"])


# Same NJ-only FIPS gate as /burden. Centralising the regex would couple
# routes that have no other reason to share a module; one line here is
# the cheaper trade-off.
_NJ_FIPS_RE = re.compile(r"^34\d{3}$")

# Acceptable FHFA HPI base years. The series starts in 1975 nationally;
# the upper bound is intentionally loose (FHFA publishes through the most
# recent quarter, but year-grain rebasing past 2030 is implausible enough
# that we cut it off there to surface typos like "2099" as 400s, not as
# silent zero-row responses).
_MIN_HPI_BASE_YEAR = 1975
_MAX_HPI_BASE_YEAR = 2030

# Acceptable ACS income base years. ACS B19013 starts in 2005; the
# CPI table goes back further but the deflator only matters where ACS
# data exists. Same logic for the upper bound as HPI.
_MIN_INCOME_BASE_YEAR = 2005
_MAX_INCOME_BASE_YEAR = 2030

_VALID_INCOME_PRODUCTS = frozenset({"acs1", "acs5"})


@router.get(
    "/hpi/{county_fips}/series",
    response_model=list[HpiCountyRow],
    summary="FHFA HPI annual time series for one NJ county, re-indexed to base_year.",
    responses={
        400: {"description": "Malformed FIPS or out-of-range base_year."},
        404: {"description": "No HPI rows for this county at this base_year."},
    },
)
def get_hpi_county_series(
    county_fips: str,
    base_year: int = Query(
        default=HPI_DEFAULT_BASE_YEAR,
        ge=_MIN_HPI_BASE_YEAR,
        le=_MAX_HPI_BASE_YEAR,
        description=(
            "Year for which the indexed value should equal 100.000. "
            f"Default {HPI_DEFAULT_BASE_YEAR}."
        ),
    ),
) -> list[HpiCountyRow]:
    """Time series of FHFA HPI for one NJ county, base-year normalized.

    Returns rows in chronological order. 404 when the county has no
    HPI rows at this base year (typically: county exists but FHFA's
    series for it does not include the base year).
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
        rows = list_hpi_county_series(
            conn, county_fips=county_fips, base_year=base_year,
        )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No FHFA HPI series for county {county_fips!r} "
                f"at base_year={base_year}."
            ),
        )
    return [HpiCountyRow.model_validate(r) for r in rows]


@router.get(
    "/income/{county_fips}/series",
    response_model=list[IncomeCountyRow],
    summary="ACS B19013 median household income, CPI-deflated to base_year dollars.",
    responses={
        400: {"description": "Malformed FIPS, bad product, or out-of-range base_year."},
        404: {"description": "No income rows for this county/product at this base_year."},
    },
)
def get_income_county_series(
    county_fips: str,
    product: str = Query(
        default="acs5",
        description="ACS product backing the series. 'acs5' (default) or 'acs1'.",
    ),
    base_year: int | None = Query(
        default=None,
        ge=_MIN_INCOME_BASE_YEAR,
        le=_MAX_INCOME_BASE_YEAR,
        description=(
            "Deflate to this year's dollars. Default: the most recent "
            "year for which both ACS dollar_year and CPI are available."
        ),
    ),
) -> list[IncomeCountyRow]:
    """Time series of CPI-deflated ACS median household income.

    The default base year is computed at request time from the data
    itself (not pinned to a magic number) so the series stays in
    "today's dollars" as new vintages land. 404 when the requested
    product has nothing for this county at the chosen base year.
    """
    if not _NJ_FIPS_RE.match(county_fips):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid NJ county FIPS {county_fips!r}; "
                "expected 5 digits prefixed with '34'."
            ),
        )
    if product not in _VALID_INCOME_PRODUCTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid product {product!r}; "
                f"expected one of {sorted(_VALID_INCOME_PRODUCTS)}."
            ),
        )
    with borrow_connection() as conn:
        eff_base = base_year
        if eff_base is None:
            eff_base = resolve_default_income_base_year(conn)
            if eff_base is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        "No ACS income or CPI data is loaded; cannot "
                        "compute a default deflation base year."
                    ),
                )
        rows = list_income_county_series(
            conn,
            county_fips=county_fips,
            base_year=eff_base,
            product=product,
        )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No ACS income series for county {county_fips!r} "
                f"product={product!r} at base_year={eff_base}."
            ),
        )
    return [IncomeCountyRow.model_validate(r) for r in rows]
