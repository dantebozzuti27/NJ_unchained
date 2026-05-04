"""Ingestion layer.

Each module in this package is a stateless, idempotent loader for a single
external data source. The contract:

1. Loaders read from local files in ``data/manual/<source>/`` (operator-
   downloaded) or stream from the source URL via ``httpx``. They never
   modify their input.
2. Loaders write to ``raw.*`` tables only, with full provenance columns
   populated (``source_filename``, ``source_sha256``, ``source_schema_version``).
3. Normalization is a follow-on responsibility of the ``derived.*`` layer.
   Loaders never compute aggregates; they pass raw rows through.
4. Loaders raise ``ingestion._base.IngestError`` on any condition that
   would corrupt the audit trail (schema drift unrecognized by the
   detector, ratio-sum invariants violated, primary-key collisions).

Shared utilities live in :mod:`ingestion._base`.
"""

from ingestion._base import (
    IngestError,
    SchemaSignature,
    canonical_employer_name,
    canonicalize_column,
    detect_schema_version,
    sha256_file,
)

__all__ = [
    "IngestError",
    "SchemaSignature",
    "canonical_employer_name",
    "canonicalize_column",
    "detect_schema_version",
    "sha256_file",
]
