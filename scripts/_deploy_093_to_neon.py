"""One-shot Neon production deploy for migration 093 + seed 022.

VISION_2026 Pillar 2 (civic integrity) -- Phase F8.5 stub: NJ state-candidate
substrate. Ships:
  * ref.nj_state_candidate          (manually-curated reference table)
  * derived.v_nj_state_candidates   (UI-shape view with ingest-pending flag)
  * 10 publicly-announced 2025 NJ Gubernatorial primary candidates seeded
    with HTTPS citation URLs (6 D + 4 R)

Idempotency contract identical to scripts/_deploy_088_to_neon.py:
  * skip if migration_id already applied with matching sha256
  * fail loudly on sha drift (sha256 mismatch -> exit 2)
  * apply + record on first run

Post-apply verification:
  * Schema constraints actually attached (party / office / id-format CHECKs)
  * 10 rows seeded with all 6 D + 4 R candidates present
  * Every row has source_url LIKE 'https://%' (substrate-honesty)
  * Every row has campaign_finance_ingest_pending = TRUE (no ELEC ingest yet)
  * No row has primary_winner / general_winner set (no certified-results)
  * View row count matches base-table row count (no row multiplication)
  * Prints the per-party candidate inventory for operator review

Run: python scripts/_deploy_093_to_neon.py
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
    / "093_nj_state_candidate_substrate.sql",
    REPO_ROOT / "db" / "seeds"
    / "022_nj_state_candidate_2025_gubernatorial.sql",
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
                f"  [apply] {mid} ({len(f.read_bytes())} bytes, "
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
        # Verification 1: schema constraints attached
        # ------------------------------------------------------------------
        print("\n--- verification 1: schema constraints attached ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'ref.nj_state_candidate'::regclass
                  AND contype = 'c'
                ORDER BY conname
            """)
            constraints = [r[0] for r in cur.fetchall()]
            expected_checks = {
                'nj_state_candidate_party_chk',
                'nj_state_candidate_office_chk',
                'nj_state_candidate_election_year_chk',
                'nj_state_candidate_id_format_chk',
                'nj_state_candidate_source_url_chk',
                'nj_state_candidate_announcement_url_chk',
                'nj_state_candidate_primary_result_url_chk',
                'nj_state_candidate_general_result_url_chk',
                'nj_state_candidate_primary_winner_requires_url_chk',
                'nj_state_candidate_general_winner_requires_url_chk',
                'nj_state_candidate_announced_consistency_chk',
            }
            present = set(constraints)
            missing = expected_checks - present
            if missing:
                print(
                    f"  [err] missing CHECK constraints: {sorted(missing)}",
                    file=sys.stderr,
                )
                return 4
            print(
                f"  [ok] all {len(expected_checks)} CHECK constraints "
                f"present on ref.nj_state_candidate"
            )

        # ------------------------------------------------------------------
        # Verification 2: 10 rows seeded, 6 D + 4 R, all governor 2025
        # ------------------------------------------------------------------
        print("\n--- verification 2: 2025 gubernatorial seed inventory ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT party, COUNT(*)
                FROM ref.nj_state_candidate
                WHERE office = 'governor'
                  AND election_year = 2025
                GROUP BY party
                ORDER BY party
            """)
            party_counts = dict(cur.fetchall())
            n_dem = party_counts.get('DEM', 0)
            n_rep = party_counts.get('REP', 0)
            n_total = n_dem + n_rep
            print(f"  DEM   : {n_dem}")
            print(f"  REP   : {n_rep}")
            print(f"  TOTAL : {n_total}")
            if n_dem != 6:
                print(
                    f"  [err] expected 6 Dem primary candidates, got {n_dem}",
                    file=sys.stderr,
                )
                return 5
            if n_rep != 4:
                print(
                    f"  [err] expected 4 GOP primary candidates, got {n_rep}",
                    file=sys.stderr,
                )
                return 6
            print("  [ok] 6 DEM + 4 REP candidates seeded")

        # ------------------------------------------------------------------
        # Verification 3: every row has HTTPS source_url
        # ------------------------------------------------------------------
        print("\n--- verification 3: substrate-honesty (HTTPS source_url) ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT candidate_id, source_url
                FROM ref.nj_state_candidate
                WHERE source_url NOT LIKE 'https://%'
                   OR length(source_url) < 15
            """)
            bad = cur.fetchall()
            if bad:
                print(
                    f"  [err] {len(bad)} row(s) lack HTTPS source_url:",
                    file=sys.stderr,
                )
                for cid, url in bad:
                    print(f"    {cid}: {url!r}", file=sys.stderr)
                return 7
            print("  [ok] every row has HTTPS source_url >= 15 chars")

        # ------------------------------------------------------------------
        # Verification 4: campaign_finance_ingest_pending = TRUE for all
        # ------------------------------------------------------------------
        print(
            "\n--- verification 4: campaign-finance ingest pending status ---"
        )
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (
                        WHERE campaign_finance_ingest_pending = TRUE
                    ) AS pending,
                    COUNT(*)                                  AS total
                FROM derived.v_nj_state_candidates
            """)
            pending, total = cur.fetchone()
            print(f"  campaign_finance_ingest_pending = TRUE : {pending}")
            print(f"  total rows in view                     : {total}")
            if pending != total:
                print(
                    "  [err] expected ALL rows to be ingest-pending "
                    "(NJ ELEC ingester not yet shipped)",
                    file=sys.stderr,
                )
                return 8
            print("  [ok] every row carries the ingest-pending badge")

        # ------------------------------------------------------------------
        # Verification 5: no certified-results claims
        # ------------------------------------------------------------------
        print(
            "\n--- verification 5: no certified-results claims yet ---"
        )
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)
                FROM ref.nj_state_candidate
                WHERE primary_winner IS NOT NULL
                   OR general_winner IS NOT NULL
            """)
            (n_results_claimed,) = cur.fetchone()
            if n_results_claimed != 0:
                print(
                    f"  [err] {n_results_claimed} row(s) claim results "
                    "without verified ingest",
                    file=sys.stderr,
                )
                return 9
            print(
                "  [ok] zero rows claim certified primary/general results"
            )

        # ------------------------------------------------------------------
        # Verification 6: view row count == base table row count
        # ------------------------------------------------------------------
        print(
            "\n--- verification 6: view shape (no row multiplication) ---"
        )
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM ref.nj_state_candidate)
                        AS base_rows,
                    (SELECT COUNT(*) FROM derived.v_nj_state_candidates)
                        AS view_rows
            """)
            base, view = cur.fetchone()
            print(f"  ref.nj_state_candidate          : {base}")
            print(f"  derived.v_nj_state_candidates    : {view}")
            if base != view:
                print(
                    "  [err] view rows != base rows -- ORDER BY introduced "
                    "row multiplication?",
                    file=sys.stderr,
                )
                return 10
            print("  [ok] view rows == base rows")

        # ------------------------------------------------------------------
        # Inventory audit: print every seeded candidate
        # ------------------------------------------------------------------
        print("\n--- inventory audit (operator review) ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    party,
                    full_name,
                    prior_office,
                    announcement_date,
                    source_doc_date
                FROM derived.v_nj_state_candidates
                WHERE office = 'governor'
                  AND election_year = 2025
                ORDER BY party, full_name
            """)
            for party, name, prior, ann_date, doc_date in cur.fetchall():
                ann_str = (
                    ann_date.isoformat() if ann_date is not None else 'n/a'
                )
                doc_str = (
                    doc_date.isoformat() if doc_date is not None else 'n/a'
                )
                print(
                    f"  [{party}] {name:25s}  announced {ann_str}  "
                    f"(verified {doc_str})"
                )
                print(f"         prior office: {prior}")

    print(
        "\n[done] migration 093 + seed 022 applied; "
        "10 publicly-announced 2025 NJ Gubernatorial candidates "
        "seeded on Neon."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
