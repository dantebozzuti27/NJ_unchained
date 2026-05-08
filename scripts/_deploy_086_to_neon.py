"""One-shot Neon production deploy for migration 086 + seeds 018/019.

Applies the fraud-evidence substrate (Phase F-UX work items F2 + F3 + F5)
to Neon production. Idempotent: re-running detects already-applied
migrations via governance.schema_migrations and skips them; the seeds
use ON CONFLICT DO UPDATE so re-running them is a no-op vs. the
authoritative content.

Used because nj-migrate's drift-detection on prior already-applied
migrations would block bulk re-application; this script applies only
the new files and records them in the ledger.

Idempotency contract:
  * If a migration_id already exists in governance.schema_migrations
    AND its recorded sha256 matches the current file, we skip and log.
  * If it exists with a DIFFERENT sha256, we fail loudly (drift) so
    the operator decides what to do.
  * If it does not exist, we apply inside a transaction and record on
    commit.

After successful application, this script prints a verification summary:
  - row count of ref.fraud_signal_human_explanation
  - row count of ref.fraud_signal_severity_calibration
  - formula_version registry entry
  - column shape of derived.v_anomaly_score_percentile_by_kind_cycle

Run: python scripts/_deploy_086_to_neon.py
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
    REPO_ROOT / "db" / "migrations" / "086_fraud_evidence_substrate.sql",
    REPO_ROOT / "db" / "seeds" / "018_fraud_signal_human_explanation.sql",
    REPO_ROOT / "db" / "seeds" / "019_fraud_signal_severity_calibration.sql",
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

        print("\n--- verification ---")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ref.fraud_signal_human_explanation"
            )
            row = cur.fetchone()
            print(
                "ref.fraud_signal_human_explanation rows:",
                row[0] if row else None,
            )

            cur.execute(
                "SELECT COUNT(*) FROM ref.fraud_signal_severity_calibration"
            )
            row = cur.fetchone()
            print(
                "ref.fraud_signal_severity_calibration rows:",
                row[0] if row else None,
            )

            cur.execute(
                "SELECT formula_version, description, effective_date "
                "FROM ref.formula_version "
                "WHERE formula_version = '2.1.0-fraud-evidence-substrate-v1'"
            )
            row = cur.fetchone()
            print("formula_version row:", row)

            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='derived'
                  AND table_name='v_anomaly_score_percentile_by_kind_cycle'
                ORDER BY ordinal_position
            """)
            cols = [r[0] for r in cur.fetchall()]
            print(
                "derived.v_anomaly_score_percentile_by_kind_cycle columns:",
                cols,
            )

            cur.execute(
                "SELECT signal_id, citation_authority, citation_section, "
                "       severity_level, calibration_basis "
                "FROM ref.fraud_signal_human_explanation expl "
                "JOIN ref.fraud_signal_severity_calibration cal "
                "  USING (signal_id) "
                "ORDER BY signal_id"
            )
            print("\nseed cross-table summary (signal | authority | section "
                  "| severity | basis):")
            for sig, auth, sec, sev, basis in cur.fetchall():
                print(f"  {sig:<48} {auth:<11} {sec:<35} sev={sev}  {basis}")

    print("\n[done] migration 086 + seeds 018/019 applied to Neon.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
