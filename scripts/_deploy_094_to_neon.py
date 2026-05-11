"""One-shot Neon production deploy for migration 094.

VISION_2026 Pillar 2 substrate hygiene. Ships:
  * derived.refresh_all_fraud_signal_observations -- rewritten to invoke
    ALL 18 seeded fraud signals (was 8); the 10 cross-source refreshers
    (LEIE 4 + SAM 3 + USAspending 3) are now reachable through the
    master orchestrator.

Idempotency contract identical to scripts/_deploy_088_to_neon.py:
  * skip if migration_id already applied with matching sha256
  * fail loudly on sha drift (sha256 mismatch -> exit 2)
  * apply + record on first run

Post-apply verification:
  * function body contains >= 18 SELECT derived.refresh_* invocations
  * every signal_id in fraud_signal_config has a matching invocation
  * empty-cycle smoke test: refresh against a non-existent cycle ('9999')
    returns 0 cleanly (no exception)
  * production cycle test: refresh cycle 2024 + 2026 produce >= prior
    observation counts (drift detection -- if any per-signal refresher
    regressed, the totals would drop)

Run: python scripts/_deploy_094_to_neon.py
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
    / "094_master_refresher_consolidation.sql",
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
        # Capture PRIOR per-signal observation counts so we can confirm no
        # regression post-rewrite. This is the substrate-honesty contract:
        # the rewrite must not silently delete prior observations.
        # ------------------------------------------------------------------
        print("\n--- pre-apply: per-signal observation snapshot ---")
        prior_counts: dict[tuple[str, str], int] = {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cycle, signal_id, COUNT(*)::INT
                FROM   derived.fraud_signal_observation
                GROUP BY cycle, signal_id
                ORDER BY cycle, signal_id
            """)
            for cycle, signal_id, n in cur.fetchall():
                prior_counts[(cycle, signal_id)] = n
                print(f"  {cycle} {signal_id:50s} {n:>6}")
        print(f"  TOTAL prior observations across all cycles: "
              f"{sum(prior_counts.values()):,}")

        # ------------------------------------------------------------------
        # Apply migration 094
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
        # Verification 1: function body contains >= 18 invocations
        # ------------------------------------------------------------------
        print("\n--- verification 1: function body invocation count ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT prosrc
                FROM   pg_proc p
                JOIN   pg_namespace n ON n.oid = p.pronamespace
                WHERE  n.nspname = 'derived'
                  AND  p.proname = 'refresh_all_fraud_signal_observations'
            """)
            (body,) = cur.fetchone()
        invocations = re.findall(
            r"SELECT\s+derived\.refresh_\w+\s*\(",
            body,
            flags=re.IGNORECASE,
        )
        n_inv = len(invocations)
        print(f"  refresher invocations in master body: {n_inv}")
        if n_inv < 18:
            print(
                f"  [err] expected >= 18 invocations, got {n_inv}",
                file=sys.stderr,
            )
            return 4
        print("  [ok] >= 18 invocations present")

        # ------------------------------------------------------------------
        # Verification 2: every seeded signal_id has a matching invocation
        # ------------------------------------------------------------------
        print(
            "\n--- verification 2: every seeded signal_id covered ---"
        )
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id
                FROM   derived.fraud_signal_config
                ORDER BY signal_id
            """)
            seeded_signals = [r[0] for r in cur.fetchall()]

        signal_to_refresher = {
            "treasurer_concentration":
                "derived.refresh_treasurer_concentration_observations",
            "candidate_no_pcc":
                "derived.refresh_candidate_no_pcc_observations",
            "candidate_broken_pcc":
                "derived.refresh_candidate_broken_pcc_observations",
            "candidate_multiple_pccs":
                "derived.refresh_candidate_multiple_pccs_observations",
            "committee_address_clusters":
                "derived.refresh_committee_address_clusters_observations",
            "committee_name_collisions":
                "derived.refresh_committee_name_collisions_observations",
            "candidate_namesakes":
                "derived.refresh_candidate_namesakes_observations",
            "treasurer_is_candidate":
                "derived.refresh_treasurer_is_candidate_observations",
            "entity_on_leie":
                "derived.refresh_signal_entity_on_leie",
            "entity_on_leie_strict_address":
                "derived.refresh_signal_entity_on_leie_strict_address",
            "donor_on_leie":
                "derived.refresh_signal_donor_on_leie",
            "candidate_funded_by_excluded_donors":
                "derived.refresh_signal_candidate_funded_by_excluded_donors",
            "entity_excluded_via_sam_uei":
                "derived.refresh_signal_entity_excluded_via_sam_uei",
            "donor_on_sam":
                "derived.refresh_signal_donor_on_sam",
            "candidate_funded_by_sam_excluded_donors":
                "derived.refresh_signal_candidate_funded_by_sam_excluded_donors",
            "entity_funded_and_excluded":
                "derived.refresh_signal_entity_funded_and_excluded",
            "candidate_funded_by_nj_contractor_employees":
                "derived.refresh_signal_candidate_funded_by_nj_contractor_employees",
            "donor_employed_by_nj_contractor":
                "derived.refresh_signal_donor_employed_by_nj_contractor",
        }
        missing_in_map = set(seeded_signals) - set(signal_to_refresher)
        if missing_in_map:
            print(
                f"  [err] signals seeded but not in coverage map: "
                f"{sorted(missing_in_map)}",
                file=sys.stderr,
            )
            return 5
        missing_in_body = []
        for signal_id in seeded_signals:
            refresher = signal_to_refresher[signal_id]
            if refresher not in body:
                missing_in_body.append((signal_id, refresher))
        if missing_in_body:
            print(
                "  [err] master function body missing invocations:",
                file=sys.stderr,
            )
            for sig, ref in missing_in_body:
                print(f"    {sig} -> {ref}", file=sys.stderr)
            return 6
        print(
            f"  [ok] all {len(seeded_signals)} seeded signals have "
            f"matching invocations in master body"
        )

        # ------------------------------------------------------------------
        # Verification 3: empty-cycle smoke test
        # ------------------------------------------------------------------
        print("\n--- verification 3: empty-cycle smoke test ---")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT derived.refresh_all_fraud_signal_observations(%s)",
                ('9999',),
            )
            (n_empty,) = cur.fetchone()
        if n_empty != 0:
            print(
                f"  [err] cycle '9999' (no data) returned {n_empty} "
                "observations (expected 0)",
                file=sys.stderr,
            )
            return 7
        print("  [ok] cycle '9999' returned 0 observations cleanly")

        # ------------------------------------------------------------------
        # Verification 4: per-cycle re-refresh against loaded substrate
        # confirms no regression and surfaces any newly-firing signals
        # ------------------------------------------------------------------
        print(
            "\n--- verification 4: re-refresh cycle 2024 + 2026 ---"
        )
        for cycle in ('2024', '2026'):
            with conn.cursor() as cur:
                t0 = time.monotonic()
                cur.execute(
                    "SELECT "
                    "derived.refresh_all_fraud_signal_observations(%s)",
                    (cycle,),
                )
                (n_new,) = cur.fetchone()
                duration_ms = int((time.monotonic() - t0) * 1000)
            conn.commit()
            print(
                f"  cycle {cycle}: {n_new:,} observations "
                f"({duration_ms} ms)"
            )

        # ------------------------------------------------------------------
        # Verification 5: post-refresh per-signal counts ≥ pre-refresh
        # ------------------------------------------------------------------
        print(
            "\n--- verification 5: per-signal regression check ---"
        )
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cycle, signal_id, COUNT(*)::INT
                FROM   derived.fraud_signal_observation
                GROUP BY cycle, signal_id
                ORDER BY cycle, signal_id
            """)
            new_counts = {
                (c, s): n for c, s, n in cur.fetchall()
            }
        regressions = []
        for key, prior_n in prior_counts.items():
            new_n = new_counts.get(key, 0)
            if new_n < prior_n:
                regressions.append((key[0], key[1], prior_n, new_n))
        if regressions:
            print(
                "  [err] per-signal regressions detected:",
                file=sys.stderr,
            )
            for cycle, sig, p, n in regressions:
                print(
                    f"    cycle {cycle} {sig}: was {p}, now {n}",
                    file=sys.stderr,
                )
            return 8
        print("  [ok] no per-signal regressions detected")

        # Newly-firing signals (delta)
        new_signals = set(new_counts) - set(prior_counts)
        if new_signals:
            print(
                f"\n  newly-firing (signal_id, cycle) combinations: "
                f"{len(new_signals)}"
            )
            for cycle, sig in sorted(new_signals):
                print(
                    f"    cycle {cycle} {sig}: {new_counts[(cycle, sig)]}"
                )

        # Total inventory
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cycle, COUNT(*)::INT
                FROM   derived.fraud_signal_observation
                GROUP BY cycle
                ORDER BY cycle
            """)
            print("\n  total observations per cycle (post-refresh):")
            for cycle, n in cur.fetchall():
                print(f"    cycle {cycle}: {n:,}")

    print(
        "\n[done] migration 094 applied; master refresher now invokes "
        "all 18 seeded fraud signals."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
