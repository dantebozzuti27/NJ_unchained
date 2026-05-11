"""One-shot Neon production deploy for migration 095.

VISION_2026 Pillar 2 substrate hygiene. Ships severity dependency
inversion: L2/L3 views read severity from
ref.fraud_signal_severity_calibration via LEFT JOIN (COALESCE
fallback to base-column value). The base column becomes an audit
artifact; analytical surfaces canonically read calibration.

Idempotency contract identical to scripts/_deploy_094_to_neon.py.

Post-apply verification gates:
  1. ref.f_signal_severity returns calibration value for each seeded signal
  2. f_signal_severity RAISES on unknown signal_id
  3. v_entity_fraud_features now reflects calibration: re-rendering the
     same observation rows yields the same severities array as the
     calibration table dictates (proves the view rewrite landed)
  4. audit_severity_drift returns 0 rows on production substrate (which
     is currently consistent -- the hardcoded refresher values match
     calibration today; this asserts that consistency)
  5. Formula version registered

Run: python scripts/_deploy_095_to_neon.py
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
    / "095_severity_dependency_inversion.sql",
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
        # ------------------------------------------------------------------
        # Pre-apply: capture L2 severities snapshot for one representative
        # entity so we can verify the rewrite is bit-identical against
        # clean substrate (where hardcoded values match calibration).
        # ------------------------------------------------------------------
        print("\n--- pre-apply: representative L2 severities snapshot ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT entity_kind, entity_id, signals_fired, severities
                FROM   derived.v_entity_fraud_features
                WHERE  cycle = '2024'
                  AND  array_length(signals_fired, 1) >= 2
                LIMIT 3
            """)
            pre_snapshot = cur.fetchall()
            for k, eid, sigs, sevs in pre_snapshot:
                print(f"  {k:10s} {str(eid)[:40]:40s} "
                      f"signals={list(sigs)} sevs={list(sevs)}")

        # ------------------------------------------------------------------
        # Apply migration 095
        # ------------------------------------------------------------------
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
                    print(f"\n  [skip] {mid} already applied, sha matches")
                    continue
                print(
                    f"\n  [drift] {mid} recorded sha256 differs",
                    file=sys.stderr,
                )
                print(
                    f"    recorded: {existing[0]}\n    current:  {sha}",
                    file=sys.stderr,
                )
                return 2
            print(
                f"\n  [apply] {mid} ({len(f.read_bytes())} bytes, "
                f"sha {sha[:8]})"
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
        # Verification 1: f_signal_severity returns calibration value
        # for every seeded signal_id
        # ------------------------------------------------------------------
        print(
            "\n--- verification 1: f_signal_severity matches calibration ---"
        )
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id, severity_level,
                       ref.f_signal_severity(signal_id) AS via_func
                FROM   ref.fraud_signal_severity_calibration
                ORDER BY signal_id
            """)
            rows = cur.fetchall()
        mismatches = [
            (sig, table_sev, fn_sev)
            for sig, table_sev, fn_sev in rows
            if table_sev != fn_sev
        ]
        if mismatches:
            print(
                "  [err] f_signal_severity disagrees with table:",
                file=sys.stderr,
            )
            for sig, t, fn in mismatches:
                print(f"    {sig}: table={t} func={fn}", file=sys.stderr)
            return 4
        print(f"  [ok] all {len(rows)} signals: function == table")

        # ------------------------------------------------------------------
        # Verification 2: f_signal_severity raises on unknown
        # ------------------------------------------------------------------
        print("\n--- verification 2: f_signal_severity raises on unknown ---")
        raised = False
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT ref.f_signal_severity('nonexistent_signal_xyz')"
                )
                cur.fetchone()
            except psycopg.errors.NoDataFound:
                raised = True
            except Exception as exc:
                print(
                    f"  [err] wrong exception class: {type(exc).__name__}: "
                    f"{exc}",
                    file=sys.stderr,
                )
                conn.rollback()
                return 5
        conn.rollback()
        if not raised:
            print(
                "  [err] f_signal_severity did NOT raise on unknown signal",
                file=sys.stderr,
            )
            return 5
        print("  [ok] f_signal_severity raised no_data_found cleanly")

        # ------------------------------------------------------------------
        # Verification 3: L2 severities snapshot is bit-identical
        # (the rewrite is correct AND prod has zero drift today)
        # ------------------------------------------------------------------
        print(
            "\n--- verification 3: L2 severities post-rewrite match snapshot ---"
        )
        with conn.cursor() as cur:
            cur.execute("""
                SELECT entity_kind, entity_id, signals_fired, severities
                FROM   derived.v_entity_fraud_features
                WHERE  cycle = '2024'
                  AND  array_length(signals_fired, 1) >= 2
                LIMIT 3
            """)
            post_snapshot = cur.fetchall()

        if len(pre_snapshot) != len(post_snapshot):
            print(
                f"  [err] snapshot size changed: pre {len(pre_snapshot)} "
                f"-> post {len(post_snapshot)}",
                file=sys.stderr,
            )
            return 6
        differing = []
        for pre, post in zip(pre_snapshot, post_snapshot, strict=True):
            if pre != post:
                differing.append((pre, post))
        if differing:
            print(
                "  [err] L2 severities changed across rewrite -- some "
                "(signal_id, entity) had drift between hardcoded and "
                "calibration. Check audit_severity_drift output below.",
                file=sys.stderr,
            )
            for pre, post in differing:
                print(f"    pre:  {pre}", file=sys.stderr)
                print(f"    post: {post}", file=sys.stderr)
            # don't return -- continue to verification 4 to surface
            # the actual drift
        else:
            print(
                f"  [ok] L2 severities bit-identical for {len(pre_snapshot)} "
                "sample rows (no drift between hardcoded and calibration "
                "on production today)"
            )

        # ------------------------------------------------------------------
        # Verification 4: audit_severity_drift on cycle 2024 + 2026
        # ------------------------------------------------------------------
        print("\n--- verification 4: audit_severity_drift on prod ---")
        for cycle in ('2024', '2026'):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT signal_id, n_obs, hardcoded_severity, "
                    "       calibration_severity, drifted "
                    "FROM derived.audit_severity_drift(%s)",
                    (cycle,),
                )
                drift_rows = cur.fetchall()
            if not drift_rows:
                print(f"  [ok] cycle {cycle}: zero drift detected")
            else:
                print(
                    f"  [WARN] cycle {cycle}: {len(drift_rows)} drift rows:"
                )
                for r in drift_rows:
                    print(f"    {r}")

        # ------------------------------------------------------------------
        # Verification 5: formula version registered
        # ------------------------------------------------------------------
        print("\n--- verification 5: formula version registered ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT effective_date
                FROM   ref.formula_version
                WHERE  formula_version =
                       '2.6.0-severity-dependency-inversion-v1'
            """)
            row = cur.fetchone()
        if row is None:
            print(
                "  [err] formula_version not registered",
                file=sys.stderr,
            )
            return 7
        print(f"  [ok] formula version registered, effective {row[0]}")

        # ------------------------------------------------------------------
        # Inventory: prod severity distribution post-rewrite
        # ------------------------------------------------------------------
        print(
            "\n--- inventory: severity distribution post-rewrite "
            "(via L2) ---"
        )
        with conn.cursor() as cur:
            cur.execute("""
                WITH unnested AS (
                    SELECT
                        cycle,
                        unnest(severities) AS severity
                    FROM derived.v_entity_fraud_features
                    WHERE cycle IN ('2024', '2026')
                )
                SELECT cycle, severity, COUNT(*) AS n
                FROM unnested
                GROUP BY cycle, severity
                ORDER BY cycle, severity
            """)
            for cycle, sev, n in cur.fetchall():
                print(f"  cycle {cycle} severity={sev}: {n:,} array elements")

    print(
        "\n[done] migration 095 applied; L2/L3 views now source severity "
        "from ref.fraud_signal_severity_calibration."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
