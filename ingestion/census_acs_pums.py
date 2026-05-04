"""ACS Public Use Microdata Sample (PUMS) ingester.

TIER 3 (population segmentation). Pulls ACS PUMS 1-year (or 5-year)
person-level and housing-unit-level microdata from Census's FTP
distribution, parses it into the canonical
``raw.acs_pums_person`` / ``raw.acs_pums_housing`` shape, and bulk-COPYs
to Postgres.

Architecture
------------
PUMS is structurally different from every other source the platform
ingests, in three ways:

  1. **It is a sample, not an aggregate.** Every analytical query MUST
     weight by ``pwgtp`` (persons) or ``wgtp`` (households). Standard
     errors require the 80 replicate weights. We store both as
     ``INTEGER[]`` columns in Postgres.

  2. **Geography is PUMA, not county.** PUMA boundaries do not align
     with county boundaries; allocation to county lives in the derived
     layer and uses Census's PUMA->county correspondence files.

  3. **File size and shape.** A single state-year PUMS file has
     ~80-300 MB uncompressed CSV with ~280 columns. We project to
     ~25-30 columns at parse time, which both shrinks the working
     set and pins the schema (an upstream column rename does not
     silently corrupt the load).

The ingester is split into the same three-layer pattern as
``ingestion.dol_oflc_lca``:

  * **Fetch**  -> :func:`fetch_pums_year` returns a tuple
    (PERSON DataFrame, HOUSING DataFrame, source metadata).
  * **Stage**  -> :func:`stage_person_dataframe` / :func:`stage_housing_dataframe`
    add provenance columns and validate shape.
  * **Load**   -> :func:`load_to_postgres` bulk-COPYs via psycopg's
    CSV protocol. We chunk reads but issue a single COPY per table
    per year to minimize round-trips.

Source URL pattern
------------------
For 2017+ PUMS::

    https://www2.census.gov/programs-surveys/acs/data/pums/{year}/1-Year/csv_p{state_lower}.zip
    https://www2.census.gov/programs-surveys/acs/data/pums/{year}/1-Year/csv_h{state_lower}.zip

Inside each ZIP is a single CSV named ``psam_p{ST}.csv`` or
``psam_h{ST}.csv`` (where ``{ST}`` is the 2-letter state abbreviation).

For 2007-2016, the URL pattern differs slightly. We do not support
those years here -- the substrate of derived metrics needs only
~10 years of PUMS to compute meaningful trends, and the older format
is enough additional code to defer.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import click
import httpx
import polars as pl

from ingestion._base import IngestError

if TYPE_CHECKING:
    from collections.abc import Iterable

    import psycopg


log = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================


# 2017 is the first year that uses the modern psam_*.csv layout. Earlier
# years use ss{YY}p{state}.csv inside differently-structured ZIPs and
# are out of scope for this ingester (see module docstring).
EARLIEST_SUPPORTED_YEAR: Final[int] = 2017

# State -> (FIPS, lowercase abbreviation, uppercase abbreviation).
# We only seed the NJ row here; expanding to all 50 states is a one-line
# addition, but the platform's first contract is NJ-only.
STATE_LOOKUP: Final[dict[str, tuple[str, str, str]]] = {
    "NJ": ("34", "nj", "NJ"),
}

ALLOWED_PRODUCTS: Final[frozenset[str]] = frozenset({"acs1", "acs5"})

# Person record columns we project from the raw CSV. Order matches the
# columns in raw.acs_pums_person (excluding replicate_weights and
# provenance, which are added at stage time).
PERSON_VARS: Final[tuple[str, ...]] = (
    "SERIALNO", "SPORDER", "ST",
    # PUMA omitted intentionally: the ingester detects PUMA / PUMA10 /
    # PUMA20 dynamically (Census's column layout depends on whether the
    # file spans a decennial-PUMA-revision boundary; 5-year files for
    # 2022 carry BOTH PUMA10 and PUMA20). The detection lives in
    # _parse_pums_csv, which projects whichever is present and emits a
    # canonical (puma, puma_vintage) pair.
    "AGEP", "SEX", "RAC1P", "HISP", "CIT", "POBP", "NATIVITY",
    "SCHL", "ESR", "COW",
    "WAGP", "PERNP", "PINCP",
    "PWGTP",
)

# 80 replicate weights for PERSON records.
PERSON_REPL_WEIGHT_VARS: Final[tuple[str, ...]] = tuple(
    f"PWGTP{i}" for i in range(1, 81)
)

# Housing record columns.
#
# YRBLT vs YBL: the year-built variable was renamed in ACS 2019 1-Year
# (and 2021 5-Year, since 5-Year files use the latest dictionary). YBL
# was a 1-22 binned code; YRBLT is a 4-digit year. We omit the variable
# from this projection list and detect dynamically in _parse_pums_csv,
# similar to PUMA / PUMA10 / PUMA20. Older files that carry YBL only
# write NULL into the canonical ``yrblt`` column -- we do not perform
# a bin->midpoint mapping here, because no derived analytic uses
# year-built today and the loss is recoverable later.
HOUSING_VARS: Final[tuple[str, ...]] = (
    "SERIALNO", "ST",
    "TEN", "BDSP", "RMSP", "BLD", "VEH",
    "VALP", "GRNTP", "RNTP", "SMOCP", "SMP",
    "HINCP", "FINCP",
    "WGTP",
)

HOUSING_REPL_WEIGHT_VARS: Final[tuple[str, ...]] = tuple(
    f"WGTP{i}" for i in range(1, 81)
)


# Census FTP base.
PUMS_BASE_URL: Final[str] = "https://www2.census.gov/programs-surveys/acs/data/pums"

# Retry policy mirrors the other Census ingesters (ACS B19013): exponential
# backoff on transient errors, immediate fail on 4xx (except 408/429).
_RETRY_MAX_ATTEMPTS: Final[int] = 4
_RETRY_BASE_BACKOFF_S: Final[float] = 2.0

# Read up to this much from the upstream HTTP response into memory at
# once. The downloaded ZIPs are 30-100 MB, comfortably below this cap.
_DOWNLOAD_TIMEOUT_S: Final[float] = 600.0  # 10 min for slow Census FTP

# COPY buffer chunk size when streaming staged DataFrame -> Postgres.
_COPY_CHUNK: Final[int] = 1 << 20  # 1 MiB


class VintageNotPublishedError(IngestError):
    """Raised when Census's FTP confirms (404) that no file exists.

    PUMS is published 12-15 months after the survey year (e.g., 2022
    PUMS released October 2023). Asking for an unreleased vintage is
    legitimate operator behavior; we want a recoverable error rather
    than a noisy stack trace.
    """


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class PUMSFetchResult:
    """Output of :func:`fetch_pums_year`.

    Both person and housing DataFrames are present; they are emitted
    together because PUMS is published as a paired (PERSON, HOUSING)
    drop and downstream queries always join across them.
    """

    person:           pl.DataFrame
    housing:          pl.DataFrame
    source_url_person: str
    source_url_housing: str
    source_sha256_person: str
    source_sha256_housing: str
    year:             int
    product:          str
    state_fips:       str
    n_person_rows:    int
    n_housing_rows:   int


def _build_url(*, year: int, product: str, state_lower: str, kind: str) -> str:
    """Return the Census FTP URL for a (year, product, state, kind) PUMS file.

    *kind* is ``"p"`` for person records, ``"h"`` for housing.
    """
    if kind not in {"p", "h"}:
        raise IngestError(f"kind must be 'p' or 'h'; got {kind!r}")
    if product == "acs1":
        sub = "1-Year"
    elif product == "acs5":
        sub = "5-Year"
    else:
        raise IngestError(f"product must be 'acs1' or 'acs5'; got {product!r}")
    return f"{PUMS_BASE_URL}/{year}/{sub}/csv_{kind}{state_lower}.zip"


def _get_with_retry(url: str, *, timeout_s: float) -> bytes:
    """GET *url* with exponential-backoff retry on transient errors.

    Mirrors the policy in :mod:`ingestion.census_acs_income`. 404
    raises :class:`VintageNotPublishedError`; other 4xx raise
    immediately; 5xx / network errors retry up to
    ``_RETRY_MAX_ATTEMPTS``.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
                resp = client.get(url)
            if resp.status_code == 404:
                raise VintageNotPublishedError(
                    f"Census PUMS FTP returned 404 for {url}. "
                    "Most common cause: requested year not yet released "
                    "(PUMS lags survey year by ~12-15 months)."
                )
            if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                resp.raise_for_status()
            if resp.is_success:
                return resp.content
            last_exc = httpx.HTTPStatusError(
                f"transient HTTP {resp.status_code}",
                request=resp.request, response=resp,
            )
        except httpx.TransportError as exc:
            last_exc = exc
        if attempt < _RETRY_MAX_ATTEMPTS:
            backoff = _RETRY_BASE_BACKOFF_S * (2 ** (attempt - 1))
            log.warning(
                "PUMS fetch attempt %d/%d failed (%s); retrying in %.1fs",
                attempt, _RETRY_MAX_ATTEMPTS, type(last_exc).__name__, backoff,
            )
            time.sleep(backoff)
    assert last_exc is not None
    raise IngestError(
        f"PUMS fetch failed after {_RETRY_MAX_ATTEMPTS} attempts: "
        f"{type(last_exc).__name__}: {last_exc}"
    ) from last_exc


def _extract_csv_from_zip(zip_bytes: bytes, *, kind: str, state_upper: str) -> bytes:
    """Find and return the inner CSV from a PUMS ZIP archive.

    Census names the inner file ``psam_{kind}{state_upper}.csv`` (e.g.,
    ``psam_pNJ.csv``) but a few historical years use a FIPS-coded name
    (``psam_p34.csv``). We search both patterns and any ``.csv`` if
    neither matches.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        # Preferred: psam_{kind}{ST}.csv
        preferred = f"psam_{kind}{state_upper}.csv"
        if preferred in names:
            return zf.read(preferred)
        # Older alternate: psam_{kind}{state_fips}.csv
        candidates = [n for n in names if n.lower().endswith(".csv")]
        if not candidates:
            raise IngestError(
                f"No CSV found in PUMS ZIP. Archive contents: {names}"
            )
        # Use the first .csv in the archive. Census never bundles
        # multiple unrelated CSVs, so this is unambiguous in practice.
        return zf.read(candidates[0])


def _detect_puma_columns(csv_bytes: bytes) -> list[str]:
    """Return which of ``PUMA20``, ``PUMA10``, ``PUMA`` appear in the CSV header.

    Census changes the PUMA column naming convention across vintages:

      * Files entirely after a decennial revision: column is just ``PUMA``.
        (e.g., 2022 1-Year, since all sample is post-2020.)
      * Files spanning a decennial revision: BOTH ``PUMA10`` and ``PUMA20``
        are present. PUMA20 is populated for records sampled 2020+;
        PUMA10 for records sampled 2010-2019. The columns are
        complementary -- exactly one is non-null per row.
        (e.g., 2022 5-Year covers 2018-2022.)

    Order matters: when both PUMA10 and PUMA20 are present we prefer
    PUMA20 in the coalesce. When only ``PUMA`` is present we treat it
    as the latest published vintage (2020 for current data).

    Returns an empty list if the header looks valid but no PUMA-shaped
    column is found, which is itself an error -- the caller must raise.
    """
    head = csv_bytes[: 64 * 1024].split(b"\n", 1)[0].decode("utf-8", errors="replace")
    cols = {c.strip() for c in head.split(",")}
    return [c for c in ("PUMA20", "PUMA10", "PUMA") if c in cols]


def _detect_yrblt_columns(csv_bytes: bytes) -> list[str]:
    """Return whichever of YRBLT, YBL is present in the CSV header.

    Up to one of the two should appear (Census switched names with
    ACS 2019 1-Year and ACS 2021 5-Year, never published both). If
    neither is present -- which is unexpected for any year we ingest
    -- we return an empty list and the caller will write NULL into
    the ``yrblt`` destination column.
    """
    head = csv_bytes[: 64 * 1024].split(b"\n", 1)[0].decode("utf-8", errors="replace")
    cols = {c.strip() for c in head.split(",")}
    return [c for c in ("YRBLT", "YBL") if c in cols]


def _default_puma_vintage_for_year(year: int, product: str) -> str:
    """Decide the PUMA vintage when only a bare ``PUMA`` column exists.

    These thresholds were verified against real Census FTP files (NJ
    state-year files for 2018-2022). The empirical pattern:

      * 1-Year ACS PUMS:
        - 2017-2019: 2010-vintage in bare ``PUMA``.
        - 2020:      not published (COVID disruption).
        - 2021:      2010-vintage in bare ``PUMA`` (Census kept 2010
                     boundaries for 2021 because the 2020-vintage
                     PUMAs were not finalized until 2022).
        - 2022+:     2020-vintage in bare ``PUMA``.
      * 5-Year ACS PUMS:
        - 2017-2021: 2010-vintage in bare ``PUMA`` (the file's sample
                     spans pre-revision years exclusively, or the
                     post-revision portion is small enough Census
                     re-coded it to 2010 for consistency).
        - 2022+:     dual-column ``PUMA10`` + ``PUMA20`` -- handled
                     in the dual-column branch of _parse_pums_csv,
                     not here. If a 2022+ 5-Year file ever appears
                     with bare ``PUMA``, the safe assumption is
                     2020-vintage.

    The dual-column case is handled separately in
    :func:`_parse_pums_csv` and never reaches this helper.

    Crossing year 2022 is the canonical decennial-revision threshold
    in the substrate; earlier ingester logic incorrectly used 2021,
    which silently mis-labeled all 2021 NJ data as 2020-vintage. The
    dual-vintage county compute then refused to materialize, surfacing
    the bug.
    """
    threshold = 2022
    return "2010" if year < threshold else "2020"


def _parse_pums_csv(
    csv_bytes: bytes,
    *,
    primary_vars: Iterable[str],
    weight_vars: Iterable[str],
    is_person: bool,
    year: int,
    product: str,
) -> pl.DataFrame:
    """Parse a PUMS CSV and project to (primary_vars + PUMA + replicate_weights array).

    Polars' lazy CSV reader is used so we can ``select`` the projected
    columns BEFORE materializing the DataFrame; this is critical for
    holding ~100K-row x ~280-column raw files in memory without OOM.

    PUMA handling: We dynamically detect whether the file uses ``PUMA``,
    ``PUMA10``, ``PUMA20``, or a combination, and synthesize a canonical
    (``PUMA``, ``puma_vintage``) pair. PUMA10-tagged records are NOT
    dropped here -- they are preserved at raw grain so a future PUMA10
    crosswalk can recover them. The derived layer is responsible for
    filtering to ``puma_vintage = '2020'`` when joining the
    ``ref.puma2020_county_xwalk`` table.

    The 80 replicate weights are read as separate columns and then
    folded into a single Polars list column (``replicate_weights``)
    that maps directly to Postgres ``INTEGER[]`` on COPY.
    """
    puma_cols = _detect_puma_columns(csv_bytes)
    if not puma_cols:
        raise IngestError(
            "PUMS CSV has no PUMA / PUMA10 / PUMA20 column in its header. "
            "This is unrecoverable -- Census's file layout has changed in a "
            "way the ingester does not understand."
        )

    yrblt_cols: list[str] = [] if is_person else _detect_yrblt_columns(csv_bytes)

    primary_list = list(primary_vars) + puma_cols + yrblt_cols
    weight_list = list(weight_vars)
    all_cols = [*primary_list, *weight_list]

    # Force string types on identifier columns BEFORE scanning. ACS 5-Year
    # SERIALNO can be alphanumeric (e.g., "2018HU0133940") in older
    # vintages -- Polars's schema inference reads only the first 1000
    # rows, and if those happen to be numeric-only it locks in Int64
    # and rejects the first alphanumeric row. Same defensive treatment
    # for ST and the PUMA-vintage columns (-9 sentinel, leading zeros).
    schema_overrides: dict[str, type[pl.DataType] | pl.DataType] = {
        "SERIALNO": pl.Utf8,
        "ST":       pl.Utf8,
    }
    for c in puma_cols:
        schema_overrides[c] = pl.Utf8

    lf = pl.scan_csv(
        io.BytesIO(csv_bytes),
        infer_schema_length=1000,
        try_parse_dates=False,
        ignore_errors=False,
        schema_overrides=schema_overrides,
    ).select(all_cols)
    df = lf.collect()

    weight_exprs = [pl.col(c).cast(pl.Int32) for c in weight_list]
    df = df.with_columns(
        pl.concat_list(weight_exprs).alias("replicate_weights"),
    ).drop(weight_list)

    # Coerce SERIALNO to text. Modern PUMS uses a 13-char alphanumeric
    # code (e.g., 2022GQ0000001234); Polars infers Utf8 for that. Older
    # years are integer-only; cast to Utf8 either way.
    df = df.with_columns(pl.col("SERIALNO").cast(pl.Utf8))

    # ST is always present, always int-coercible, always 2 chars.
    df = df.with_columns(
        pl.col("ST").cast(pl.Int64).cast(pl.Utf8).str.zfill(2).alias("ST"),
    )

    # Synthesize canonical (PUMA, puma_vintage). Census encodes
    # "not applicable" PUMA values as -9 (which zfill-pads to "-0009"),
    # so the safe normalization is: cast to Int64, treat <=0 as NULL,
    # then back to zero-padded 5-char string. PUMA codes are always
    # in the range [00100, 99999] -- there is no zero PUMA.
    for c in puma_cols:
        df = df.with_columns(
            pl.col(c).cast(pl.Int64, strict=False).alias(f"_{c}_int"),
        )
        df = df.with_columns(
            pl.when(pl.col(f"_{c}_int") > 0)
              .then(pl.col(f"_{c}_int").cast(pl.Utf8).str.zfill(5))
              .otherwise(None)
              .alias(c),
        ).drop([f"_{c}_int"])

    has20 = "PUMA20" in puma_cols
    has10 = "PUMA10" in puma_cols
    hasplain = "PUMA" in puma_cols

    if has20 and has10:
        # Coalesce: prefer PUMA20 (post-decennial); fall back to PUMA10.
        # Each row is expected to have exactly one populated.
        df = df.with_columns(
            pl.coalesce(["PUMA20", "PUMA10"]).alias("PUMA"),
            pl.when(pl.col("PUMA20").is_not_null()).then(pl.lit("2020"))
              .when(pl.col("PUMA10").is_not_null()).then(pl.lit("2010"))
              .otherwise(pl.lit("unknown")).alias("puma_vintage"),
        ).drop(["PUMA20", "PUMA10"])
    elif has20:
        df = df.with_columns(
            pl.col("PUMA20").alias("PUMA"),
            pl.lit("2020").alias("puma_vintage"),
        ).drop(["PUMA20"])
    elif has10:
        df = df.with_columns(
            pl.col("PUMA10").alias("PUMA"),
            pl.lit("2010").alias("puma_vintage"),
        ).drop(["PUMA10"])
    else:
        # Only "PUMA" -- vintage depends on the year of the file. For
        # 1-Year files: 2017-2019 are 2010-vintage; 2022+ are 2020. For
        # 5-Year files: 2017-2020 are 2010-vintage; 2021+ have both
        # codes (handled in the dual-column branch above).
        if not hasplain:
            raise IngestError("Unreachable: no PUMA column matched.")
        default_vintage = _default_puma_vintage_for_year(year, product)
        df = df.with_columns(pl.lit(default_vintage).alias("puma_vintage"))

    bad_rows = df.filter(pl.col("PUMA").is_null()).height
    if bad_rows > 0:
        log.warning(
            "PUMS parse: %d rows had no valid PUMA in any vintage column; "
            "dropping. (This is rare; usually the file is internally "
            "consistent.)", bad_rows,
        )
        df = df.filter(pl.col("PUMA").is_not_null())

    # Synthesize canonical YRBLT for housing files. ACS 2017-2018
    # 1-Year and 2017-2020 5-Year carry "YBL" (1-22 binned code); ACS
    # 2019+ 1-Year and 2021+ 5-Year carry "YRBLT" (4-digit year). We
    # write the modern YRBLT into raw and lose the YBL signal -- there
    # is no analytic that uses year-built today, and the bin->midpoint
    # conversion is approximate. Older years get NULL.
    if not is_person:
        if "YRBLT" in yrblt_cols:
            df = df.with_columns(pl.col("YRBLT").cast(pl.Int32, strict=False))
        elif "YBL" in yrblt_cols:
            df = df.with_columns(
                pl.lit(None).cast(pl.Int32).alias("YRBLT"),
            ).drop("YBL")
            log.info(
                "PUMS housing %d %s: file uses pre-2019 YBL coding; "
                "writing NULL into yrblt. To recover, add a 1-22 "
                "bin->midpoint year mapping in the ingester.",
                year, product,
            )
        else:
            df = df.with_columns(
                pl.lit(None).cast(pl.Int32).alias("YRBLT"),
            )

    if is_person:
        df = df.with_columns(pl.col("SPORDER").cast(pl.Int16))

    return df


def fetch_pums_year(
    *,
    year: int,
    product: str,
    state: str = "NJ",
    timeout_s: float = _DOWNLOAD_TIMEOUT_S,
) -> PUMSFetchResult:
    """Fetch PUMS person + housing CSVs for *year* / *product* / *state*.

    Returns a :class:`PUMSFetchResult` with both DataFrames already
    projected to the canonical column set and with replicate weights
    folded into a list column.

    Raises :class:`VintageNotPublishedError` if Census returns 404
    for either file (caller should record a governance signal and
    move on).
    """
    if year < EARLIEST_SUPPORTED_YEAR:
        raise IngestError(
            f"PUMS year {year} predates the supported {EARLIEST_SUPPORTED_YEAR}+ "
            "psam_* layout. Older years use ss{YY}p{state}.csv inside differently-"
            "structured ZIPs and are not implemented here."
        )
    if product not in ALLOWED_PRODUCTS:
        raise IngestError(
            f"Unknown product {product!r}; expected one of {sorted(ALLOWED_PRODUCTS)}"
        )
    if state not in STATE_LOOKUP:
        raise IngestError(
            f"State {state!r} not in the supported set {sorted(STATE_LOOKUP)}. "
            "Add an entry to STATE_LOOKUP to extend coverage."
        )
    state_fips, state_lower, state_upper = STATE_LOOKUP[state]

    url_p = _build_url(year=year, product=product, state_lower=state_lower, kind="p")
    url_h = _build_url(year=year, product=product, state_lower=state_lower, kind="h")
    log.info("Fetching PUMS person:  %s", url_p)
    body_p = _get_with_retry(url_p, timeout_s=timeout_s)
    log.info("Fetching PUMS housing: %s", url_h)
    body_h = _get_with_retry(url_h, timeout_s=timeout_s)

    sha_p = hashlib.sha256(body_p).hexdigest()
    sha_h = hashlib.sha256(body_h).hexdigest()

    csv_p = _extract_csv_from_zip(body_p, kind="p", state_upper=state_upper)
    csv_h = _extract_csv_from_zip(body_h, kind="h", state_upper=state_upper)

    df_p = _parse_pums_csv(
        csv_p,
        primary_vars=PERSON_VARS, weight_vars=PERSON_REPL_WEIGHT_VARS,
        is_person=True, year=year, product=product,
    )
    df_h = _parse_pums_csv(
        csv_h,
        primary_vars=HOUSING_VARS, weight_vars=HOUSING_REPL_WEIGHT_VARS,
        is_person=False, year=year, product=product,
    )

    if df_p.height == 0:
        raise IngestError(
            f"PUMS person file at {url_p} parsed to zero rows; this should "
            "not happen for a valid year/product/state."
        )
    if df_h.height == 0:
        raise IngestError(f"PUMS housing file at {url_h} parsed to zero rows.")

    return PUMSFetchResult(
        person=df_p,
        housing=df_h,
        source_url_person=url_p,
        source_url_housing=url_h,
        source_sha256_person=sha_p,
        source_sha256_housing=sha_h,
        year=year, product=product, state_fips=state_fips,
        n_person_rows=df_p.height,
        n_housing_rows=df_h.height,
    )


# ============================================================================
# Stage
# ============================================================================


# Mapping from PUMS uppercase column names to lowercase Postgres column
# names. Keeping a single explicit dict here means a column rename in
# the schema is a one-line change.
_PERSON_COL_RENAME: Final[dict[str, str]] = {
    "SERIALNO": "serialno",
    "SPORDER":  "sporder",
    "ST":       "state_fips",
    "PUMA":     "puma",
    "AGEP":     "agep",
    "SEX":      "sex",
    "RAC1P":    "rac1p",
    "HISP":     "hisp",
    "CIT":      "cit",
    "POBP":     "pobp",
    "NATIVITY": "nativity",
    "SCHL":     "schl",
    "ESR":      "esr",
    "COW":      "cow",
    "WAGP":     "wagp",
    "PERNP":    "pernp",
    "PINCP":    "pincp",
    "PWGTP":    "pwgtp",
}

_HOUSING_COL_RENAME: Final[dict[str, str]] = {
    "SERIALNO": "serialno",
    "ST":       "state_fips",
    "PUMA":     "puma",
    "TEN":      "ten",
    "BDSP":     "bdsp",
    "RMSP":     "rmsp",
    "BLD":      "bld",
    "YRBLT":    "yrblt",
    "VEH":      "veh",
    "VALP":     "valp",
    "GRNTP":    "grntp",
    "RNTP":     "rntp",
    "SMOCP":    "smocp",
    "SMP":      "smp",
    "HINCP":    "hincp",
    "FINCP":    "fincp",
    "WGTP":     "wgtp",
}


# Final column ORDER, matching raw.acs_pums_person primary key + columns +
# replicate_weights + provenance. Order matters for COPY.
_PERSON_DEST_COLS: Final[tuple[str, ...]] = (
    "year", "product", "serialno", "sporder",
    "state_fips", "puma", "puma_vintage",
    "agep", "sex", "rac1p", "hisp", "cit", "pobp", "nativity",
    "schl", "esr", "cow",
    "wagp", "pernp", "pincp",
    "pwgtp", "replicate_weights",
    "source_url", "source_sha256", "source_vintage",
)

_HOUSING_DEST_COLS: Final[tuple[str, ...]] = (
    "year", "product", "serialno",
    "state_fips", "puma", "puma_vintage",
    "ten", "bdsp", "rmsp", "bld", "yrblt", "veh",
    "valp", "grntp", "rntp", "smocp", "smp",
    "hincp", "fincp",
    "wgtp", "replicate_weights",
    "source_url", "source_sha256", "source_vintage",
)


def stage_person_dataframe(
    fetch: PUMSFetchResult, *, source_vintage: str | None = None,
) -> pl.DataFrame:
    """Project + rename + add provenance for the person DataFrame."""
    vintage = source_vintage or f"{fetch.year}-{fetch.product}"
    df = fetch.person.rename(_PERSON_COL_RENAME)
    df = df.with_columns(
        pl.lit(fetch.year).cast(pl.Int16).alias("year"),
        pl.lit(fetch.product).alias("product"),
        pl.lit(fetch.source_url_person).alias("source_url"),
        pl.lit(fetch.source_sha256_person).alias("source_sha256"),
        pl.lit(vintage).alias("source_vintage"),
    )
    return df.select(_PERSON_DEST_COLS)


def stage_housing_dataframe(
    fetch: PUMSFetchResult, *, source_vintage: str | None = None,
) -> pl.DataFrame:
    """Project + rename + add provenance for the housing DataFrame."""
    vintage = source_vintage or f"{fetch.year}-{fetch.product}"
    df = fetch.housing.rename(_HOUSING_COL_RENAME)
    df = df.with_columns(
        pl.lit(fetch.year).cast(pl.Int16).alias("year"),
        pl.lit(fetch.product).alias("product"),
        pl.lit(fetch.source_url_housing).alias("source_url"),
        pl.lit(fetch.source_sha256_housing).alias("source_sha256"),
        pl.lit(vintage).alias("source_vintage"),
    )
    return df.select(_HOUSING_DEST_COLS)


# ============================================================================
# Load
# ============================================================================


def _format_int_array(values: list[int | None] | None) -> str:
    """Format a Python list of ints as a Postgres array literal.

    Used in the CSV COPY pipeline because Polars' ``write_csv``
    serializes a list column as a Python repr (``[1, 2, 3]``) which
    Postgres rejects. We replace the list column with a string column
    formatted as ``{1,2,3}`` before writing the CSV.
    """
    if values is None:
        return ""
    if any(v is None for v in values):
        # PUMS always emits all 80 weights; a NULL inside the array is
        # a strong signal of upstream corruption. Fail loudly.
        raise IngestError(
            f"NULL inside replicate_weights array (length {len(values)}); "
            "PUMS always emits 80 non-null integer weights. Refusing to load."
        )
    return "{" + ",".join(str(v) for v in values) + "}"


def _delete_existing(
    connection: psycopg.Connection,
    *, table: str, year: int, product: str,
) -> int:
    """DELETE existing rows for this (year, product) before COPY.

    PUMS occasionally republishes a vintage with corrections (extremely
    rare, but the substrate has to handle it). Combined with our
    transactional COPY, DELETE + COPY is the simplest correct re-load
    semantics.
    """
    # The *table* arg comes from a static module-level constant, never
    # from user input. F-string is safe; a parameterized identifier is
    # not supported by psycopg without sql.SQL composition, and that
    # adds noise without security value here.
    with connection.cursor() as cur:
        cur.execute(
            f"DELETE FROM {table} WHERE year = %s AND product = %s",
            (year, product),
        )
        return cur.rowcount


def _copy_dataframe(
    df: pl.DataFrame, connection: psycopg.Connection, *, table: str,
) -> int:
    """Bulk-COPY *df* into *table* (CSV format, NULL '').

    *df* must already have ``replicate_weights`` formatted as a Postgres
    array literal string.
    """
    from psycopg import sql

    if "." in table:
        schema_part, table_part = table.split(".", 1)
        ident = sql.Identifier(schema_part, table_part)
    else:
        ident = sql.Identifier(table)
    col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in df.columns)
    copy_query = sql.SQL("COPY {tbl} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')").format(
        tbl=ident, cols=col_idents,
    )

    buf = io.BytesIO()
    df.write_csv(buf, include_header=False)
    buf.seek(0)

    with connection.cursor().copy(copy_query) as cp:
        while chunk := buf.read(_COPY_CHUNK):
            cp.write(chunk)
    return df.height


def load_to_postgres(
    person_staged: pl.DataFrame,
    housing_staged: pl.DataFrame,
    connection: psycopg.Connection,
    *,
    year: int,
    product: str,
) -> tuple[int, int]:
    """DELETE + COPY both staged DataFrames; return (n_person, n_housing).

    Wraps both COPYs in the caller's transaction. We DELETE before COPY
    so a partial-vintage reload is atomic and idempotent.
    """
    # Format replicate_weights as Postgres array literal strings.
    person_for_copy = person_staged.with_columns(
        pl.col("replicate_weights")
          .map_elements(_format_int_array, return_dtype=pl.Utf8)
          .alias("replicate_weights"),
    )
    housing_for_copy = housing_staged.with_columns(
        pl.col("replicate_weights")
          .map_elements(_format_int_array, return_dtype=pl.Utf8)
          .alias("replicate_weights"),
    )

    _delete_existing(connection, table="raw.acs_pums_person",  year=year, product=product)
    _delete_existing(connection, table="raw.acs_pums_housing", year=year, product=product)

    n_person  = _copy_dataframe(person_for_copy,  connection, table="raw.acs_pums_person")
    n_housing = _copy_dataframe(housing_for_copy, connection, table="raw.acs_pums_housing")
    return n_person, n_housing


# ============================================================================
# CLI
# ============================================================================


@click.group()
def cli() -> None:
    """ACS PUMS ingester (Tier 3)."""


@cli.command("fetch")
@click.option("--year", type=int, required=True)
@click.option("--product", type=click.Choice(sorted(ALLOWED_PRODUCTS)),
              default="acs1", show_default=True)
@click.option("--state", default="NJ", show_default=True)
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help="Optional directory to dump parsed CSVs (debugging only).")
def fetch_cmd(year: int, product: str, state: str, out_dir: Path | None) -> None:
    """Fetch one (year, product, state) PUMS pair and print a summary."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = fetch_pums_year(year=year, product=product, state=state)
    click.echo(
        f"person  url={result.source_url_person}\n"
        f"        sha={result.source_sha256_person}\n"
        f"        n_rows={result.n_person_rows}\n"
        f"housing url={result.source_url_housing}\n"
        f"        sha={result.source_sha256_housing}\n"
        f"        n_rows={result.n_housing_rows}\n"
    )
    click.echo(result.person.head(5))
    click.echo(result.housing.head(5))
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        result.person.write_parquet(out_dir / f"pums_p_{state}_{year}_{product}.parquet")
        result.housing.write_parquet(out_dir / f"pums_h_{state}_{year}_{product}.parquet")
        click.echo(f"Wrote parquet to {out_dir}")


@cli.command("load")
@click.option("--year", type=int, required=True)
@click.option("--product", type=click.Choice(sorted(ALLOWED_PRODUCTS)),
              default="acs1", show_default=True)
@click.option("--state", default="NJ", show_default=True)
@click.option("--dsn", envvar="PG_DSN", required=True,
              help="Postgres DSN (or set PG_DSN env var).")
def load_cmd(year: int, product: str, state: str, dsn: str) -> None:
    """Fetch + DELETE/COPY one (year, product, state) PUMS pair into Postgres."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import psycopg

    try:
        result = fetch_pums_year(year=year, product=product, state=state)
    except VintageNotPublishedError as exc:
        log.warning("Skipping PUMS %s %s %s: %s", state, year, product, exc)
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO governance.dataset_health "
                "(dataset_id, signal_name, severity, details) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (
                    "raw.acs_pums_person",
                    "vintage_not_published",
                    "warn",
                    json.dumps({
                        "year": year, "product": product, "state": state,
                        "reason": "Census FTP returned 404",
                    }),
                ),
            )
            conn.commit()
        return

    person_staged  = stage_person_dataframe(result)
    housing_staged = stage_housing_dataframe(result)
    with psycopg.connect(dsn) as conn:
        n_p, n_h = load_to_postgres(
            person_staged, housing_staged, conn,
            year=year, product=product,
        )
        conn.commit()
    click.echo(
        f"Loaded PUMS {state} {year} {product}: "
        f"{n_p} person rows, {n_h} housing rows."
    )


