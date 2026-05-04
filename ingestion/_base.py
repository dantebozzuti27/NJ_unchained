"""Shared utilities used across ingester modules.

Everything in this module is pure-Python, side-effect-free (modulo file I/O
in :func:`sha256_file`), and exhaustively unit-tested. Loaders should depend
on these helpers rather than re-implementing them, so canonicalization
remains identical across sources.

Three concerns live here:

* **Provenance hashing** -- :func:`sha256_file` produces the exact value
  stored in ``raw.*.source_sha256``.
* **Schema-version detection** -- :class:`SchemaSignature` and
  :func:`detect_schema_version` implement the "canonical column set
  fingerprint" pattern. The fingerprint is the SHA-256 of a
  newline-separated, alphabetized list of canonicalized column names. This
  is more robust than detecting by sentinel column because it survives DOL
  adding ancillary columns to a release.
* **Employer-name canonicalization** -- :func:`canonical_employer_name`
  produces the value stored in ``raw.lca_disclosure.employer_canonical_name``
  (and the analogous columns in future ingesters). Stable across NFKD
  punctuation variants and business-suffix differences.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------


class IngestError(RuntimeError):
    """Raised by ingesters on any condition that would corrupt the audit trail.

    Specifically: unrecognized schema versions, primary-key collisions during
    a load, ratio-sum invariants violated post-load, and any I/O failure that
    leaves a partially-loaded staging table.
    """


# ----------------------------------------------------------------------------
# Provenance
# ----------------------------------------------------------------------------

_SHA_BUFSIZE = 1 << 20  # 1 MiB


def sha256_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 of *path*.

    Streamed in 1 MiB chunks so multi-GB DOL OFLC LCA files do not spike
    memory. The output is exactly the value stored in
    ``raw.*.source_sha256`` columns and is the canonical provenance
    fingerprint for any downloaded artifact.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_SHA_BUFSIZE):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------
# Schema-version detection
# ----------------------------------------------------------------------------


def canonicalize_column(col: str) -> str:
    """Return a column name normalized for fingerprint comparison.

    Lower-cased, NFKD-decomposed, with all non-alphanumeric characters
    collapsed to a single underscore and edge underscores stripped. The
    result is stable against:

    * leading/trailing whitespace
    * mixed-case headers
    * Unicode dashes / non-breaking spaces sneaking into headers
    * doubled-underscore variants (``WORKSITE__CITY`` -> ``worksite_city``)
    """
    nfkd = unicodedata.normalize("NFKD", col)
    # Replace non-ASCII characters with a space (em-dash, NBSP, etc.) so they
    # become word separators in the regex pass below, rather than being
    # silently dropped (which would collapse 'WORKSITE\u2014CITY' to
    # 'worksitecity'). Then ASCII-encode to drop combining marks left over
    # from the NFKD decomposition (accents on letters).
    no_unicode = "".join(c if ord(c) < 128 else " " for c in nfkd)
    ascii_only = no_unicode.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    underscored = re.sub(r"[^a-z0-9]+", "_", lowered)
    return underscored.strip("_")


# Backward-compatibility alias retained only for clarity in callers that
# treat the canonicalizer as an internal helper. Prefer ``canonicalize_column``.
_canonicalize_column = canonicalize_column


@dataclass(frozen=True)
class SchemaSignature:
    """A versioned signature for a known source-schema vintage.

    ``required_columns`` are columns that MUST be present (in their
    canonicalized form) for the signature to match. ``forbidden_columns``
    are columns that, if present, disqualify the signature -- used to
    distinguish later vintages that added columns from earlier ones.

    The match score is the ratio of required columns present, used as a
    tie-breaker when two signatures both match all of their required
    columns (which can happen because v5_2023 is a strict superset of
    v4_2020 minus a few additions).
    """

    name: str
    required_columns: frozenset[str]
    forbidden_columns: frozenset[str] = frozenset()

    def matches(self, observed: frozenset[str]) -> bool:
        """Return True iff every required column is present and no forbidden one is."""
        if not self.required_columns.issubset(observed):
            return False
        return self.forbidden_columns.isdisjoint(observed)

    def specificity(self) -> int:
        """How specific this signature is. More forbidden cols => more specific."""
        return len(self.required_columns) + 2 * len(self.forbidden_columns)


def detect_schema_version(
    columns: Iterable[str],
    signatures: Iterable[SchemaSignature],
) -> str:
    """Return the name of the most-specific matching signature.

    Args:
        columns: Raw column headers from the source file. Will be
            canonicalized via :func:`canonicalize_column`.
        signatures: Ordered set of known signatures. Order does not matter
            for correctness; ties are broken by :meth:`SchemaSignature.specificity`.

    Raises:
        IngestError: if zero signatures match, or if multiple signatures
            tie on specificity. A tie means the signatures are
            non-discriminating and the registry needs more
            ``forbidden_columns`` constraints; we refuse to guess.

    """
    observed = frozenset(canonicalize_column(c) for c in columns)
    candidates = [sig for sig in signatures if sig.matches(observed)]

    if not candidates:
        raise IngestError(
            "No schema signature matches the observed columns. "
            f"Observed (first 10): {sorted(observed)[:10]}"
        )

    candidates.sort(key=SchemaSignature.specificity, reverse=True)
    if len(candidates) > 1 and candidates[0].specificity() == candidates[1].specificity():
        names = [c.name for c in candidates if c.specificity() == candidates[0].specificity()]
        raise IngestError(
            f"Ambiguous schema match between {names}. Add forbidden_columns "
            "to disambiguate."
        )
    return candidates[0].name


# ----------------------------------------------------------------------------
# Employer-name canonicalization
# ----------------------------------------------------------------------------

# Business-entity suffixes to strip during canonicalization. Order matters
# only for tracing; the regex below handles all of them in a single pass.
_SUFFIX_PATTERNS: tuple[str, ...] = (
    r"l\.?l\.?c\.?",           # LLC, L.L.C., LLC.
    r"l\.?l\.?p\.?",           # LLP
    r"p\.?l\.?l\.?c\.?",       # PLLC
    r"l\.?p\.?",               # LP, L.P.
    r"p\.?c\.?",               # PC, P.C.
    r"p\.?a\.?",               # PA, P.A.
    r"inc\.?(orporated)?",     # INC, INCORPORATED
    r"corp\.?(oration)?",      # CORP, CORPORATION
    r"co\.?(mpany)?",          # CO, COMPANY
    r"ltd\.?",                 # LTD
    r"limited",                # LIMITED
    r"holdings?",              # HOLDING / HOLDINGS
    r"group",                  # GROUP
    r"the",                    # leading 'THE'
)
_SUFFIX_REGEX = re.compile(
    r"\b(?:" + "|".join(_SUFFIX_PATTERNS) + r")\b\.?",
    flags=re.IGNORECASE,
)
_PUNCT_REGEX = re.compile(r"[^a-z0-9]+")
_MULTISPACE_REGEX = re.compile(r"\s+")


def canonical_employer_name(name: str) -> str:
    """Return a canonical form for *name*, suitable for grouping employers.

    The transform is:

    1. NFKD Unicode normalization (collapses accented characters).
    2. Lowercase.
    3. Strip business-entity suffixes (LLC, INC, CORP, ...) including
       punctuation variants (``L.L.C.`` and ``LLC`` collapse to identical
       output).
    4. Collapse all non-alphanumeric runs to a single space.
    5. Strip leading/trailing whitespace.

    Examples
    --------
    >>> canonical_employer_name("Tata Consultancy Services Ltd.")
    'tata consultancy services'
    >>> canonical_employer_name("TATA CONSULTANCY SERVICES, LLC")
    'tata consultancy services'
    >>> canonical_employer_name("Tata Consultancy Services L.L.C.")
    'tata consultancy services'
    >>> canonical_employer_name("The Goldman Sachs Group, Inc.")
    'goldman sachs'

    The function is intentionally conservative: it does not attempt fuzzy
    matching across spelling variants (``Tata`` vs ``TaTa Consulting``).
    Fuzzy clustering belongs in a derived layer with explicit linkage
    rules and a ``data_quality='computed'`` stamp; the canonical name
    is for exact-match grouping only.

    """
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    no_suffix = _SUFFIX_REGEX.sub(" ", lowered)
    no_punct = _PUNCT_REGEX.sub(" ", no_suffix)
    return _MULTISPACE_REGEX.sub(" ", no_punct).strip()
