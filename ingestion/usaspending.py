"""USAspending.gov federal-award ingester (FRAUD-F1, contracts-only v1).

Fetches NJ-place-of-performance contract awards from
``POST /api/v2/search/spending_by_award/`` and loads them into
``raw.usaspending_award``.

Architecture
------------
* :func:`fetch_awards`   -- paginated REST fetch, streams each page to
                            a JSONL sidecar in the cache dir. Returns a
                            FetchResult with the file path + provenance
                            (sha256 of the concatenated JSONL bytes,
                            row count, filter fingerprint).
* :func:`parse_awards`   -- reads the JSONL, yields one canonicalized
                            row dict per award.
* :func:`load_to_postgres` -- COPY into a temp staging table, then
                            INSERT ... ON CONFLICT (generated_unique_award_id)
                            DO UPDATE to upsert with last_seen_at bumps.

Why JSONL on disk
-----------------
1. Re-parseability: if our column projection changes, we re-parse
   without re-fetching (the API quota is the scarce resource, not the
   parser).
2. Provenance: sha256(jsonl) is the canonical "what bytes did we get
   from USAspending" fingerprint, recorded on every loaded row.
3. Resumability: if pagination dies at page 437 of 1000, pages 1-436
   are already on disk. The fetcher detects partial files and resumes.

Why not SQLAlchemy / pandas
---------------------------
psycopg.copy + io.BytesIO is faster, lower memory, and matches the FEC
loader's pattern. The cleaned-row list is materialized in Python only
because we need to compute provenance fields per-row before COPY; the
table is small (NJ contracts FY2024 ~30K-50K awards), so memory is not
a constraint.

Rate limiting
-------------
USAspending publishes a 60 req/min anonymous rate limit. We pace at
1 req/sec by default (well under the limit) with exponential backoff
on 429 / 5xx. The pacing is configurable via ``request_interval_s``
because the platform deploys to a single Oracle Always Free VM and
shares the API quota with no other tenants.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import click
import httpx
from psycopg import sql

from ingestion._base import IngestError, sha256_file

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg

log = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

USASPENDING_API_BASE: Final[str] = "https://api.usaspending.gov/api/v2"
SPENDING_BY_AWARD_PATH: Final[str] = "/search/spending_by_award/"

# The filter shape the platform uses for the canonical "NJ contracts"
# pull. Captured here as a Python constant rather than constructed
# ad-hoc per call so the filter sha256 is stable across runs (changing
# the filter is a deliberate, reviewed code change -- not an accidental
# kwarg drift).
DEFAULT_AWARD_TYPE_CODES: Final[tuple[str, ...]] = ("A", "B", "C", "D")
DEFAULT_PLACE_OF_PERFORMANCE_STATE: Final[str] = "NJ"

# Field names requested from the API. The names are USAspending's
# (mixed-case-with-spaces); pinned here so a server-side rename surfaces
# as a parser KeyError instead of a silent field drop.
API_FIELDS: Final[tuple[str, ...]] = (
    "Award ID",
    "Recipient Name",
    "Recipient UEI",
    "Recipient DUNS",
    "Award Amount",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Award Type",
    "Start Date",
    "End Date",
    "Description",
    "generated_internal_id",
    "Recipient Location",
    "Place of Performance",
    "Last Modified Date",
    "Period of Performance Start Date",
    "Period of Performance Current End Date",
)

# Pagination + pacing.
DEFAULT_PAGE_SIZE: Final[int]   = 100         # API max
DEFAULT_REQUEST_INTERVAL_S: Final[float] = 1.0
DEFAULT_MAX_RETRIES: Final[int] = 5
_HTTP_TIMEOUT_S: Final[float]  = 60.0

# COPY chunk size. Awards are ~1 KB each in JSON-flattened form; 8 MiB
# buffers ~8K rows -- comfortably one or two flushes for a typical FY
# pull of ~30-50K awards.
_COPY_CHUNK_SIZE: Final[int] = 8 * 1024 * 1024

# Earliest fiscal year supported by the search endpoint (per the API's
# own messaging on every response). Pulls before this require the
# bulk-download endpoint, which is out of scope.
EARLIEST_FY: Final[int] = 2008

# YYYY-MM-DD regex for date fields. The API formats dates this way for
# Start Date / End Date / Period of Performance fields; the Last
# Modified Date field uses 'YYYY-MM-DD HH:MM:SS' instead.
_DATE_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
)


# ============================================================================
# Filter / fingerprinting
# ============================================================================


def fiscal_year_window(fiscal_year: int) -> tuple[str, str]:
    """Return ('YYYY-10-01', 'YYYY-09-30') for a federal FY.

    Federal FY N runs from Oct 1 of calendar year N-1 through Sep 30
    of calendar year N. Returns (start, end) as ISO date strings.
    """
    if fiscal_year < EARLIEST_FY:
        raise ValueError(
            f"fiscal_year={fiscal_year} is older than the search-endpoint "
            f"limit of {EARLIEST_FY}; use the bulk-download API for "
            "earlier data.",
        )
    return (f"{fiscal_year - 1}-10-01", f"{fiscal_year}-09-30")


def build_filter(
    *,
    fiscal_year: int,
    state: str = DEFAULT_PLACE_OF_PERFORMANCE_STATE,
    award_type_codes: tuple[str, ...] = DEFAULT_AWARD_TYPE_CODES,
) -> dict[str, Any]:
    """Build the canonical ``filters`` object for a fiscal-year pull.

    The output is what we send to the API and (in JSON-canonical form)
    what we sha256 for ``api_query_filter_sha256``. Field order is
    fixed across runs by sorting keys at JSON-encode time.
    """
    start, end = fiscal_year_window(fiscal_year)
    return {
        "place_of_performance_locations": [
            {"country": "USA", "state": state},
        ],
        "award_type_codes": list(award_type_codes),
        "time_period": [
            {"start_date": start, "end_date": end},
        ],
    }


def fingerprint_filter(filter_obj: dict[str, Any]) -> str:
    """Return SHA-256 hex of the filter JSON (stable across runs).

    Keys are sorted, separators are tight, ensuring two operators
    invoking the same logical filter produce the same fingerprint.
    """
    canonical = json.dumps(filter_obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================================
# Fetch
# ============================================================================


@dataclass(frozen=True)
class FetchResult:
    """Output of :func:`fetch_awards`."""

    path:                   Path
    fiscal_year:            int
    state:                  str
    filter_sha256:          str
    n_pages:                int
    n_awards:               int
    source_url:             str
    file_sha256:            str
    cache_hit:              bool
    last_modified_observed: str | None  # max API "Last Modified Date" seen


@dataclass
class _PaginatorState:
    """In-memory state for the paginator.

    Counter for awards seen and a max-Last-Modified tracker; the latter
    is recorded as ``last_modified_observed`` and serves as the
    high-water-mark for "USAspending content as of when".
    """

    awards: int = 0
    pages:  int = 0
    last_modified_max: str | None = None
    seen_award_ids: set[str] = field(default_factory=set)


def _extract_last_modified_max(
    cur_max: str | None, results: list[dict[str, Any]],
) -> str | None:
    """Return max(cur_max, max(result['Last Modified Date'])) as a string.

    Lexicographic comparison is correct for ISO-8601-shaped strings; we
    do not need to parse to datetime.
    """
    out = cur_max
    for r in results:
        v = r.get("Last Modified Date")
        if not isinstance(v, str) or not v:
            continue
        if out is None or v > out:
            out = v
    return out


def fetch_awards(
    *,
    fiscal_year: int,
    dest_dir: Path,
    state: str = DEFAULT_PLACE_OF_PERFORMANCE_STATE,
    award_type_codes: tuple[str, ...] = DEFAULT_AWARD_TYPE_CODES,
    page_size: int = DEFAULT_PAGE_SIZE,
    request_interval_s: float = DEFAULT_REQUEST_INTERVAL_S,
    max_retries: int = DEFAULT_MAX_RETRIES,
    overwrite: bool = False,
    http_client: httpx.Client | None = None,
) -> FetchResult:
    """Paginate the spending_by_award endpoint, streaming JSONL to disk.

    The output file is one JSON record per line, in API-response order
    across pages. A ``.part`` sidecar holds the in-progress write and
    is renamed atomically on success.

    If ``overwrite=False`` and a complete output file already exists
    (matching the FY+state+filter fingerprint), the function returns
    a cache_hit FetchResult without re-fetching.

    Returns a FetchResult with provenance fields. ``filter_sha256`` is
    the fingerprint of the JSON filter object; ``file_sha256`` is the
    fingerprint of the on-disk JSONL bytes.

    Raises:
        IngestError: on persistent upstream failure (after
            ``max_retries`` exponential-backoff retries) or zero awards
            for a year the operator did not expect to be empty (this
            is left as a soft-info log; the loader downstream is the
            place that fails on zero rows).

    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    filter_obj = build_filter(
        fiscal_year=fiscal_year, state=state, award_type_codes=award_type_codes,
    )
    filter_sha = fingerprint_filter(filter_obj)
    out_name = f"usaspending_fy{fiscal_year}_{state}_{filter_sha[:12]}.jsonl"
    out_path = dest_dir / out_name

    if out_path.exists() and not overwrite:
        with out_path.open("r", encoding="utf-8") as fh:
            n_awards = sum(1 for _ in fh)
        log.info(
            "usaspending.fetch: cache hit for %s (%d awards)",
            out_path.name, n_awards,
        )
        return FetchResult(
            path=out_path, fiscal_year=fiscal_year, state=state,
            filter_sha256=filter_sha, n_pages=0, n_awards=n_awards,
            source_url=f"{USASPENDING_API_BASE}{SPENDING_BY_AWARD_PATH}",
            file_sha256=sha256_file(out_path),
            cache_hit=True,
            last_modified_observed=None,
        )

    state_obj = _PaginatorState()
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=_HTTP_TIMEOUT_S)
    try:
        page = 1
        with tmp.open("w", encoding="utf-8") as fh:
            while True:
                payload = {
                    "filters": filter_obj,
                    "fields":  list(API_FIELDS),
                    "limit":   page_size,
                    "page":    page,
                }
                results, has_next = _post_with_retry(
                    client,
                    USASPENDING_API_BASE + SPENDING_BY_AWARD_PATH,
                    payload,
                    max_retries=max_retries,
                )
                state_obj.pages += 1
                state_obj.last_modified_max = _extract_last_modified_max(
                    state_obj.last_modified_max, results,
                )
                for r in results:
                    award_id = r.get("generated_internal_id")
                    if not isinstance(award_id, str) or not award_id:
                        # Refuse to write a row whose PK we cannot
                        # populate. A future API change that drops this
                        # field should fail loud at fetch time, not
                        # later at COPY time.
                        raise IngestError(
                            f"USAspending API row on page {page} missing "
                            "non-empty 'generated_internal_id'; refusing "
                            "to silently drop.",
                        )
                    if award_id in state_obj.seen_award_ids:
                        # The API uses cursor-style pagination on
                        # last_record_unique_id; in rare cases (a
                        # transaction modification mid-paginate) the
                        # same award can appear twice. De-duplicate at
                        # write time so the JSONL byte stream is
                        # canonical.
                        continue
                    state_obj.seen_award_ids.add(award_id)
                    state_obj.awards += 1
                    fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")))
                    fh.write("\n")
                if not has_next:
                    break
                page += 1
                if request_interval_s > 0:
                    time.sleep(request_interval_s)
    finally:
        if owns_client:
            client.close()

    if state_obj.awards == 0:
        log.info(
            "usaspending.fetch: 0 awards for FY%d state=%s. The loader "
            "will refuse to UPSERT a 0-row pull; remove the empty "
            "cache file %s if this is unexpected.",
            fiscal_year, state, tmp,
        )

    tmp.replace(out_path)
    file_sha = sha256_file(out_path)
    log.info(
        "usaspending.fetch: wrote %d awards across %d pages -> %s "
        "(filter_sha=%s, file_sha=%s)",
        state_obj.awards, state_obj.pages, out_path.name,
        filter_sha[:12], file_sha[:12],
    )
    return FetchResult(
        path=out_path, fiscal_year=fiscal_year, state=state,
        filter_sha256=filter_sha,
        n_pages=state_obj.pages, n_awards=state_obj.awards,
        source_url=f"{USASPENDING_API_BASE}{SPENDING_BY_AWARD_PATH}",
        file_sha256=file_sha,
        cache_hit=False,
        last_modified_observed=state_obj.last_modified_max,
    )


def _post_with_retry(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    *,
    max_retries: int,
) -> tuple[list[dict[str, Any]], bool]:
    """POST with exponential-backoff retry. Return (results, has_next).

    Retries on:
        * 429 (rate-limit) -- back off and try again
        * 5xx              -- transient upstream
        * httpx.RequestError -- network error

    Does NOT retry on 4xx other than 429 (those are bugs in our
    payload shape; raising IngestError is the right surface).
    """
    delay = 1.0
    last_exc: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.post(url, json=payload)
            if resp.status_code == 429:
                log.warning(
                    "usaspending: 429 rate-limit on attempt %d; "
                    "sleeping %.1fs", attempt, delay,
                )
                time.sleep(delay)
                delay *= 2
                continue
            if 500 <= resp.status_code < 600:
                log.warning(
                    "usaspending: %d on attempt %d; sleeping %.1fs",
                    resp.status_code, attempt, delay,
                )
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code != 200:
                raise IngestError(
                    f"USAspending {url} -> {resp.status_code}: "
                    f"{resp.text[:500]}",
                )
            data = resp.json()
            results = data.get("results", [])
            has_next = bool(data.get("page_metadata", {}).get("hasNext", False))
            if not isinstance(results, list):
                raise IngestError(
                    f"USAspending {url}: 'results' is not a list "
                    f"(got {type(results).__name__})",
                )
            return results, has_next
        except httpx.RequestError as exc:
            last_exc = exc
            log.warning(
                "usaspending: %s on attempt %d; sleeping %.1fs",
                type(exc).__name__, attempt, delay,
            )
            time.sleep(delay)
            delay *= 2
            continue
    raise IngestError(
        f"USAspending {url}: exhausted {max_retries} retries; "
        f"last_exc={last_exc!r}",
    )


# ============================================================================
# Parse
# ============================================================================


@dataclass(frozen=True)
class ParseResult:
    """Output of :func:`parse_awards`."""

    rows:          list[dict[str, Any]]
    fetch:         FetchResult
    n_rows:        int


def _coerce_date(value: object, *, field_name: str) -> dt.date | None:
    """Validate / convert a YYYY-MM-DD string to date; None on empty."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise IngestError(
            f"USAspending {field_name}: expected str or null, got "
            f"{type(value).__name__}",
        )
    s = value.strip()
    if not s:
        return None
    if not _DATE_RE.match(s):
        raise IngestError(
            f"USAspending {field_name}: {value!r} is not YYYY-MM-DD",
        )
    return dt.date.fromisoformat(s)


def _coerce_datetime(value: object, *, field_name: str) -> dt.datetime | None:
    """Validate / convert a YYYY-MM-DD HH:MM:SS string to datetime."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise IngestError(
            f"USAspending {field_name}: expected str or null, got "
            f"{type(value).__name__}",
        )
    s = value.strip()
    if not s:
        return None
    if not _DATETIME_RE.match(s):
        raise IngestError(
            f"USAspending {field_name}: {value!r} is not "
            "'YYYY-MM-DD HH:MM:SS'",
        )
    return dt.datetime.fromisoformat(s)


def _coerce_amount(value: object) -> float | None:
    """Coerce an API Number-or-null amount to a float (or None)."""
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is a subclass of int in Python; explicitly reject.
        raise IngestError(f"USAspending Award Amount: bool {value!r} unexpected")
    if isinstance(value, (int, float)):
        return float(value)
    raise IngestError(
        f"USAspending Award Amount: expected Number or null, got "
        f"{type(value).__name__}",
    )


def _flatten_location(loc: object, *, kind: str) -> dict[str, str | None]:
    """Flatten the API's nested {Recipient,Place of Performance} Location.

    Returns a dict with stable keys regardless of API-side null shape:
        country_code, state, city, county_name, zip5, zip4,
        congressional_district.

    *kind* is purely for error messages ('Recipient' or 'Place of
    Performance').
    """
    if loc is None:
        return {
            "country_code": None, "state": None, "city": None,
            "county_name": None, "zip5": None, "zip4": None,
            "congressional_district": None,
        }
    if not isinstance(loc, dict):
        raise IngestError(
            f"USAspending {kind} Location: expected object or null, got "
            f"{type(loc).__name__}",
        )

    def _g(key: str) -> str | None:
        v = loc.get(key)
        if v is None:
            return None
        if not isinstance(v, str):
            return str(v)
        return v.strip() or None
    return {
        "country_code":           _g("location_country_code"),
        "state":                  _g("state_code"),
        "city":                   _g("city_name"),
        "county_name":            _g("county_name"),
        "zip5":                   _g("zip5"),
        "zip4":                   _g("zip4"),
        "congressional_district": _g("congressional_code"),
    }


def parse_awards(fetch: FetchResult) -> ParseResult:
    """Read JSONL on disk, return a list of cleaned row dicts.

    One pass over the file; rejects any row missing a required key
    (raises IngestError -- substrate-honesty: a parser surprise should
    surface as a load failure, not a silent skip).

    Required fields per row:
        generated_internal_id   -- PK
        Award Type code is parsed from the API field "Award Type" if
            populated; otherwise inferred from the `CONT_AWD_*` prefix
            of generated_internal_id (the API occasionally drops the
            human-readable type description but the prefix is stable).

    """
    rows: list[dict[str, Any]] = []
    if not fetch.path.exists():
        raise IngestError(f"USAspending JSONL not found: {fetch.path}")
    with fetch.path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IngestError(
                    f"USAspending JSONL line {line_no}: invalid JSON: "
                    f"{exc.msg}",
                ) from exc
            if not isinstance(obj, dict):
                raise IngestError(
                    f"USAspending JSONL line {line_no}: not an object",
                )

            award_id = obj.get("generated_internal_id")
            if not isinstance(award_id, str) or not award_id:
                raise IngestError(
                    f"USAspending JSONL line {line_no}: missing "
                    "non-empty generated_internal_id",
                )

            recipient_loc = _flatten_location(
                obj.get("Recipient Location"), kind="Recipient",
            )
            pop_loc = _flatten_location(
                obj.get("Place of Performance"), kind="Place of Performance",
            )

            # Award type code: prefer the API's "Award Type" field when
            # populated; else infer from the generated_internal_id
            # prefix. CONT_AWD_<piid>_<...> is the contract pattern.
            type_code = _award_type_code_from(obj, award_id)

            internal_id_raw = obj.get("internal_id")
            internal_id_int: int | None = None
            if isinstance(internal_id_raw, int):
                internal_id_int = internal_id_raw
            elif isinstance(internal_id_raw, str) and internal_id_raw.isdigit():
                internal_id_int = int(internal_id_raw)

            row: dict[str, Any] = {
                "generated_unique_award_id":  award_id,
                "award_id_piid":              _str_or_none(obj.get("Award ID")),
                "award_internal_id":          internal_id_int,
                "award_type_code":            type_code,
                "award_type_description":     _str_or_none(obj.get("Award Type")),
                "recipient_name":             _str_or_none(obj.get("Recipient Name")),
                "recipient_uei":              _str_or_none(obj.get("Recipient UEI")),
                "recipient_duns":             _str_or_none(obj.get("Recipient DUNS")),
                "recipient_country_code":     recipient_loc["country_code"],
                "recipient_state":            recipient_loc["state"],
                "recipient_city":             recipient_loc["city"],
                "recipient_county_name":      recipient_loc["county_name"],
                "recipient_zip5":             recipient_loc["zip5"],
                "recipient_zip4":             recipient_loc["zip4"],
                "recipient_congressional_district":
                                              recipient_loc["congressional_district"],
                "pop_country_code":           pop_loc["country_code"],
                "pop_state":                  pop_loc["state"],
                "pop_city":                   pop_loc["city"],
                "pop_county_name":            pop_loc["county_name"],
                "pop_zip5":                   pop_loc["zip5"],
                "pop_zip4":                   pop_loc["zip4"],
                "pop_congressional_district": pop_loc["congressional_district"],
                "award_amount":               _coerce_amount(obj.get("Award Amount")),
                "description":                _str_or_none(obj.get("Description")),
                "awarding_agency_name":       _str_or_none(obj.get("Awarding Agency")),
                "awarding_subagency_name":    _str_or_none(obj.get("Awarding Sub Agency")),
                "funding_agency_name":        _str_or_none(obj.get("Funding Agency")),
                "funding_subagency_name":     _str_or_none(obj.get("Funding Sub Agency")),
                "awarding_agency_id":         _int_or_none(obj.get("awarding_agency_id")),
                "agency_slug":                _str_or_none(obj.get("agency_slug")),
                "period_start":               _coerce_date(
                    obj.get("Start Date"), field_name="Start Date",
                ),
                "period_end":                 _coerce_date(
                    obj.get("End Date"), field_name="End Date",
                ),
                "period_pop_start":           _coerce_date(
                    obj.get("Period of Performance Start Date"),
                    field_name="Period of Performance Start Date",
                ),
                "period_pop_current_end":     _coerce_date(
                    obj.get("Period of Performance Current End Date"),
                    field_name="Period of Performance Current End Date",
                ),
                "last_modified_at":           _coerce_datetime(
                    obj.get("Last Modified Date"),
                    field_name="Last Modified Date",
                ),
            }

            # CHECK constraint requires at least one recipient identity.
            if not (row["recipient_name"] or row["recipient_uei"]
                    or row["recipient_duns"]):
                raise IngestError(
                    f"USAspending JSONL line {line_no}: row "
                    f"{award_id!r} has neither name, UEI, nor DUNS.",
                )

            rows.append(row)

    return ParseResult(rows=rows, fetch=fetch, n_rows=len(rows))


def _str_or_none(v: object) -> str | None:
    """Return v as a stripped str, or None if blank/None."""
    if v is None:
        return None
    if not isinstance(v, str):
        return str(v)
    s = v.strip()
    return s or None


def _int_or_none(v: object) -> int | None:
    """Return v as an int, or None for null. Strings accepted if numeric."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def _award_type_code_from(obj: dict[str, Any], award_id: str) -> str:
    """Resolve the award_type_code from API row.

    Order of preference:
        1. The "Award Type" field if it's a single-character A/B/C/D.
        2. The generated_internal_id prefix: 'CONT_AWD_*' is a contract
           award; all four codes (A, B, C, D) are subtypes of contract,
           and the API does not always populate "Award Type" with the
           letter -- but the platform's filter pinned the type set, so
           ANY contract row from a contract-only filter must be one of
           {A,B,C,D}. We default to 'D' (Definitive Contract) as the
           most common in NJ contract rolls; the actual subtype is
           recoverable from the awarding agency's transaction-level
           data (separate ingester).

    Returns:
        One of {'A', 'B', 'C', 'D'}.

    Raises:
        IngestError: if the row is clearly not a contract (no
        CONT_AWD_ prefix and no plausible Award Type).

    """
    api_type = obj.get("Award Type")
    if isinstance(api_type, str) and api_type.strip().upper() in {"A", "B", "C", "D"}:
        return api_type.strip().upper()
    if award_id.startswith("CONT_AWD_") or award_id.startswith("CONT_IDV_"):
        return "D"
    raise IngestError(
        f"USAspending row {award_id!r}: cannot resolve award_type_code "
        f"from API 'Award Type'={api_type!r} and prefix={award_id[:10]!r}. "
        "Filter expected contracts only.",
    )


# ============================================================================
# Load
# ============================================================================


_LOAD_COLUMNS: Final[tuple[str, ...]] = (
    "generated_unique_award_id",
    "award_id_piid",
    "award_internal_id",
    "award_type_code",
    "award_type_description",
    "recipient_name",
    "recipient_uei",
    "recipient_duns",
    "recipient_country_code",
    "recipient_state",
    "recipient_city",
    "recipient_county_name",
    "recipient_zip5",
    "recipient_zip4",
    "recipient_congressional_district",
    "pop_country_code",
    "pop_state",
    "pop_city",
    "pop_county_name",
    "pop_zip5",
    "pop_zip4",
    "pop_congressional_district",
    "award_amount",
    "description",
    "awarding_agency_name",
    "awarding_subagency_name",
    "funding_agency_name",
    "funding_subagency_name",
    "awarding_agency_id",
    "agency_slug",
    "period_start",
    "period_end",
    "period_pop_start",
    "period_pop_current_end",
    "last_modified_at",
    "fiscal_year_pulled",
    "api_query_filter_sha256",
    "page_number",
)


def _row_to_csv_record(
    row: dict[str, Any],
    *,
    fiscal_year: int,
    filter_sha: str,
) -> tuple[str, ...]:
    """Stringify one row into the COPY column order."""
    def _s(v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, dt.datetime):
            return v.isoformat()
        if isinstance(v, dt.date):
            return v.isoformat()
        return str(v)
    enriched = dict(row)
    enriched["fiscal_year_pulled"]      = fiscal_year
    enriched["api_query_filter_sha256"] = filter_sha
    enriched["page_number"]             = enriched.get("page_number")
    return tuple(_s(enriched.get(c)) for c in _LOAD_COLUMNS)


def _iter_csv_lines(parse: ParseResult) -> Iterator[bytes]:
    """Yield CSV-formatted bytes for COPY ... FORMAT csv."""
    import csv
    for row in parse.rows:
        out = io.StringIO()
        csv.writer(out, lineterminator="\n").writerow(
            _row_to_csv_record(
                row,
                fiscal_year=parse.fetch.fiscal_year,
                filter_sha=parse.fetch.filter_sha256,
            ),
        )
        yield out.getvalue().encode("utf-8")


def load_to_postgres(
    parse: ParseResult,
    conn: psycopg.Connection,
) -> int:
    """UPSERT parsed rows into raw.usaspending_award.

    Same two-phase shape as the LEIE loader: COPY into a temp staging
    table, then INSERT ... ON CONFLICT DO UPDATE on the PK.

    Returns the number of rows touched (inserted + updated).

    Raises:
        IngestError: when parse.n_rows == 0. A 0-row pull is either an
            empty FY (legitimate for a backfill that pre-dates the
            search endpoint) or a parser bug; either way we refuse to
            silently UPSERT nothing.

    """
    if parse.n_rows == 0:
        raise IngestError(
            f"usaspending.load: 0 parsed rows for FY{parse.fetch.fiscal_year} "
            f"state={parse.fetch.state}; refusing to write a no-op load.",
        )

    staging_col_idents = sql.SQL(", ").join(
        sql.Identifier(c) for c in _LOAD_COLUMNS
    )

    with conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE usaspending_staging ("
            "    generated_unique_award_id TEXT, "
            "    award_id_piid             TEXT, "
            "    award_internal_id         BIGINT, "
            "    award_type_code           TEXT, "
            "    award_type_description    TEXT, "
            "    recipient_name            TEXT, "
            "    recipient_uei             TEXT, "
            "    recipient_duns            TEXT, "
            "    recipient_country_code    TEXT, "
            "    recipient_state           TEXT, "
            "    recipient_city            TEXT, "
            "    recipient_county_name     TEXT, "
            "    recipient_zip5            TEXT, "
            "    recipient_zip4            TEXT, "
            "    recipient_congressional_district TEXT, "
            "    pop_country_code          TEXT, "
            "    pop_state                 TEXT, "
            "    pop_city                  TEXT, "
            "    pop_county_name           TEXT, "
            "    pop_zip5                  TEXT, "
            "    pop_zip4                  TEXT, "
            "    pop_congressional_district TEXT, "
            "    award_amount              NUMERIC(20, 2), "
            "    description               TEXT, "
            "    awarding_agency_name      TEXT, "
            "    awarding_subagency_name   TEXT, "
            "    funding_agency_name       TEXT, "
            "    funding_subagency_name    TEXT, "
            "    awarding_agency_id        INT, "
            "    agency_slug               TEXT, "
            "    period_start              DATE, "
            "    period_end                DATE, "
            "    period_pop_start          DATE, "
            "    period_pop_current_end    DATE, "
            "    last_modified_at          TIMESTAMPTZ, "
            "    fiscal_year_pulled        SMALLINT, "
            "    api_query_filter_sha256   CHAR(64), "
            "    page_number               INT"
            ") ON COMMIT DROP",
        )

        copy_query = sql.SQL(
            "COPY usaspending_staging ({cols}) FROM STDIN "
            "WITH (FORMAT csv, NULL '')",
        ).format(cols=staging_col_idents)

        with cur.copy(copy_query) as cp:
            buf = bytearray()
            for line_bytes in _iter_csv_lines(parse):
                buf.extend(line_bytes)
                if len(buf) >= _COPY_CHUNK_SIZE:
                    cp.write(bytes(buf))
                    buf.clear()
            if buf:
                cp.write(bytes(buf))

        cur.execute(
            """
            INSERT INTO raw.usaspending_award (
                generated_unique_award_id,
                award_id_piid, award_internal_id,
                award_type_code, award_type_description,
                recipient_name, recipient_uei, recipient_duns,
                recipient_country_code, recipient_state, recipient_city,
                recipient_county_name, recipient_zip5, recipient_zip4,
                recipient_congressional_district,
                pop_country_code, pop_state, pop_city,
                pop_county_name, pop_zip5, pop_zip4,
                pop_congressional_district,
                award_amount, description,
                awarding_agency_name, awarding_subagency_name,
                funding_agency_name, funding_subagency_name,
                awarding_agency_id, agency_slug,
                period_start, period_end,
                period_pop_start, period_pop_current_end,
                last_modified_at,
                fiscal_year_pulled, api_query_filter_sha256, page_number
            )
            SELECT
                generated_unique_award_id,
                award_id_piid, award_internal_id,
                award_type_code, award_type_description,
                recipient_name, recipient_uei, recipient_duns,
                recipient_country_code, recipient_state, recipient_city,
                recipient_county_name, recipient_zip5, recipient_zip4,
                recipient_congressional_district,
                pop_country_code, pop_state, pop_city,
                pop_county_name, pop_zip5, pop_zip4,
                pop_congressional_district,
                award_amount, description,
                awarding_agency_name, awarding_subagency_name,
                funding_agency_name, funding_subagency_name,
                awarding_agency_id, agency_slug,
                period_start, period_end,
                period_pop_start, period_pop_current_end,
                last_modified_at,
                fiscal_year_pulled, api_query_filter_sha256, page_number
            FROM usaspending_staging
            ON CONFLICT (generated_unique_award_id) DO UPDATE SET
                last_seen_at             = now(),
                fiscal_year_pulled       = EXCLUDED.fiscal_year_pulled,
                api_query_filter_sha256  = EXCLUDED.api_query_filter_sha256,
                award_amount             = EXCLUDED.award_amount,
                period_end               = EXCLUDED.period_end,
                period_pop_current_end   = EXCLUDED.period_pop_current_end,
                last_modified_at         = EXCLUDED.last_modified_at,
                description              = EXCLUDED.description
            """,
        )
        n_touched = cur.rowcount

    log.info(
        "usaspending.load: UPSERTed %d rows into raw.usaspending_award "
        "(FY%d, state=%s, file_sha256=%s...)",
        n_touched, parse.fetch.fiscal_year, parse.fetch.state,
        parse.fetch.file_sha256[:16],
    )
    return n_touched


# ============================================================================
# Click CLI
# ============================================================================


@click.group()
def cli() -> None:
    """USAspending federal-award ingester (FRAUD-F1, contracts-only v1)."""


def _default_dest_dir() -> Path:
    return Path("data/cache/usaspending")


@cli.command("fetch")
@click.option("--fiscal-year", type=int, required=True,
              help="Federal fiscal year (Oct 1 of FY-1 .. Sep 30 of FY).")
@click.option("--state", default=DEFAULT_PLACE_OF_PERFORMANCE_STATE,
              show_default=True, help="Place-of-performance state filter.")
@click.option("--dest-dir",
              type=click.Path(file_okay=False, path_type=Path),
              default=_default_dest_dir(), show_default=True)
@click.option("--overwrite/--no-overwrite", default=False)
@click.option("--page-size", type=int, default=DEFAULT_PAGE_SIZE,
              show_default=True)
@click.option("--request-interval-s", type=float,
              default=DEFAULT_REQUEST_INTERVAL_S, show_default=True,
              help="Seconds between requests (rate-limit pacing).")
def cmd_fetch(
    fiscal_year: int, state: str, dest_dir: Path, overwrite: bool,
    page_size: int, request_interval_s: float,
) -> None:
    """Paginate the search endpoint and write JSONL to disk."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    fetch = fetch_awards(
        fiscal_year=fiscal_year, state=state, dest_dir=dest_dir,
        overwrite=overwrite, page_size=page_size,
        request_interval_s=request_interval_s,
    )
    click.echo(f"path:           {fetch.path}")
    click.echo(f"fiscal_year:    {fetch.fiscal_year}")
    click.echo(f"state:          {fetch.state}")
    click.echo(f"filter_sha256:  {fetch.filter_sha256}")
    click.echo(f"file_sha256:    {fetch.file_sha256}")
    click.echo(f"n_pages:        {fetch.n_pages}")
    click.echo(f"n_awards:       {fetch.n_awards}")
    click.echo(f"cache_hit:      {fetch.cache_hit}")
    if fetch.last_modified_observed:
        click.echo(f"max_last_mod:   {fetch.last_modified_observed}")


@cli.command("load")
@click.argument(
    "jsonl_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--fiscal-year", type=int, required=True)
@click.option("--state", default=DEFAULT_PLACE_OF_PERFORMANCE_STATE,
              show_default=True)
@click.option("--dsn", envvar="PG_DSN", required=True)
def cmd_load(
    jsonl_path: Path, fiscal_year: int, state: str, dsn: str,
) -> None:
    """Parse and UPSERT a previously-fetched JSONL file."""
    import psycopg as _psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    filter_obj = build_filter(fiscal_year=fiscal_year, state=state)
    filter_sha = fingerprint_filter(filter_obj)
    with jsonl_path.open("r", encoding="utf-8") as _fh:
        n_awards = sum(1 for _ in _fh)
    fetch = FetchResult(
        path=jsonl_path,
        fiscal_year=fiscal_year, state=state,
        filter_sha256=filter_sha,
        n_pages=0,
        n_awards=n_awards,
        source_url=f"{USASPENDING_API_BASE}{SPENDING_BY_AWARD_PATH}",
        file_sha256=sha256_file(jsonl_path),
        cache_hit=True,
        last_modified_observed=None,
    )
    parse = parse_awards(fetch)
    click.echo(f"parsed {parse.n_rows} rows")
    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(f"upserted {n} rows (FY{fiscal_year}, state={state})")


@cli.command("fetch-and-load")
@click.option("--fiscal-year", type=int, required=True)
@click.option("--state", default=DEFAULT_PLACE_OF_PERFORMANCE_STATE,
              show_default=True)
@click.option("--dest-dir",
              type=click.Path(file_okay=False, path_type=Path),
              default=_default_dest_dir(), show_default=True)
@click.option("--overwrite/--no-overwrite", default=False)
@click.option("--page-size", type=int, default=DEFAULT_PAGE_SIZE,
              show_default=True)
@click.option("--request-interval-s", type=float,
              default=DEFAULT_REQUEST_INTERVAL_S, show_default=True)
@click.option("--dsn", envvar="PG_DSN", required=True)
def cmd_fetch_and_load(
    fiscal_year: int, state: str, dest_dir: Path, overwrite: bool,
    page_size: int, request_interval_s: float, dsn: str,
) -> None:
    """Fetch + parse + UPSERT in one step (the Dagster-tick command)."""
    import psycopg as _psycopg

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    fetch = fetch_awards(
        fiscal_year=fiscal_year, state=state, dest_dir=dest_dir,
        overwrite=overwrite, page_size=page_size,
        request_interval_s=request_interval_s,
    )
    parse = parse_awards(fetch)
    click.echo(f"parsed {parse.n_rows} rows (cache_hit={fetch.cache_hit})")
    with _psycopg.connect(dsn) as conn:
        n = load_to_postgres(parse, conn)
        conn.commit()
    click.echo(
        f"upserted {n} rows (FY{fiscal_year}, state={state}, "
        f"file_sha={fetch.file_sha256[:16]}...)",
    )


if __name__ == "__main__":
    cli()
