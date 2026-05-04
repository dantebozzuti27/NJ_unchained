"""Unit tests for ``ingestion._base``.

These cover the three responsibilities of ``_base.py``:

1. Provenance hashing -- ``sha256_file`` is byte-equivalent to the
   reference implementation (``hashlib.sha256(<contents>).hexdigest()``)
   for fixtures of various sizes.
2. Schema-version detection -- :func:`detect_schema_version` resolves
   the most-specific match, raises on no-match, and raises on unbroken
   ties.
3. Employer canonicalization -- the canonical name collapses across
   suffix and punctuation variants.

Property-based assertions (Hypothesis) are used for the canonicalizer
because the suffix-strip composition is non-trivial and naive unit tests
miss combinatorial edge cases.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ingestion._base import (
    IngestError,
    SchemaSignature,
    canonical_employer_name,
    canonicalize_column,
    detect_schema_version,
    sha256_file,
)

# ============================================================================
# sha256_file
# ============================================================================


@pytest.mark.parametrize("size", [0, 1, 1023, 1024, 1024 * 1024 + 7])
def test_sha256_file_matches_hashlib(tmp_path: Path, size: int) -> None:
    """sha256_file is byte-equivalent to hashlib.sha256 across boundary sizes."""
    blob = bytes((i & 0xFF) for i in range(size))
    f = tmp_path / "blob.bin"
    f.write_bytes(blob)
    assert sha256_file(f) == hashlib.sha256(blob).hexdigest()


# ============================================================================
# canonicalize_column
# ============================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("WAGE_RATE_OF_PAY_FROM", "wage_rate_of_pay_from"),
        ("Wage Rate Of Pay From", "wage_rate_of_pay_from"),
        ("  Wage_Rate_of_Pay_From  ", "wage_rate_of_pay_from"),
        ("WORKSITE\u00a0CITY", "worksite_city"),  # non-breaking space
        ("WORKSITE\u2014CITY", "worksite_city"),  # em-dash
        ("Worksite__City", "worksite_city"),  # doubled underscore
        ("EMPLOYER  NAICS  CODE", "employer_naics_code"),
    ],
)
def test_canonicalize_column(raw: str, expected: str) -> None:
    assert canonicalize_column(raw) == expected


# ============================================================================
# detect_schema_version
# ============================================================================


@pytest.fixture
def sigs() -> tuple[SchemaSignature, ...]:
    """Two-version registry used to test the resolver in isolation."""
    return (
        SchemaSignature(
            name="vA",
            required_columns=frozenset({"a", "b"}),
            forbidden_columns=frozenset({"c"}),
        ),
        SchemaSignature(
            name="vB",
            required_columns=frozenset({"a", "b", "c"}),
        ),
    )


def test_detect_schema_returns_specific_match(sigs: tuple[SchemaSignature, ...]) -> None:
    assert detect_schema_version(["a", "b", "c"], sigs) == "vB"
    assert detect_schema_version(["A", "B"], sigs) == "vA"


def test_detect_schema_canonicalizes_input(sigs: tuple[SchemaSignature, ...]) -> None:
    """Casing/punctuation variants in the input must not defeat detection."""
    assert detect_schema_version(["A", "B ", "  c  "], sigs) == "vB"


def test_detect_schema_no_match_raises(sigs: tuple[SchemaSignature, ...]) -> None:
    with pytest.raises(IngestError, match="No schema signature matches"):
        detect_schema_version(["a"], sigs)


def test_detect_schema_tie_raises() -> None:
    """Two equally-specific matches must raise rather than guess."""
    sigs = (
        SchemaSignature(name="X", required_columns=frozenset({"a", "b"})),
        SchemaSignature(name="Y", required_columns=frozenset({"a", "b"})),
    )
    with pytest.raises(IngestError, match="Ambiguous schema match"):
        detect_schema_version(["a", "b"], sigs)


# ============================================================================
# canonical_employer_name
# ============================================================================


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("Tata Consultancy Services Ltd.",     "tata consultancy services"),
        ("TATA CONSULTANCY SERVICES, LLC",     "tata consultancy services"),
        ("Tata Consultancy Services L.L.C.",   "tata consultancy services"),
        ("Tata Consultancy Services LLC",      "tata consultancy services"),
        ("The Goldman Sachs Group, Inc.",      "goldman sachs"),
        ("Goldman, Sachs & Co.",               "goldman sachs"),
        ("CRÉDIT AGRICOLE CORPORATE & INVESTMENT BANK",
         "credit agricole corporate investment bank"),
    ],
)
def test_canonical_employer_collapses_variants(variant: str, expected: str) -> None:
    assert canonical_employer_name(variant) == expected


def test_canonical_employer_empty_string() -> None:
    assert canonical_employer_name("") == ""


@given(st.text(min_size=0, max_size=64))
def test_canonical_employer_is_idempotent(name: str) -> None:
    """Running the canonicalizer on its own output must be a fixed point."""
    once = canonical_employer_name(name)
    twice = canonical_employer_name(once)
    assert once == twice


@given(st.text(alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
              min_size=1, max_size=32))
def test_canonical_employer_lowercase_input_unchanged_modulo_whitespace(name: str) -> None:
    """Plain lowercase ASCII names that are NOT business suffixes pass through.

    The exclusion set must mirror :data:`ingestion._base._SUFFIX_PATTERNS`
    EXACTLY (modulo regex syntax). Missing a suffix here causes
    Hypothesis to occasionally land on a falsifying example like
    'pa' or 'lp' that the canonicalizer legitimately strips.
    """
    suffixes = {
        "llc", "llp", "pllc", "lp", "pc", "pa",
        "inc", "incorporated",
        "corp", "corporation",
        "co", "company",
        "ltd", "limited",
        "holding", "holdings",
        "group", "the",
    }
    if name in suffixes or any(s in name for s in suffixes):
        return
    assert canonical_employer_name(name) == name.strip()
