"""Tests for the USAspending federal-award ingester (FRAUD-F1 substrate).

Test taxonomy
-------------
1. Pure helpers (no I/O)
   - fiscal_year_window arithmetic and bounds
   - fingerprint_filter is stable across runs
   - _coerce_date / _coerce_datetime / _coerce_amount
   - _flatten_location for both populated and null subobjects
   - _award_type_code_from infers from generated_internal_id prefix
     when the API field is null

2. Pure parser (no DB)
   - parse_awards on a synthetic JSONL fixture
   - Refuses rows with no recipient identity at all
   - Refuses rows missing generated_internal_id
   - Empty / blank lines are skipped

3. Paginator (mocked HTTP)
   - fetch_awards retries on 429 then succeeds
   - Multi-page response: hasNext=True iterates; hasNext=False stops
   - De-duplicates if API returns same award_id twice
   - Cache hit: re-running with overwrite=False reads disk

4. Integration (live_pg)
   - Apply migrations + seeds; load synthetic JSONL; verify
     raw.usaspending_award + v_usaspending_award_active.
   - Idempotent reload: same file twice = same row count, last_seen_at
     bumped.
   - Negative-amount row (de-obligation) survives the loader without
     constraint violation.
   - Recipient state may legitimately != 'NJ' (out-of-state company
     performing NJ work); pop_state must always == 'NJ'.
   - ref.release_calendar entry exists with cadence='monthly'.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from ingestion._base import IngestError
from ingestion.usaspending import (
    DEFAULT_AWARD_TYPE_CODES,
    DEFAULT_PLACE_OF_PERFORMANCE_STATE,
    EARLIEST_FY,
    FetchResult,
    _award_type_code_from,
    _coerce_amount,
    _coerce_date,
    _coerce_datetime,
    _flatten_location,
    build_filter,
    fetch_awards,
    fingerprint_filter,
    fiscal_year_window,
    load_to_postgres,
    parse_awards,
)

if TYPE_CHECKING:
    import psycopg


# ============================================================================
# 1. Pure helpers
# ============================================================================


def test_fiscal_year_window_arithmetic_is_correct() -> None:
    """FY N runs Oct 1 of N-1 through Sep 30 of N."""
    assert fiscal_year_window(2024) == ("2023-10-01", "2024-09-30")
    assert fiscal_year_window(2008) == ("2007-10-01", "2008-09-30")


def test_fiscal_year_window_rejects_pre_endpoint_data() -> None:
    """The /search endpoint stops at FY2008; pre-2008 must error.

    Older data requires the bulk-download endpoint, which the platform
    does not implement.
    """
    with pytest.raises(ValueError, match="older than"):
        fiscal_year_window(2007)
    # Sanity: 2008 is the boundary, must succeed.
    fiscal_year_window(EARLIEST_FY)


def test_build_filter_shape_is_canonical() -> None:
    """The filter object has the exact shape USAspending expects."""
    f = build_filter(fiscal_year=2024)
    assert f["place_of_performance_locations"] == [
        {"country": "USA", "state": "NJ"},
    ]
    assert f["award_type_codes"] == list(DEFAULT_AWARD_TYPE_CODES)
    assert f["time_period"] == [
        {"start_date": "2023-10-01", "end_date": "2024-09-30"},
    ]


def test_fingerprint_filter_is_stable_and_collision_resistant() -> None:
    """Equal filters produce equal fingerprints; different filters differ."""
    f1 = build_filter(fiscal_year=2024)
    f2 = build_filter(fiscal_year=2024)
    f3 = build_filter(fiscal_year=2025)
    f4 = build_filter(fiscal_year=2024, state="NY")
    f5 = build_filter(
        fiscal_year=2024, award_type_codes=("A", "B"),
    )

    assert fingerprint_filter(f1) == fingerprint_filter(f2)
    assert len(fingerprint_filter(f1)) == 64
    assert all(c in "0123456789abcdef" for c in fingerprint_filter(f1))
    assert fingerprint_filter(f1) != fingerprint_filter(f3)
    assert fingerprint_filter(f1) != fingerprint_filter(f4)
    assert fingerprint_filter(f1) != fingerprint_filter(f5)


def test_fingerprint_is_independent_of_dict_key_order() -> None:
    """Reordering keys must not change the fingerprint.

    A future change that shuffles dict literal order in build_filter
    must not break operator's stored fingerprints.
    """
    f1 = {"a": 1, "b": [{"x": 1, "y": 2}]}
    f2 = {"b": [{"y": 2, "x": 1}], "a": 1}
    assert fingerprint_filter(f1) == fingerprint_filter(f2)


def test_coerce_date_accepts_iso_and_blank() -> None:
    assert _coerce_date("2024-05-15", field_name="x") == dt.date(2024, 5, 15)
    assert _coerce_date("", field_name="x") is None
    assert _coerce_date(None, field_name="x") is None


def test_coerce_date_rejects_malformed() -> None:
    """A non-ISO string is a parser bug, not a soft drop."""
    with pytest.raises(IngestError, match="not YYYY-MM-DD"):
        _coerce_date("05/15/2024", field_name="x")
    with pytest.raises(IngestError, match="not YYYY-MM-DD"):
        _coerce_date("2024-5-15", field_name="x")


def test_coerce_datetime_accepts_full_format() -> None:
    """API uses 'YYYY-MM-DD HH:MM:SS' (space, not T) for Last Modified."""
    got = _coerce_datetime("2025-09-23 07:34:41", field_name="x")
    assert got == dt.datetime(2025, 9, 23, 7, 34, 41)
    assert _coerce_datetime(None, field_name="x") is None
    assert _coerce_datetime("", field_name="x") is None


def test_coerce_datetime_rejects_malformed() -> None:
    with pytest.raises(IngestError):
        _coerce_datetime("2025-09-23T07:34:41", field_name="x")


def test_coerce_amount_accepts_int_float_and_null() -> None:
    assert _coerce_amount(None) is None
    assert _coerce_amount(0) == 0.0
    assert _coerce_amount(1234.5) == 1234.5
    assert _coerce_amount(-1000) == -1000.0  # de-obligation


def test_coerce_amount_rejects_bool_and_string() -> None:
    """API contract is Number; anything else is a parser bug."""
    with pytest.raises(IngestError):
        _coerce_amount(True)
    with pytest.raises(IngestError):
        _coerce_amount("1000")


def test_flatten_location_handles_null() -> None:
    """A null location produces all-null fields, not a KeyError."""
    out = _flatten_location(None, kind="Recipient")
    assert all(v is None for v in out.values())
    assert set(out.keys()) == {
        "country_code", "state", "city", "county_name",
        "zip5", "zip4", "congressional_district",
    }


def test_flatten_location_extracts_all_subkeys() -> None:
    loc = {
        "location_country_code": "USA",
        "state_code": "NJ",
        "city_name": "TRENTON",
        "county_name": "MERCER",
        "zip5": "08608",
        "zip4": "1234",
        "congressional_code": "12",
        "address_line1": "ignored",
    }
    out = _flatten_location(loc, kind="Place of Performance")
    assert out["state"] == "NJ"
    assert out["city"] == "TRENTON"
    assert out["zip5"] == "08608"
    assert out["zip4"] == "1234"
    assert out["county_name"] == "MERCER"
    assert out["congressional_district"] == "12"


def test_flatten_location_treats_blanks_as_null() -> None:
    """Empty-string subfields are treated as None, not literal ''."""
    loc = {"state_code": "", "city_name": "  ", "zip5": "08608"}
    out = _flatten_location(loc, kind="Recipient")
    assert out["state"] is None
    assert out["city"] is None
    assert out["zip5"] == "08608"


def test_flatten_location_rejects_wrong_type() -> None:
    """A list or scalar where an object is expected is a parser bug."""
    with pytest.raises(IngestError):
        _flatten_location([1, 2], kind="Recipient")
    with pytest.raises(IngestError):
        _flatten_location("oops", kind="Recipient")


def test_award_type_code_from_uses_api_field_when_present() -> None:
    """The API's 'Award Type' field, when populated with A/B/C/D, wins."""
    obj = {"Award Type": "C"}
    assert _award_type_code_from(obj, "CONT_AWD_xyz") == "C"


def test_award_type_code_from_falls_back_to_prefix() -> None:
    """When 'Award Type' is null, the prefix tells us it's a contract."""
    obj: dict[str, Any] = {"Award Type": None}
    assert _award_type_code_from(obj, "CONT_AWD_WE31_9700") == "D"
    assert _award_type_code_from(obj, "CONT_IDV_xyz") == "D"


def test_award_type_code_from_rejects_non_contract_prefix() -> None:
    """A grant-prefixed row in a contract pull is a filter regression."""
    obj: dict[str, Any] = {"Award Type": None}
    with pytest.raises(IngestError, match="cannot resolve"):
        _award_type_code_from(obj, "ASST_AWD_xyz")


# ============================================================================
# 2. Pure parser (no DB)
# ============================================================================


def _make_award(
    award_id: str,
    *,
    recipient_name: str | None = "TETRA TECH INC",
    recipient_uei: str | None = "HVDMNK1MYQN3",
    pop_state: str | None = "NJ",
    award_amount: float | None = 1234.0,
    award_type: str | None = None,
) -> dict[str, Any]:
    """Build a minimal API-shape award dict for fixtures."""
    return {
        "Award ID":                    "WE31",
        "Recipient Name":              recipient_name,
        "Recipient UEI":               recipient_uei,
        "Recipient DUNS":              None,
        "Award Amount":                award_amount,
        "Awarding Agency":             "Department of Defense",
        "Awarding Sub Agency":         "Department of the Navy",
        "Award Type":                  award_type,
        "Start Date":                  "2017-05-26",
        "End Date":                    "2027-09-30",
        "Description":                 "TEST",
        "generated_internal_id":       award_id,
        "internal_id":                 350598169,
        "Recipient Location": {
            "location_country_code": "USA",
            "state_code": "VA",
            "city_name": "VIRGINIA BEACH",
            "zip5": "23462",
            "zip4": "3352",
            "county_name": "VIRGINIA BEACH (CITY)",
            "congressional_code": "02",
        },
        "Place of Performance": {
            "location_country_code": "USA",
            "state_code": pop_state,
            "city_name": "TRENTON",
            "zip5": "08608",
            "county_name": "MERCER",
            "congressional_code": "12",
        } if pop_state is not None else None,
        "Last Modified Date":          "2025-09-23 07:34:41",
        "Period of Performance Start Date": None,
        "Period of Performance Current End Date": None,
        "agency_slug":                 "department-of-defense",
        "awarding_agency_id":          1173,
    }


def _write_jsonl(tmp_path: Path, records: list[dict[str, Any]]) -> FetchResult:
    p = tmp_path / "test.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(",", ":")))
            fh.write("\n")
    return FetchResult(
        path=p, fiscal_year=2024,
        state=DEFAULT_PLACE_OF_PERFORMANCE_STATE,
        filter_sha256="0" * 64,
        n_pages=1, n_awards=len(records),
        source_url="https://api.usaspending.gov/api/v2/search/spending_by_award/",
        file_sha256="f" * 64,
        cache_hit=False,
        last_modified_observed=None,
    )


def test_parse_happy_path_three_rows(tmp_path: Path) -> None:
    """Round-trip: API-shape records -> canonicalized row dicts."""
    records = [
        _make_award("CONT_AWD_001"),
        _make_award("CONT_AWD_002", recipient_name="ACME CORP"),
        _make_award("CONT_AWD_003", award_type="A"),
    ]
    fetch = _write_jsonl(tmp_path, records)
    parsed = parse_awards(fetch)
    assert parsed.n_rows == 3

    row0 = parsed.rows[0]
    assert row0["generated_unique_award_id"] == "CONT_AWD_001"
    assert row0["recipient_name"] == "TETRA TECH INC"
    assert row0["recipient_state"] == "VA"
    assert row0["pop_state"] == "NJ"
    assert row0["award_amount"] == 1234.0
    assert row0["award_type_code"] == "D"  # inferred from CONT_AWD_ prefix
    assert row0["period_start"] == dt.date(2017, 5, 26)
    assert row0["last_modified_at"] == dt.datetime(2025, 9, 23, 7, 34, 41)

    row2 = parsed.rows[2]
    assert row2["award_type_code"] == "A"  # from API field


def test_parse_skips_blank_lines(tmp_path: Path) -> None:
    """Blank lines in the JSONL are skipped, not raised."""
    p = tmp_path / "blanks.jsonl"
    rec = _make_award("CONT_AWD_001")
    p.write_text(
        "\n"
        + json.dumps(rec, separators=(",", ":"))
        + "\n\n",
        encoding="utf-8",
    )
    fetch = FetchResult(
        path=p, fiscal_year=2024,
        state=DEFAULT_PLACE_OF_PERFORMANCE_STATE,
        filter_sha256="0" * 64,
        n_pages=1, n_awards=1,
        source_url="x", file_sha256="f" * 64,
        cache_hit=False, last_modified_observed=None,
    )
    parsed = parse_awards(fetch)
    assert parsed.n_rows == 1


def test_parse_rejects_missing_generated_internal_id(tmp_path: Path) -> None:
    """A row without the PK field is a parser-loud failure, not a drop."""
    rec = _make_award("CONT_AWD_001")
    del rec["generated_internal_id"]
    fetch = _write_jsonl(tmp_path, [rec])
    with pytest.raises(IngestError, match="generated_internal_id"):
        parse_awards(fetch)


def test_parse_rejects_no_recipient_identity(tmp_path: Path) -> None:
    """A row with no name, UEI, or DUNS violates the CHECK constraint."""
    rec = _make_award(
        "CONT_AWD_001", recipient_name=None, recipient_uei=None,
    )
    rec["Recipient DUNS"] = None
    fetch = _write_jsonl(tmp_path, [rec])
    with pytest.raises(IngestError, match="neither name, UEI, nor DUNS"):
        parse_awards(fetch)


def test_parse_rejects_invalid_json(tmp_path: Path) -> None:
    """A malformed JSON line surfaces with a clear line number."""
    p = tmp_path / "bad.jsonl"
    p.write_text('{"not": "valid json\n', encoding="utf-8")
    fetch = FetchResult(
        path=p, fiscal_year=2024,
        state=DEFAULT_PLACE_OF_PERFORMANCE_STATE,
        filter_sha256="0" * 64,
        n_pages=1, n_awards=0,
        source_url="x", file_sha256="f" * 64,
        cache_hit=False, last_modified_observed=None,
    )
    with pytest.raises(IngestError, match="line 1"):
        parse_awards(fetch)


def test_parse_handles_null_place_of_performance(tmp_path: Path) -> None:
    """The API sometimes returns null Place of Performance; loader still proceeds.

    The row's pop_state will be NULL; the asset check
    `pop_state_nj_invariant` accepts NULL (treats it as
    ``unknown but not contradictory``).
    """
    rec = _make_award("CONT_AWD_001", pop_state=None)
    fetch = _write_jsonl(tmp_path, [rec])
    parsed = parse_awards(fetch)
    assert parsed.rows[0]["pop_state"] is None


# ============================================================================
# 3. Paginator (mocked HTTP)
# ============================================================================


class _MockTransport(httpx.BaseTransport):
    """A tiny in-memory mock that returns canned responses in order.

    Each call to handle_request pops the next entry from `responses`;
    the test provides a list of (status_code, body) tuples or raw
    httpx.Response objects.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        if not self._responses:
            raise AssertionError(
                "MockTransport: ran out of canned responses",
            )
        nxt = self._responses.pop(0)
        if isinstance(nxt, httpx.Response):
            return nxt
        status, payload = nxt
        return httpx.Response(
            status_code=status,
            json=payload,
            request=request,
        )


def _ok(results: list[dict[str, Any]], *, has_next: bool) -> tuple[int, dict[str, Any]]:
    return (200, {
        "results": results,
        "page_metadata": {"hasNext": has_next, "page": 1},
    })


def test_fetch_paginates_until_has_next_false(tmp_path: Path) -> None:
    """Multi-page response: fetcher continues until hasNext=False."""
    page1 = [_make_award("CONT_AWD_001"), _make_award("CONT_AWD_002")]
    page2 = [_make_award("CONT_AWD_003")]
    transport = _MockTransport([
        _ok(page1, has_next=True),
        _ok(page2, has_next=False),
    ])
    client = httpx.Client(transport=transport)
    try:
        result = fetch_awards(
            fiscal_year=2024, dest_dir=tmp_path / "cache",
            request_interval_s=0.0, http_client=client, overwrite=True,
        )
    finally:
        client.close()

    assert result.n_pages == 2
    assert result.n_awards == 3
    assert result.cache_hit is False
    # Both pages were requested.
    assert len(transport.calls) == 2
    assert transport.calls[0]["page"] == 1
    assert transport.calls[1]["page"] == 2


def test_fetch_dedupes_repeated_award_ids(tmp_path: Path) -> None:
    """If the API returns the same award on two pages (rare, but possible
    when a transaction modification lands mid-paginate), only one row is
    written.
    """
    page1 = [_make_award("CONT_AWD_001")]
    page2 = [_make_award("CONT_AWD_001"), _make_award("CONT_AWD_002")]
    transport = _MockTransport([
        _ok(page1, has_next=True),
        _ok(page2, has_next=False),
    ])
    client = httpx.Client(transport=transport)
    try:
        result = fetch_awards(
            fiscal_year=2024, dest_dir=tmp_path / "cache",
            request_interval_s=0.0, http_client=client, overwrite=True,
        )
    finally:
        client.close()
    assert result.n_awards == 2  # not 3; the dup was elided


def test_fetch_retries_on_429(tmp_path: Path) -> None:
    """A 429 is a soft failure; the fetcher backs off and retries."""
    transport = _MockTransport([
        (429, {"detail": "rate limit"}),
        _ok([_make_award("CONT_AWD_001")], has_next=False),
    ])
    client = httpx.Client(transport=transport)
    try:
        result = fetch_awards(
            fiscal_year=2024, dest_dir=tmp_path / "cache",
            request_interval_s=0.0, http_client=client, overwrite=True,
            max_retries=3,
        )
    finally:
        client.close()
    assert result.n_awards == 1


def test_fetch_raises_on_4xx_other_than_429(tmp_path: Path) -> None:
    """A 400 means our payload is wrong; surface loud, do not retry."""
    transport = _MockTransport([
        (400, {"detail": "bad request"}),
    ])
    client = httpx.Client(transport=transport)
    try:
        with pytest.raises(IngestError, match="400"):
            fetch_awards(
                fiscal_year=2024, dest_dir=tmp_path / "cache",
                request_interval_s=0.0, http_client=client, overwrite=True,
                max_retries=1,
            )
    finally:
        client.close()


def test_fetch_cache_hit_skips_http(tmp_path: Path) -> None:
    """A complete on-disk JSONL with the same fingerprint = cache hit.

    No HTTP calls are made on the second invocation.
    """
    page1 = [_make_award("CONT_AWD_001")]
    transport = _MockTransport([_ok(page1, has_next=False)])
    client = httpx.Client(transport=transport)
    try:
        first = fetch_awards(
            fiscal_year=2024, dest_dir=tmp_path / "cache",
            request_interval_s=0.0, http_client=client, overwrite=True,
        )
    finally:
        client.close()
    assert first.cache_hit is False
    assert len(transport.calls) == 1

    # Second client must not be hit; we pass it but expect zero calls.
    second_transport = _MockTransport([])
    second_client = httpx.Client(transport=second_transport)
    try:
        second = fetch_awards(
            fiscal_year=2024, dest_dir=tmp_path / "cache",
            request_interval_s=0.0, http_client=second_client,
            overwrite=False,
        )
    finally:
        second_client.close()

    assert second.cache_hit is True
    assert second.n_awards == 1
    assert len(second_transport.calls) == 0


def test_fetch_refuses_row_without_generated_internal_id(tmp_path: Path) -> None:
    """A row missing the PK field at fetch time is a fail-loud surface."""
    bad_award = _make_award("CONT_AWD_001")
    del bad_award["generated_internal_id"]
    transport = _MockTransport([
        _ok([bad_award], has_next=False),
    ])
    client = httpx.Client(transport=transport)
    try:
        with pytest.raises(IngestError, match="generated_internal_id"):
            fetch_awards(
                fiscal_year=2024, dest_dir=tmp_path / "cache",
                request_interval_s=0.0, http_client=client, overwrite=True,
            )
    finally:
        client.close()


# ============================================================================
# 4. Integration (live_pg)
# ============================================================================


@pytest.fixture
def usaspending_db(live_pg: psycopg.Connection) -> psycopg.Connection:
    """Apply all migrations + seeds; yield the connection."""
    from scripts.migrate import (
        MIGRATIONS_DIR,
        SEEDS_DIR,
        apply_migrations,
        discover,
    )
    conn = live_pg
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS governance CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS derived    CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS raw        CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ref        CASCADE")
        cur.execute(
            "DO $$ "
            "DECLARE r record; "
            "BEGIN "
            "  FOR r IN SELECT viewname FROM pg_views "
            "           WHERE schemaname='public' AND viewname LIKE 'v_%%' LOOP "
            "    EXECUTE 'DROP VIEW IF EXISTS public.' || quote_ident(r.viewname) || ' CASCADE'; "
            "  END LOOP; "
            "END $$;",
        )
    conn.commit()
    apply_migrations(conn, discover(MIGRATIONS_DIR))
    apply_migrations(conn, discover(SEEDS_DIR))
    return conn


@pytest.mark.live_pg
def test_round_trip_load_three_awards(
    usaspending_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Happy path: 3 synthetic awards land in raw.usaspending_award."""
    records = [
        _make_award("CONT_AWD_001"),
        _make_award("CONT_AWD_002", recipient_name="ACME CORP"),
        _make_award("CONT_AWD_003", award_type="A", award_amount=-50000.0),
    ]
    fetch = _write_jsonl(tmp_path, records)
    parsed = parse_awards(fetch)
    n = load_to_postgres(parsed, usaspending_db)
    usaspending_db.commit()
    assert n == 3

    with usaspending_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.usaspending_award")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 3

        cur.execute(
            "SELECT generated_unique_award_id, award_type_code, "
            "award_amount, pop_state, recipient_state "
            "FROM raw.usaspending_award ORDER BY generated_unique_award_id",
        )
        rows = cur.fetchall()
        assert rows[0][0] == "CONT_AWD_001"
        assert rows[0][1] == "D"
        assert rows[0][2] == 1234.00
        assert rows[0][3] == "NJ"            # filter pin invariant
        assert rows[0][4] == "VA"            # NJ work, out-of-state company
        # De-obligation: amount is legitimately negative.
        assert rows[2][1] == "A"
        assert rows[2][2] == -50000.00


@pytest.mark.live_pg
def test_active_view_picks_up_freshly_loaded_rows(
    usaspending_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """v_usaspending_award_active reflects the 35-day active window."""
    records = [_make_award("CONT_AWD_001"), _make_award("CONT_AWD_002")]
    fetch = _write_jsonl(tmp_path, records)
    parsed = parse_awards(fetch)
    load_to_postgres(parsed, usaspending_db)
    usaspending_db.commit()

    with usaspending_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM derived.v_usaspending_award_active")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 2


@pytest.mark.live_pg
def test_idempotent_reload_is_a_no_op_on_row_count(
    usaspending_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """Loading the same file twice leaves the row count unchanged."""
    records = [_make_award("CONT_AWD_001"), _make_award("CONT_AWD_002")]
    fetch = _write_jsonl(tmp_path, records)
    parsed = parse_awards(fetch)
    load_to_postgres(parsed, usaspending_db)
    usaspending_db.commit()

    parsed2 = parse_awards(fetch)
    n2 = load_to_postgres(parsed2, usaspending_db)
    usaspending_db.commit()
    assert n2 == 2

    with usaspending_db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw.usaspending_award")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 2  # not 4


@pytest.mark.live_pg
def test_idempotent_reload_bumps_last_seen_at(
    usaspending_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """The second load advances last_seen_at on existing rows."""
    records = [_make_award("CONT_AWD_001")]
    fetch = _write_jsonl(tmp_path, records)
    parsed = parse_awards(fetch)
    load_to_postgres(parsed, usaspending_db)
    usaspending_db.commit()

    with usaspending_db.cursor() as cur:
        cur.execute(
            "SELECT last_seen_at FROM raw.usaspending_award "
            "WHERE generated_unique_award_id = 'CONT_AWD_001'",
        )
        row = cur.fetchone()
        assert row is not None
        first_seen = row[0]

    # Reload after a small wait so the timestamp advances detectably.
    import time
    time.sleep(0.05)
    parsed2 = parse_awards(fetch)
    load_to_postgres(parsed2, usaspending_db)
    usaspending_db.commit()

    with usaspending_db.cursor() as cur:
        cur.execute(
            "SELECT last_seen_at FROM raw.usaspending_award "
            "WHERE generated_unique_award_id = 'CONT_AWD_001'",
        )
        row = cur.fetchone()
        assert row is not None
        second_seen = row[0]

    assert second_seen > first_seen


@pytest.mark.live_pg
def test_release_calendar_entry_exists(
    usaspending_db: psycopg.Connection,
) -> None:
    """The seed installed a release_calendar row with cadence=monthly."""
    with usaspending_db.cursor() as cur:
        cur.execute(
            "SELECT cadence FROM ref.release_calendar "
            "WHERE source_id = 'raw.usaspending_award'",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "monthly"


@pytest.mark.live_pg
def test_loader_rejects_zero_row_pull(
    usaspending_db: psycopg.Connection, tmp_path: Path,
) -> None:
    """An empty parse must raise IngestError, not silently no-op.

    A 0-row fetch is either a parser bug or a wholly empty FY; either
    way the operator must see it. Substrate-honesty: silent no-op
    loads corrupt the audit trail.
    """
    fetch = _write_jsonl(tmp_path, [])
    parsed = parse_awards(fetch)
    assert parsed.n_rows == 0
    with pytest.raises(IngestError, match="0 parsed rows"):
        load_to_postgres(parsed, usaspending_db)
