"""Pydantic v2 response models for the serving API.

Every endpoint returns one of these. Do NOT return raw dicts.
The OpenAPI schema generated from these models is the contract.

Design rules
------------
* Every model has a stable ``model_config = ConfigDict(...)`` with
  ``str_strip_whitespace=True`` and ``frozen=True``. Frozen prevents
  accidental mutation post-construction (pure data transfer objects).
* Every model is exported in __all__ for discoverability.
* Field documentation uses Pydantic's ``Field(description=...)`` so
  it lands in the OpenAPI schema.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 -- pydantic resolves annotations at runtime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AssetDetail",
    "AssetSummary",
    "BurdenRow",
    "CountyRef",
    "DatasetHealthSummary",
    "FecCandidateDetail",
    "FecCandidateRow",
    "FecCommitteeDetail",
    "FecCommitteeRow",
    "FecContributionRow",
    "FecEnumValue",
    "FecMoneyToNjRow",
    "FecPagedResponse",
    "FecSummary",
    "FraudMetricCatalogEntry",
    "FraudMetricResult",
    "FraudMetricSummary",
    "Health",
    "HpiCountyRow",
    "IncomeCountyRow",
    "PumsBurdenCountyRow",
    "PumsBurdenCountySeriesRow",
    "PumsBurdenRow",
    "ReleaseCalendarHorizonRow",
    "ReleaseCalendarPanel",
    "ReleaseCalendarRow",
    "RiskEntityPanel",
    "RiskQueueResponse",
    "RiskQueueRow",
    "RiskSignalObservation",
]


_FROZEN = ConfigDict(str_strip_whitespace=True, frozen=True)


# ============================================================================
# /health
# ============================================================================


class Health(BaseModel):
    """Liveness + dependency state for the serving API."""

    model_config = _FROZEN

    status:           str           = Field(..., description="ok|degraded")
    db_reachable:     bool          = Field(..., description="Did SELECT 1 succeed?")
    n_errors_last_1h: int           = Field(
        ...,
        description=(
            "Count of governance.dataset_health rows with severity in "
            "(error, fatal) within the last 1 hour. > 0 => degraded."
        ),
    )
    api_version:      str           = Field(default="0.1.0")
    timestamp:        dt.datetime   = Field(...)


# ============================================================================
# /releases
# ============================================================================


class ReleaseCalendarRow(BaseModel):
    """One row of ref.release_calendar (the BBG ECO<GO> equivalent)."""

    model_config = _FROZEN

    source_id:          str           = Field(..., examples=["raw.fred_observation"])
    cadence:            str           = Field(..., examples=["weekly"])
    schedule_label:     str           = Field(..., examples=["Thursdays at 12:00 ET"])
    timezone:           str
    expected_lag_hours: int
    notes:              str | None    = None


class ReleaseCalendarHorizonRow(BaseModel):
    """Calendar entry plus freshness and computed upcoming release instants."""

    model_config = _FROZEN

    source_id:          str           = Field(..., examples=["raw.fred_observation"])
    cadence:            str
    schedule_label:     str
    timezone:           str
    expected_lag_hours: int
    notes:              str | None    = None
    last_materialized_at: dt.datetime | None = None
    age_hours:          float | None  = None
    freshness_state:    str
    overdue:            bool = Field(
        ...,
        description="Same as materialization older than expected_lag_hours (stale).",
    )
    upcoming_releases: list[dt.datetime] = Field(
        ...,
        description="UTC timestamps within [as_of, as_of + horizon_days] when computable.",
    )
    next_expected_at: dt.datetime | None = Field(
        ...,
        description="Next scheduled UTC instant after as_of; may fall outside the horizon.",
    )
    schedule_computed: bool = Field(
        ...,
        description="False for on_event rows or when structured schedule fields are insufficient.",
    )


class ReleaseCalendarPanel(BaseModel):
    """``GET /release-calendar`` envelope: horizon parameters + per-source rows."""

    model_config = _FROZEN

    as_of:         dt.datetime = Field(..., description="Server UTC instant used for the window.")
    horizon_days:  int         = Field(..., ge=1, description="Half-open window length in days.")
    sources:       list[ReleaseCalendarHorizonRow]


# ============================================================================
# /assets
# ============================================================================


class AssetSummary(BaseModel):
    """One row of the /assets list endpoint.

    Joins ref.release_calendar (cadence/schedule) with
    governance.v_latest_materialization (last refresh) and
    governance.v_dataset_health_summary (last 30d signals). Computes a
    derived freshness state in code, NOT in SQL, because the lag
    budget lives in the orchestration package.
    """

    model_config = _FROZEN

    dataset_id:           str           = Field(..., examples=["raw.fred_observation"])
    cadence:              str | None    = None
    schedule_label:       str | None    = None
    expected_lag_hours:   int | None    = None
    last_materialized_at: dt.datetime | None = None
    last_rows_upserted:   int | None    = None
    age_hours:            float | None  = None
    freshness_state:      str           = Field(
        ...,
        description=(
            "fresh | stale | unknown. 'fresh' = age < expected_lag_hours; "
            "'stale' = age >= expected_lag_hours; 'unknown' = no calendar "
            "or no materialization observed yet."
        ),
    )
    n_warn_30d:           int           = 0
    n_error_30d:          int           = 0


class DatasetHealthSummary(BaseModel):
    """Last 30 days of signal counts for one dataset."""

    model_config = _FROZEN

    dataset_id:    str
    n_signals_30d: int
    n_info_30d:    int
    n_warn_30d:    int
    n_error_30d:   int
    last_warn_at:  dt.datetime | None = None
    last_error_at: dt.datetime | None = None


class AssetDetail(AssetSummary):
    """/assets/{schema}/{table}: AssetSummary + last materialization payload."""

    model_config = _FROZEN

    last_materialization_details: dict[str, Any] | None = Field(
        default=None,
        description="The JSONB `details` payload from the most recent "
                    "'materialized' signal: per-series row counts, fetch "
                    "windows, source vintages, content fingerprints.",
    )


# ============================================================================
# /burden
# ============================================================================


class BurdenRow(BaseModel):
    """One row of the housing burden time-series query.

    Mirrors public.v_housing_burden_nj_5yr. Property-tax context
    columns (property_tax_*) are NULL for years before NJ DCA's
    2016 coverage starts.

    NOTE: ACS B25088/B25089 already include property tax in owner
    cost, so the burden RATIOS already account for it. The
    property_tax_* columns are informational/transparency only --
    they let consumers see the dollar share of owner cost that is
    property tax, NOT a re-computation of the burden ratio. See
    migration 032 for the analytic rationale.
    """

    model_config = _FROZEN

    county_fips:                            str
    county_name:                            str
    year:                                   int
    household_income:                       int | None
    median_gross_rent:                      int | None
    median_owner_cost_w_mtg:                int | None
    renter_burden_ratio:                    float | None
    owner_burden_w_mtg_ratio:               float | None
    owner_burden_no_mtg_ratio:              float | None
    blended_burden_ratio:                   float | None
    # NJ DCA property-tax context (informational only).
    property_tax_amount_avg:                int | None    = None
    property_tax_effective_rate_pct:        float | None  = None
    property_tax_share_of_income:           float | None  = None
    property_tax_share_of_owner_cost_w_mtg: float | None  = None


class PumsBurdenRow(BaseModel):
    """One cell of derived.pums_burden_segmented.

    A cell is uniquely identified by (year, product, puma, tenure_class,
    segment_dim, segment_value). The metric columns are:

      * weighted_n: estimated population in the cell (sum of person
        sampling weights). The denominator for any "X% of group are
        burdened" calculation.
      * sample_n: un-weighted respondent count. For consumer
        transparency about how many real respondents the cell summarizes.
      * household_income_p50, monthly_cost_p50: weighted median income
        and weighted median monthly housing cost (rent for renters,
        SMOCP for owners).
      * burden_ratio_p50: (monthly_cost_p50 * 12) / household_income_p50.
        Annualized housing cost as fraction of income. Per HUD,
        >= 0.30 is "cost burdened", >= 0.50 is "severely cost burdened".
      * suppressed: TRUE if weighted_n < 1000. When suppressed, the
        ratio columns are NULL.
    """

    model_config = _FROZEN

    year:                  int
    product:               str
    state_fips:            str
    puma:                  str
    tenure_class:          str
    segment_dim:           str
    segment_value:         str
    weighted_n:               int
    sample_n:                  int
    household_income_p50:      float | None = None
    household_income_p50_se:   float | None = Field(
        default=None,
        description=(
            "SDR-based standard error of household_income_p50. NULL when "
            "the point estimate is NULL or when too few replicates "
            "produced a finite estimate. 90% CI = p50 +/- 1.645 * se."
        ),
    )
    monthly_cost_p50:          float | None = None
    monthly_cost_p50_se:       float | None = Field(
        default=None,
        description="SDR-based standard error of monthly_cost_p50.",
    )
    burden_ratio_p50:          float | None = None
    burden_ratio_p50_se:       float | None = Field(
        default=None,
        description=(
            "SDR-based standard error of burden_ratio_p50. Computed by "
            "recomputing the ratio under each replicate weight, NOT by "
            "delta-method approximation -- so it correctly reflects the "
            "joint variance of numerator and denominator."
        ),
    )
    suppressed:                bool


class PumsBurdenCountyRow(BaseModel):
    """One cell of derived.pums_burden_county_segmented.

    Same shape as PumsBurdenRow but at COUNTY grain (county_fips +
    county_name) and with one additional column:
    ``n_pumas_contributing`` -- how many distinct PUMAs allocated
    population to this county-cell. For NJ, almost always 1 (PUMAs
    nest within counties); only PUMAs 02501 and 02601 are
    multi-county and they contribute fractionally to multiple
    county-cells.

    METHODOLOGY: This table is NOT a roll-up of pums_burden_segmented.
    It is re-aggregated from raw PUMS via the population-weighted
    PUMA-county crosswalk (ref.puma2020_county_xwalk). Median-of-
    medians is statistically invalid; this approach produces
    statistically defensible county-level medians.
    """

    model_config = _FROZEN

    year:                  int
    product:               str
    state_fips:            str
    county_fips:           str
    county_name:           str
    tenure_class:          str
    segment_dim:           str
    segment_value:         str
    weighted_n:               int
    sample_n:                  int
    household_income_p50:      float | None = None
    household_income_p50_se:   float | None = Field(
        default=None,
        description="SDR-based standard error of household_income_p50.",
    )
    monthly_cost_p50:          float | None = None
    monthly_cost_p50_se:       float | None = Field(
        default=None,
        description="SDR-based standard error of monthly_cost_p50.",
    )
    burden_ratio_p50:          float | None = None
    burden_ratio_p50_se:       float | None = Field(
        default=None,
        description=(
            "SDR-based standard error of burden_ratio_p50. For multi-"
            "county PUMAs the replicate weights are first multiplied by "
            "the PUMA-county allocation factor, so the SE captures the "
            "additional uncertainty from allocation."
        ),
    )
    suppressed:                bool
    n_pumas_contributing:      int          = Field(
        ...,
        description=(
            "Distinct PUMAs that allocated population to this cell. "
            "1 for the vast majority of NJ counties; 2 for cells in "
            "Salem/Cumberland/Cape May/Atlantic that overlap "
            "multi-county PUMAs."
        ),
    )


# ============================================================================
# /pums-burden-county-series  (multi-year time-series, overall segment)
# ============================================================================


class PumsBurdenCountySeriesRow(BaseModel):
    """One (year, product, county, tenure) point on the overall-segment series.

    Slim variant of PumsBurdenCountyRow: keeps only the columns a chart
    needs (ratio, SE, weighted_n, sample_n) and omits the per-segment
    breakdown columns. The series is always ``segment_dim='overall'``,
    ``segment_value='overall'`` -- per-segment trends are out of scope
    for the trend chart and would explode the response payload.

    The 90% confidence interval at any point is::

        burden_ratio_p50 +/- 1.645 * burden_ratio_p50_se

    For acs5-to-acs5 deltas the two endpoints share four years of
    sample, so naive independence overstates uncertainty; the YoY view
    in SQL flags this caveat with ``burden_ratio_delta_se_naive``.
    """

    model_config = _FROZEN

    year:                  int
    product:               str
    state_fips:            str
    county_fips:           str
    county_name:           str
    tenure_class:          str
    weighted_n:               int
    sample_n:                  int
    burden_ratio_p50:          float | None = None
    burden_ratio_p50_se:       float | None = Field(
        default=None,
        description=(
            "SDR-based standard error of burden_ratio_p50. NULL when the "
            "point estimate is NULL or when the cell is suppressed."
        ),
    )
    suppressed:                bool


# ============================================================================
# /hpi/{county_fips}/series
# ============================================================================


class HpiCountyRow(BaseModel):
    """One year of FHFA HPI for one county, re-indexed to a chosen base year.

    The platform serves the indexed series (base_year = 100.000 by
    convention) because that is the cross-county-comparable form. The
    raw FHFA value (``hpi_raw``) is exposed for transparency, since
    its scale is vintage-dependent and only meaningful as a ratio.

    ``annual_change`` and ``n_transactions`` come straight from FHFA;
    we do NOT recompute annual_change from hpi_raw because FHFA's
    rounding convention differs from a naive (cur/prev - 1).
    """

    model_config = _FROZEN

    county_fips:    str
    county_name:    str
    year:           int
    hpi_indexed:    float = Field(
        ...,
        description=(
            "FHFA HPI rebased so that the requested ``base_year`` = "
            "100.000 for this county. Comparable across counties."
        ),
    )
    hpi_raw:        float = Field(
        ...,
        description=(
            "FHFA's published index value in their current vintage's "
            "base year. NOT comparable across vintages without rebasing."
        ),
    )
    base_year_used: int   = Field(
        ...,
        description=(
            "The base year actually applied. Echoes the request "
            "parameter; useful when the API picks a default."
        ),
    )
    annual_change:  float | None = Field(
        default=None,
        description=(
            "FHFA's published annual percent change. NULL for the first "
            "year a county has data."
        ),
    )
    n_transactions: int | None = Field(
        default=None,
        description=(
            "Repeat-sales transaction count behind this year's index "
            "value. Below ~25 the estimate is statistically thin "
            "(FHFA still publishes them; the platform passes them "
            "through and lets consumers apply their own filter)."
        ),
    )


# ============================================================================
# /income/{county_fips}/series
# ============================================================================


class IncomeCountyRow(BaseModel):
    """One year of CPI-deflated ACS B19013 median household income.

    The flagship value is ``estimate_real``: median household income
    expressed in ``base_year_used`` dollars. ``estimate_nominal`` is
    the as-published ACS estimate (in ``dollar_year`` dollars); the
    deflator that maps one to the other is ``deflator``.

    Suppressed estimates are excluded server-side -- ACS uses
    -666666666 / -222222222 sentinels, the substrate stores them as
    NULL with a ``suppression_code``, and the deflator function
    filters them out. This row schema therefore never carries a
    NULL ``estimate_real``; the route returns 404 instead of an
    empty list when nothing usable exists.
    """

    model_config = _FROZEN

    county_fips:      str
    county_name:      str
    year:             int   = Field(..., description="ACS survey year (the END year for ACS5).")
    product:          str   = Field(..., description="'acs1' or 'acs5'.")
    estimate_real:    float = Field(
        ...,
        description=(
            "Median household income in ``base_year_used`` dollars. "
            "Computed as estimate_nominal x (CPI[base_year] / "
            "CPI[dollar_year])."
        ),
    )
    estimate_nominal: float = Field(
        ...,
        description=(
            "ACS-published estimate, in ``dollar_year`` dollars (no "
            "deflation applied)."
        ),
    )
    deflator:         float = Field(
        ...,
        description=(
            "Multiplicative factor from nominal to real. >1 when "
            "deflating past dollars to a more recent base year."
        ),
    )
    base_year_used:   int   = Field(
        ...,
        description="Echoes the deflation base year actually used.",
    )
    dollar_year:      int   = Field(
        ...,
        description=(
            "ACS dollar year. For ACS1 = year; for ACS5 = end year of "
            "the rolling 5-year window. Sets the CPI denominator."
        ),
    )
    margin_of_error:  float | None = Field(
        default=None,
        description=(
            "ACS-published 90% MOE on the NOMINAL estimate. The platform "
            "does NOT inflate the MOE alongside the estimate; consumers "
            "wanting a real-dollar CI should multiply MOE by the same "
            "deflator (an exact transformation under linear scaling)."
        ),
    )


# ============================================================================
# /counties
# ============================================================================


class CountyRef(BaseModel):
    """One row of ref.county exposed to the UI.

    Just enough to populate a dropdown; consumers that need centroids
    or area should hit the underlying table directly. Stable shape so
    the front-end does not need to evolve when we add columns to
    ``ref.county`` for non-display purposes.
    """

    model_config = _FROZEN

    county_fips: str = Field(..., examples=["34003"])
    name:        str = Field(..., examples=["Bergen"])


# ============================================================================
# /fec/* (civic-integrity / fraud surface, Tier 4 v1)
# ============================================================================
#
# Two design choices baked into the FEC model layer:
#
# 1. ALL ID FIELDS ARE STRINGS, not ints. FEC's CAND_ID and CMTE_ID
#    are alphanumeric (e.g. "C00500587"); SUB_ID is a 19-digit string
#    that exceeds Postgres BIGINT but fits in TEXT. We mirror the raw
#    storage type to keep round-trips lossless.
#
# 2. NO MODEL FOR ENUM-VALUE LOOKUPS BEYOND `FecEnumValue`. The
#    candidate office (H/S/P), party (DEM/REP/IND/...), and committee
#    type (P/H/S/Q/...) are codes whose human label varies by client
#    and locale. We expose the code; the UI can attach a human label
#    by reading the FEC documentation file (which we do not duplicate
#    server-side -- one source of truth, FEC's docs).
# ============================================================================


class FecEnumValue(BaseModel):
    """A distinct value drawn from a FEC enum column (state, office, party, ...).

    Used by /fec/cycles, /fec/states, /fec/parties, /fec/offices to
    populate UI filter dropdowns. ``count`` is the row count for that
    value in the most recent cycle (helps the UI sort by signal, not
    alpha order).
    """

    model_config = _FROZEN

    value: str = Field(..., description="The raw FEC code (e.g. 'NJ', 'DEM', 'S').")
    count: int = Field(..., description="Number of rows in raw with this value.")


class FecCandidateRow(BaseModel):
    """One candidate (slim row for tables / lists)."""

    model_config = _FROZEN

    cycle:                str     = Field(..., examples=["2024"], min_length=4, max_length=4)
    cand_id:              str     = Field(..., examples=["S4NJ00466"])
    cand_name:            str | None = None
    cand_pty_affiliation: str | None = Field(default=None, examples=["DEM"])
    cand_office:          str | None = Field(default=None, examples=["S"])
    cand_office_st:       str | None = Field(default=None, examples=["NJ"])
    cand_office_district: str | None = None
    cand_ici:             str | None = Field(
        default=None, description="I=incumbent, C=challenger, O=open seat",
    )
    cand_status:          str | None = Field(
        default=None, description="C=current, F=future, N=not-yet-filed, P=prior, W=withdrawn",
    )
    cand_pcc:             str | None = Field(
        default=None, examples=["C00540500"],
        description="Principal campaign committee FECID (joins to fec_committee.cmte_id)",
    )


class FecCandidateDetail(FecCandidateRow):
    """Single-candidate detail view: candidate row + linked committees."""

    model_config = _FROZEN

    cand_st1:        str | None = None
    cand_st2:        str | None = None
    cand_city:       str | None = None
    cand_st:         str | None = None
    cand_zip:        str | None = None
    cand_election_yr: int | None = None
    linked_committees: list[FecCommitteeRow] = Field(
        default_factory=list,
        description=(
            "All committees in raw.fec_committee where cand_id = this "
            "candidate's cand_id, scoped to the same cycle."
        ),
    )


class FecCommitteeRow(BaseModel):
    """One committee (slim row for tables / lists)."""

    model_config = ConfigDict(
        str_strip_whitespace=True, frozen=True, populate_by_name=True,
    )

    cycle:                str     = Field(..., examples=["2024"], min_length=4, max_length=4)
    cmte_id:              str     = Field(..., examples=["C00540500"])
    committee_name:       str | None = Field(default=None, examples=["BOOKER FOR SENATE"])
    treasurer_name:       str | None = Field(default=None, examples=["WHITE, ELIZABETH"])
    cmte_st:              str | None = Field(default=None, examples=["NJ"])
    cmte_dsgn:            str | None = Field(
        default=None,
        description="A=authorized by candidate, P=principal campaign, "
                    "B=lobbyist/registrant PAC, U=unauthorized, ...",
    )
    cmte_tp:              str | None = Field(
        default=None,
        description="P=presidential, H=house, S=senate, Q=PAC qualified, "
                    "N=PAC non-qualified, O=independent expenditure-only, ...",
    )
    cmte_pty_affiliation: str | None = Field(default=None, examples=["DEM"])
    cand_id:              str | None = Field(
        default=None,
        description="Candidate this committee is affiliated with, if any.",
    )


class FecCommitteeDetail(FecCommitteeRow):
    """Single-committee detail: row + linked candidate + recent contributions sample."""

    model_config = _FROZEN

    cmte_st1:        str | None = None
    cmte_st2:        str | None = None
    cmte_city:       str | None = None
    cmte_zip:        str | None = None
    cmte_filing_freq: str | None = None
    org_tp:          str | None = None
    connected_org_nm: str | None = None
    linked_candidate: FecCandidateRow | None = None
    recent_contributions: list[FecContributionRow] = Field(
        default_factory=list,
        description=(
            "Up to 25 most recent contributions to this committee "
            "(by transaction_date desc, NULL last)."
        ),
    )


class FecContributionRow(BaseModel):
    """One individual contribution transaction (raw + cooked-date view)."""

    model_config = _FROZEN

    cycle:                       str     = Field(..., examples=["2024"], min_length=4, max_length=4)
    sub_id:                      str     = Field(..., examples=["4031120241234567"])
    cmte_id:                     str | None = None
    contributor_name:            str | None = None
    contributor_city:            str | None = None
    contributor_state:           str | None = None
    contributor_zip:             str | None = None
    contributor_employer:        str | None = None
    contributor_occupation:      str | None = None
    contributor_entity_type:     str | None = Field(
        default=None,
        description="IND=individual, CCM=committee, PAC=PAC, "
                    "PTY=party, ORG=organization, ...",
    )
    transaction_type:            str | None = None
    transaction_primary_general: str | None = Field(
        default=None,
        description="P=primary, G=general, S=special, R=runoff, "
                    "C=convention, ...",
    )
    transaction_amount:          float | None = None
    transaction_date:            dt.date | None = Field(
        default=None,
        description="Parsed from FEC's MMDDYYYY raw field; NULL on invalid.",
    )
    is_memo:                     bool = Field(
        default=False,
        description=(
            "True for memo_cd='X' rows: itemized sub-line entries that "
            "must be excluded from summable totals to avoid double-counting."
        ),
    )


class FecMoneyToNjRow(BaseModel):
    """One row of public.v_fec_money_to_nj_candidates (the headline view)."""

    model_config = _FROZEN

    cycle:                       str = Field(..., min_length=4, max_length=4)
    sub_id:                      str
    cand_id:                     str
    cand_name:                   str | None = None
    cand_office:                 str | None = None
    cand_office_district:        str | None = None
    cand_pty_affiliation:        str | None = None
    cmte_id:                     str
    committee_name:              str | None = None
    cmte_dsgn:                   str | None = None
    contributor_name:            str | None = None
    contributor_city:            str | None = None
    contributor_state:           str | None = None
    contributor_zip:             str | None = None
    contributor_employer:        str | None = None
    contributor_occupation:      str | None = None
    contributor_entity_type:     str | None = None
    transaction_type:            str | None = None
    transaction_primary_general: str | None = None
    transaction_amount:          float | None = None
    transaction_date:            dt.date | None = None
    is_memo:                     bool = False


class FecPagedResponse(BaseModel):
    """Standard envelope for paginated FEC list endpoints.

    Carries the rows alongside total_count + the pagination knobs the
    caller used. The UI uses total_count to compute total page count
    without requiring a separate /count endpoint.
    """

    model_config = _FROZEN

    rows:        list[Any] = Field(..., description="Page contents.")
    total_count: int       = Field(..., description="Total matching rows ignoring limit/offset.")
    limit:       int       = Field(...)
    offset:      int       = Field(...)


class FecSummary(BaseModel):
    """Cross-table snapshot for the fraud-UI dashboard header.

    All counts are scoped to the most recent (max) cycle present in
    raw.fec_candidate. If no FEC data has been loaded yet, ``cycle``
    will be the empty string and all counts are zero -- so the UI
    can render an "ingest data first" empty state without an extra
    health probe.
    """

    model_config = _FROZEN

    cycle:                  str = Field(..., examples=["2024"])
    candidates_total:       int
    candidates_nj:          int
    committees_total:       int
    committees_nj_domiciled: int
    contributions_total:    int
    contributions_nj_donor: int
    contributions_to_nj_candidates: int
    cycles_available:       list[str] = Field(
        default_factory=list,
        description="All cycles present in raw.fec_candidate, descending.",
    )


# ============================================================================
# Tier 4 v2 -- fraud-detection metric layer
# ============================================================================
#
# A fraud "metric" here is a stable, named, parameterised SQL signal that
# returns a list of *flagged* entities (committees, candidates, donors,
# clusters). The catalog is shared across the serving API and the UI so
# adding a new signal is a single registration in `serving.queries_fec_metrics`.
#
# These models intentionally use `dict[str, Any]` for `rows`. Every metric
# has its own column shape (one returns "treasurer + n_committees", another
# returns "address + n_committees"), and exhaustively typing each shape would
# triple the model count for negligible safety win at the API boundary --
# the UI is a generic table renderer that already inspects keys at runtime.
# We DO type the catalog metadata strictly, because UIs depend on it.

class FraudMetricCatalogEntry(BaseModel):
    """One signal in the fraud-detection catalog.

    Catalog entries are the API's contract with the UI. They tell the UI
    (a) which endpoints exist, (b) how to label each signal, (c) which
    columns to render and which to hide, and (d) how to interpret severity.
    """

    model_config = _FROZEN

    id:           str       = Field(..., examples=["treasurer_concentration"])
    name:         str       = Field(..., examples=["Treasurer concentration"])
    tier:         str       = Field(
        ...,
        description=(
            "Either 'structural' (cn/cm-only signals, computable from a "
            "fresh load) or 'contribution' (requires indiv contributions, "
            "i.e. derived signals over donations)."
        ),
        examples=["structural", "contribution"],
    )
    description:  str       = Field(
        ...,
        description="Plain-English explanation of what the signal flags and why.",
    )
    threshold_note: str | None = Field(
        default=None,
        description=(
            "Informal threshold guidance for analysts (e.g. 'rows with "
            "n_committees > 15 are likely leads')."
        ),
    )
    sort_default: str       = Field(
        ...,
        description="Default ORDER BY column when the UI loads this metric.",
        examples=["severity_score"],
    )
    primary_key_cols: list[str] = Field(
        default_factory=list,
        description=(
            "Columns the UI should treat as the entity identity for "
            "drill-downs (link to candidate/committee detail panels)."
        ),
    )


class FraudMetricSummary(BaseModel):
    """Catalog-level summary: how many flagged rows per signal at a cycle.

    Powers the Metrics-tab overview header so analysts can see in one
    glance which signals have anything to investigate before drilling in.
    """

    model_config = _FROZEN

    cycle:        str = Field(..., examples=["2024"])
    counts:       dict[str, int] = Field(
        ...,
        description=(
            "Map of metric_id -> total flagged rows in raw.fec_* for "
            "this cycle. Empty dict if no FEC data is loaded."
        ),
    )


class FraudMetricResult(BaseModel):
    """A page of flagged rows for a single fraud metric.

    Pairs the catalog entry (so consumers can render metadata without a
    separate fetch) with a paginated row slice.
    """

    model_config = _FROZEN

    metric:       FraudMetricCatalogEntry
    rows:         list[dict[str, Any]] = Field(
        ...,
        description=(
            "Flagged entities. Column shape is metric-specific; consult "
            "metric.id to interpret."
        ),
    )
    total_count:  int = Field(..., description="Total flagged rows ignoring pagination.")
    limit:        int = Field(...)
    offset:       int = Field(...)


# ============================================================================
# Tier 4 v3 -- entity-first fraud-risk surface
# ============================================================================
#
# These models back the ``/fec/risk/entities`` routes. Two shapes:
#
# * Queue rows (one per entity)             -> RiskQueueRow / RiskQueueResponse
# * Per-entity evidence panel               -> RiskEntityPanel
#
# The queue is a thin slice (no parallel arrays), the panel pivots the
# parallel arrays into a list of RiskSignalObservation entries with
# per-signal score decomposition (phi_contribution + score_share_pct).
#
# Source of truth for ``risk_score`` is ``derived.fraud_risk_score`` in
# migration 052; the per-signal decomposition is computed in Python in
# ``serving.queries_fec_risk._decompose_score`` using the same constants
# (gamma=2, k=50, percentile_floor=0.95).


class RiskQueueRow(BaseModel):
    """One entity in the risk-ranked queue.

    Carries the score, the L2 aggregates the UI uses to render
    secondary columns (signal count, max severity, max peer percentile,
    primary peer bucket), and the list of signal IDs that fired so the
    queue row can show signal-pill badges without an extra fetch. The
    full per-signal arrays are NOT included here -- those belong to the
    panel.
    """

    model_config = _FROZEN

    cycle:                str  = Field(
        ...,
        description="FEC election cycle as 4-digit string.",
        examples=["2024"],
    )
    entity_kind:          str  = Field(
        ...,
        description="One of: committee, candidate, treasurer, address, donor_cluster.",
        examples=["candidate"],
    )
    entity_id:            str  = Field(
        ...,
        description=(
            "Stable identifier for the entity. Shape depends on "
            "entity_kind: candidate -> FEC candidate ID, committee -> "
            "FEC committee ID, treasurer -> canonical treasurer name, "
            "address -> canonical street, donor_cluster -> cluster ID."
        ),
    )
    risk_score:           float = Field(
        ...,
        ge=0,
        le=100,
        description=(
            "Composite risk score (0..100). Sourced from "
            "derived.fraud_risk_score; this is NOT a probability of "
            "fraud, it is a ranked outlier score. See work_left.txt "
            "calibration anchors."
        ),
    )
    n_signals_fired:      int = Field(..., ge=0)
    max_severity:         int = Field(..., ge=1, le=5)
    max_peer_percentile:  float = Field(..., ge=0, le=1)
    avg_peer_percentile:  float = Field(..., ge=0, le=1)
    primary_peer_bucket:  str = Field(
        ...,
        description=(
            "The peer bucket of the highest-severity / highest-percentile "
            "signal that fired. Used by the UI to label 'compared to' "
            "context (e.g. 'office=H, state=NJ')."
        ),
    )
    signals_fired:        list[str] = Field(
        ...,
        description=(
            "Sorted list of signal_id values that fired for this entity "
            "in this cycle. Stable order (alphabetical)."
        ),
    )
    last_observation_at:  dt.datetime = Field(
        ...,
        description="Most recent observation materialization time across the entity's signals.",
    )


class RiskQueueResponse(BaseModel):
    """Paginated envelope for the risk queue.

    Mirrors :class:`FecPagedResponse` but with a typed row list (rather
    than ``list[Any]``) so OpenAPI consumers see the row schema. The
    ``filters`` block echoes the query knobs the request used so the UI
    can show "showing entities with risk_score >= 60 in 2024" without
    threading the request params through state.
    """

    model_config = _FROZEN

    rows:        list[RiskQueueRow]
    total_count: int = Field(..., ge=0)
    limit:       int = Field(..., ge=1)
    offset:      int = Field(..., ge=0)
    filters:     dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Echo of effective filter knobs (cycle, entity_kind, "
            "signal_id, min_score, max_score)."
        ),
    )


class RiskSignalObservation(BaseModel):
    """One per-signal observation inside a panel.

    Carries enough data for the analyst to (a) understand WHY the score
    is what it is (severity x percentile x bucket), (b) drill into the
    underlying metric view (evidence_url), and (c) see how much THIS
    signal contributed to the final score (score_share_pct).
    """

    model_config = _FROZEN

    signal_id:        str
    severity:         int   = Field(..., ge=1, le=5)
    peer_percentile:  float = Field(..., ge=0, le=1)
    peer_bucket:      str
    raw_value:        float | None = Field(
        default=None,
        description=(
            "The raw metric value that earned this percentile. NULL "
            "for binary signals (e.g. 'no PCC') where the absence is "
            "the signal."
        ),
    )
    evidence_url:     str = Field(
        ...,
        description=(
            "Relative URL into the existing /fec/metrics surface that "
            "lists every entity flagged by this signal in this cycle. "
            "Lets the UI deep-link 'see other treasurers like this one'."
        ),
    )
    phi_contribution: float = Field(
        ...,
        ge=0,
        description=(
            "Raw additive contribution to the risk-score's pre-EXP sum: "
            "phi = severity * max(0, percentile - 0.95)^2. Signals at or "
            "below the percentile floor contribute exactly 0."
        ),
    )
    score_share_pct:  float = Field(
        ...,
        ge=0,
        le=100,
        description=(
            "Percentage of the total raw_sum that this signal accounts "
            "for. Sums (across observations) to 100 when at least one "
            "signal exceeds the floor; to 0 otherwise."
        ),
    )


class RiskEntityPanel(BaseModel):
    """Full evidence panel for a single (entity_kind, entity_id, cycle).

    The list of observations is sorted by score_share_pct DESC so the
    panel renders "biggest contributors first". When no signal exceeds
    the percentile floor, all share values are 0 and the order falls
    back to signal_id ASC for determinism.
    """

    model_config = _FROZEN

    cycle:                str
    entity_kind:          str
    entity_id:            str
    risk_score:           float = Field(..., ge=0, le=100)
    n_signals_fired:      int   = Field(..., ge=0)
    max_severity:         int   = Field(..., ge=1, le=5)
    max_peer_percentile:  float = Field(..., ge=0, le=1)
    avg_peer_percentile:  float = Field(..., ge=0, le=1)
    primary_peer_bucket:  str
    last_observation_at:  dt.datetime
    observations:         list[RiskSignalObservation]
