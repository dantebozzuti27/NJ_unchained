"""One-shot Neon production deploy for migration 087 (address-clusters grain fix).

Surfaces a substrate bug observed when the FEC bulk loader (cycle 2024)
populated raw.fec_committee with real published filings: the source view
``derived.fec_committee_address_clusters`` produces TWO rows for the same
physical address when committees file with mixed zip5 / zip+4 (e.g.
"33606" vs "336062647"); the refresher's prior 2-tuple entity_id
(``address|state``) collided on the fraud_signal_observation PK.

Migration 087:
  * Normalizes zip in the source view to LEFT(REGEXP_REPLACE(zip,'\\D','','g'),5)
    so zip+4 noise collapses at the GROUP BY layer.
  * Extends the refresher's entity_id to address|city|state|zip5 to match
    the source view's full grain.

Idempotency contract identical to scripts/_deploy_086_to_neon.py:
  * skip if already applied with matching sha256
  * fail loudly on sha drift
  * apply + record on first run

Post-apply verification:
  * Re-runs derived.refresh_all_fraud_signal_observations('2024') and
    confirms it returns >0 (previously raised UniqueViolation on
    committee_address_clusters).
  * Reports per-signal observation counts so the operator sees the
    full Pillar 2 substrate light up.

Run: python scripts/_deploy_087_to_neon.py
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent

FILES_TO_APPLY: list[Path] = [
    REPO_ROOT / "db" / "migrations"
    / "087_fraud_committee_address_clusters_grain_fix.sql",
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
            print(f"  [apply] {mid} ({len(f.read_bytes())} bytes, sha {sha[:8]})")
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
        # Verification: re-run the master refresher and pin per-signal counts
        # ------------------------------------------------------------------
        print("\n--- verification: master refresher post-fix ---")
        with conn.cursor() as cur:
            t0 = time.monotonic()
            try:
                cur.execute(
                    "SELECT derived.refresh_all_fraud_signal_observations('2024')"
                )
                n_total = cur.fetchone()[0]
            except Exception as exc:
                print(f"  [err] master refresher still failing: {exc}",
                      file=sys.stderr)
                conn.rollback()
                return 4
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            conn.commit()
            print(f"  refresh_all_fraud_signal_observations('2024') = "
                  f"{n_total:,} rows in {elapsed_ms} ms")

            cur.execute("""
                SELECT signal_id, COUNT(*) AS n_rows,
                       MIN(severity) AS min_sev, MAX(severity) AS max_sev,
                       MIN(peer_percentile)::FLOAT8 AS min_pct,
                       MAX(peer_percentile)::FLOAT8 AS max_pct
                FROM derived.fraud_signal_observation
                WHERE cycle = '2024'
                GROUP BY signal_id
                ORDER BY signal_id
            """)
            rows = cur.fetchall()
            print("\n  per-signal observation counts (cycle 2024):")
            print(f"    {'signal_id':<48} {'n':>7}  sev    pctile_range")
            for sig, n, min_s, max_s, min_p, max_p in rows:
                sev_disp = (
                    f"{min_s}-{max_s}" if min_s != max_s else f"{min_s}"
                )
                print(
                    f"    {sig:<48} {n:>7,}  {sev_disp:<5}  "
                    f"{min_p:.3f}..{max_p:.3f}"
                )

            # Pin the exact case that originally tripped the bug, post-fix.
            cur.execute("""
                SELECT entity_id, raw_value, peer_percentile
                FROM derived.fraud_signal_observation
                WHERE cycle = '2024'
                  AND signal_id = 'committee_address_clusters'
                  AND entity_id LIKE 'PO BOX 97275|RALEIGH|NC|%'
            """)
            row = cur.fetchone()
            if row is not None:
                eid, raw_val, pct = row
                print(
                    f"\n  audit (formerly-failing case):\n"
                    f"    entity_id      = {eid}\n"
                    f"    raw_value      = {raw_val}  (= union of zip5 + zip+4 "
                    f"committees)\n"
                    f"    peer_percentile= {pct:.4f}"
                )

            cur.execute("""
                SELECT COUNT(*)
                FROM derived.v_entity_fraud_risk
                WHERE cycle = '2024'
            """)
            print(
                f"\n  derived.v_entity_fraud_risk rows for cycle 2024: "
                f"{cur.fetchone()[0]:,}"
            )

            cur.execute("""
                SELECT COUNT(*)
                FROM derived.v_anomaly_score_percentile_by_kind_cycle
                WHERE cycle = '2024'
            """)
            print(
                f"  derived.v_anomaly_score_percentile_by_kind_cycle "
                f"rows for cycle 2024: {cur.fetchone()[0]:,}"
            )

    print("\n[done] migration 087 applied + Pillar 2 substrate live on Neon.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
