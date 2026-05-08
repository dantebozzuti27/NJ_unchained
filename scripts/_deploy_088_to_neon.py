"""One-shot Neon production deploy for migration 088 + seed 020.

VISION_2026 Pillar 2 (civic integrity) -- Phase F-UX work items F4 + F6/F7
substrate. Ships:
  * derived.v_entity_fraud_evidence (canonical join: observation -> rendered
    plain-English + citation + severity precedent + display + NJ-relevance
    + upstream-verify URL)
  * derived.v_nj_federal_officials (curated NJ federal incumbent roster
    for the /risk overview Section 1 card grid)
  * ref.fraud_signal_evidence_url_template (per-signal_id URL template
    registry, 17 rows seeded by 020)

Idempotency contract:
  * skip if migration_id already applied with matching sha256
  * fail loudly on sha drift (sha256 mismatch -> exit 2)
  * apply + record on first run

Post-apply verification:
  * v_entity_fraud_evidence row count = fraud_signal_observation count
  * No row has {{...}} placeholder residue in rendered_explanation or
    upstream_verify_url
  * v_nj_federal_officials returns 14 rows for cycle 2024 (2 Sen + 12 Hou)
  * Per-NJ-incumbent score / signal-count inventory printed for the
    operator to review

Run: python scripts/_deploy_088_to_neon.py
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
    / "088_fraud_evidence_view_and_nj_officials.sql",
    REPO_ROOT / "db" / "seeds"
    / "020_fraud_signal_evidence_url_template.sql",
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
        # Verification 1: v_entity_fraud_evidence row count matches L1
        # ------------------------------------------------------------------
        print("\n--- verification: v_entity_fraud_evidence shape ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  (SELECT COUNT(*) FROM derived.fraud_signal_observation
                     WHERE cycle='2024')      AS l1_obs,
                  (SELECT COUNT(*) FROM derived.v_entity_fraud_evidence
                     WHERE cycle='2024')      AS view_rows
            """)
            l1, vr = cur.fetchone()
            print(f"  L1 fraud_signal_observation : {l1:,}")
            print(f"  v_entity_fraud_evidence rows: {vr:,}")
            if l1 != vr:
                print(
                    "  [warn] view row count != L1 row count "
                    "(left-join introduced row multiplication)",
                    file=sys.stderr,
                )

        # ------------------------------------------------------------------
        # Verification 2: no {{...}} placeholder residue
        # ------------------------------------------------------------------
        print("\n--- verification: token substitution completeness ---")
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
                print("  [err] rendered_explanation has placeholder residue:",
                      file=sys.stderr)
                for sig, eid, txt in residue:
                    tokens = re.findall(r"\{\{[^}]+\}\}", txt)
                    print(f"    {sig} {eid}: {tokens}", file=sys.stderr)
                return 4
            print("  [ok] no placeholder residue in rendered_explanation")

            cur.execute("""
                SELECT signal_id, entity_id, upstream_verify_url
                FROM derived.v_entity_fraud_evidence
                WHERE cycle='2024'
                  AND upstream_verify_url ~ '\\{\\{[^}]+\\}\\}'
                LIMIT 5
            """)
            residue = cur.fetchall()
            if residue:
                print("  [err] upstream_verify_url has placeholder residue:",
                      file=sys.stderr)
                for sig, eid, url in residue:
                    tokens = re.findall(r"\{\{[^}]+\}\}", url)
                    print(f"    {sig} {eid}: {tokens}", file=sys.stderr)
                return 5
            print("  [ok] no placeholder residue in upstream_verify_url")

        # ------------------------------------------------------------------
        # Verification 3: NJ-only filter row counts
        # ------------------------------------------------------------------
        print("\n--- verification: NJ-only filter coverage ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT entity_kind,
                       COUNT(*) FILTER (WHERE NOT is_nj) AS national,
                       COUNT(*) FILTER (WHERE     is_nj) AS nj
                FROM derived.v_entity_fraud_evidence
                WHERE cycle='2024'
                GROUP BY entity_kind ORDER BY entity_kind
            """)
            print(f"    {'kind':<12} {'national':>10} {'nj':>6}")
            for kind, nat, nj in cur.fetchall():
                print(f"    {kind:<12} {nat:>10,} {nj:>6,}")

        # ------------------------------------------------------------------
        # Verification 4: NJ federal officials roster
        # ------------------------------------------------------------------
        print("\n--- verification: v_nj_federal_officials roster ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT entity_id, official_name, office_label,
                       office_district, office_party, risk_score,
                       n_signals_fired
                FROM derived.v_nj_federal_officials
                WHERE cycle='2024'
            """)
            rows = cur.fetchall()
            print(f"  total NJ federal incumbents: {len(rows)} "
                  "(expected ~14: 2 Sen + 12 Hou)")
            print(f"    {'id':<12} {'office':<22} {'dist':<5} {'pty':<5} "
                  f"{'name':<35} {'score':>6} {'sigs':>4}")
            for eid, name, label, dist, pty, score, n_sigs in rows:
                marker = " *" if (score or 0) > 0 else ""
                print(
                    f"    {eid:<12} {label:<22} {(dist or ''):<5} "
                    f"{(pty or ''):<5} {(name or '')[:35]:<35} "
                    f"{float(score or 0):>6.1f} {n_sigs:>4}{marker}"
                )

        # ------------------------------------------------------------------
        # Verification 5: ref.fraud_signal_evidence_url_template coverage
        # ------------------------------------------------------------------
        print("\n--- verification: URL template coverage ---")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cfg.signal_id, eut.upstream_source, eut.button_label
                FROM derived.fraud_signal_config cfg
                LEFT JOIN ref.fraud_signal_evidence_url_template eut
                  ON eut.signal_id = cfg.signal_id
                ORDER BY cfg.signal_id
            """)
            print(f"    {'signal_id':<45} {'upstream':<18} {'label'}")
            missing: list[str] = []
            for sid, src, label in cur.fetchall():
                if src is None:
                    missing.append(sid)
                    print(f"    {sid:<45} {'<MISSING>':<18} {'-'}")
                else:
                    print(f"    {sid:<45} {src:<18} {label}")
            if missing:
                print(
                    f"\n  [err] signals without URL template: {missing}",
                    file=sys.stderr,
                )
                return 6
            print("\n  [ok] every signal has an upstream-verify URL template")

    print("\n[done] migration 088 + seed 020 applied; "
          "v_entity_fraud_evidence + v_nj_federal_officials live on Neon.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
