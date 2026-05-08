"""One-shot Neon production deploy for migration 089 (token substitution fix).

Closes the gap discovered during the 088 deploy: rendered_explanation in
derived.v_entity_fraud_evidence leaked literal "{{peer_bucket}}" and
"{{entity_kind}}" placeholder tokens because the chained REPLACE in mig 088
only handled four of the six tokens that ref.fraud_signal_human_explanation
actually emits.

Affected production rows (cycle 2024):
  * 1,072 treasurer_concentration observations leaking {{peer_bucket}}
  * 530 committee_address_clusters observations leaking {{peer_bucket}}
    + {{entity_kind}}
  * Total: 1,602 of 6,129 v_entity_fraud_evidence rows had visible
    placeholder residue in the UI render path

Idempotency contract identical to scripts/_deploy_088_to_neon.py.

Post-apply verification:
  * Re-runs the production placeholder-residue probe and confirms
    EXACTLY zero rows have {{...}} residue in rendered_explanation OR
    upstream_verify_url.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent

FILES_TO_APPLY: list[Path] = [
    REPO_ROOT / "db" / "migrations"
    / "089_fraud_evidence_view_token_substitution_complete.sql",
]


def sha256_text(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migration_id(path: Path) -> str:
    return path.stem


def main() -> int:
    dsn = os.environ.get("NEON_DATABASE_URL") or os.environ.get("PG_DSN")
    if not dsn:
        print("ERR: NEON_DATABASE_URL / PG_DSN not set", file=sys.stderr)
        return 1

    for f in FILES_TO_APPLY:
        if not f.exists():
            print(f"ERR: missing file {f}", file=sys.stderr)
            return 1

    print(f"Connecting to {dsn[: dsn.find('@')]}@... (Neon)")
    with psycopg.connect(dsn) as conn:
        for f in FILES_TO_APPLY:
            mid = migration_id(f)
            sha = sha256_text(f)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sha256 FROM governance.schema_migrations "
                    "WHERE migration_id = %s",
                    (mid,),
                )
                existing = cur.fetchone()
            if existing is not None:
                if existing[0] == sha:
                    print(f"  [skip] {mid} already applied, sha matches")
                    continue
                print(
                    f"  [drift] {mid} recorded sha256 differs",
                    file=sys.stderr,
                )
                print(
                    f"    recorded: {existing[0]}\n    current:  {sha}",
                    file=sys.stderr,
                )
                return 2
            print(
                f"  [apply] {mid} ({len(f.read_bytes())} bytes, sha {sha[:8]})"
            )
            sql = f.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                t0 = time.monotonic()
                try:
                    cur.execute(sql)
                except Exception as exc:
                    print(f"  [err]   {mid}: {exc}", file=sys.stderr)
                    conn.rollback()
                    return 3
                duration_ms = int((time.monotonic() - t0) * 1000)
                cur.execute(
                    "INSERT INTO governance.schema_migrations "
                    "(migration_id, sha256, applied_at, duration_ms) "
                    "VALUES (%s, %s, now(), %s)",
                    (mid, sha, duration_ms),
                )
            conn.commit()
            print(f"  [ok]    {mid} ({duration_ms} ms)")

        # ------------------------------------------------------------------
        # Verification: zero placeholder residue in production rows
        # ------------------------------------------------------------------
        print("\n--- verification: COMPLETE token substitution ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id, entity_id, rendered_explanation
                FROM derived.v_entity_fraud_evidence
                WHERE cycle='2024'
                  AND rendered_explanation ~ '\\{\\{[^}]+\\}\\}'
                LIMIT 5
            """)
            residue = cur.fetchall()
            if residue:
                print("  [err] STILL has placeholder residue:",
                      file=sys.stderr)
                for sig, eid, txt in residue:
                    tokens = re.findall(r"\{\{[^}]+\}\}", txt)
                    print(f"    {sig} {eid}: {tokens}", file=sys.stderr)
                return 4
            print(
                "  [ok] zero rendered_explanation rows have placeholder residue"
            )

            cur.execute("""
                SELECT signal_id, entity_id, upstream_verify_url
                FROM derived.v_entity_fraud_evidence
                WHERE cycle='2024'
                  AND upstream_verify_url ~ '\\{\\{[^}]+\\}\\}'
                LIMIT 5
            """)
            residue = cur.fetchall()
            if residue:
                print("  [err] upstream_verify_url residue:",
                      file=sys.stderr)
                for sig, eid, url in residue:
                    tokens = re.findall(r"\{\{[^}]+\}\}", url)
                    print(f"    {sig} {eid}: {tokens}", file=sys.stderr)
                return 5
            print("  [ok] zero upstream_verify_url rows have placeholder residue")

            # Pin sample renders for the formerly-broken signals.
            for signal in ("treasurer_concentration",
                           "committee_address_clusters"):
                cur.execute("""
                    SELECT entity_id, rendered_explanation
                    FROM derived.v_entity_fraud_evidence
                    WHERE cycle='2024' AND signal_id=%s
                    ORDER BY peer_percentile DESC NULLS LAST
                    LIMIT 1
                """, (signal,))
                row = cur.fetchone()
                if row is not None:
                    eid, txt = row
                    print(f"\n  audit (formerly-broken signal): {signal}")
                    print(f"    entity_id            = {eid}")
                    print(f"    rendered_explanation = {txt[:200]}...")

    print("\n[done] migration 089 applied; rendered_explanation token "
          "substitution complete on Neon.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
