#!/usr/bin/env bash
# =============================================================================
# Deploy VISION_2026 Pillar 2 (civic integrity) substrate to Neon production.
#
# Pillar 2 is structured anomaly detection on the federal-elections side of
# the house. It is logically independent of Pillar 1 (housing affordability,
# deployed by scripts/deploy_neon_substrate.sh): the migrations train shares
# both pillars but the bulk loads, refreshers, and freshness signals are
# scoped per-pillar so an operator can refresh one without touching the
# other (e.g. when FEC publishes a fresh bi-weekly cn/cm zip).
#
# What this does, in order:
#   1. Validates NEON_DATABASE_URL is set and reachable.
#   2. Applies every pending migration in db/migrations/ (idempotent --
#      same train as the Pillar 1 deploy; safe to re-run).
#   3. Applies every pending seed in db/seeds/ (idempotent).
#   4. Bulk-loads FEC cn{yy} (Candidate Master) + cm{yy} (Committee Master)
#      for the requested cycle into raw.fec_candidate / raw.fec_committee.
#      Default cycle is 2024 (current presidential / federal cycle as of
#      May 2026); pass FEC_CYCLE=2026 etc. to retarget.
#      indiv{yy} (Individual Contributions, ~58M rows / 4.2 GB compressed)
#      is INTENTIONALLY EXCLUDED to stay inside the Neon free-tier 10 GB
#      quota. The contribution-graph signals (donor_*) become available
#      once a paid Neon tier is provisioned and indiv is loaded; until
#      then the eight structural signals below are the active substrate.
#   5. Materializes the eight structural fraud signals via
#      derived.refresh_all_fraud_signal_observations(cycle):
#        - candidate_no_pcc                (severity 1, empirical_pctile)
#        - candidate_broken_pcc            (severity 2, empirical_pctile)
#        - candidate_multiple_pccs         (severity 2, empirical_pctile)
#        - candidate_namesakes             (severity 3, empirical_pctile)
#        - committee_address_clusters      (severity 4, fec_mur)
#        - committee_name_collisions       (severity 3, fec_advisory)
#        - treasurer_concentration         (severity 3, empirical_pctile)
#        - treasurer_is_candidate          (severity 1, fec_advisory)
#      All eight read from raw.fec_candidate + raw.fec_committee; none
#      depend on indiv. Each one's severity and federal-authority citation
#      are pinned in ref.fraud_signal_severity_calibration and
#      ref.fraud_signal_human_explanation respectively (mig 086 + seeds
#      018 / 019).
#   6. Backfills the raw.fec freshness signal via
#      derived.f_backfill_freshness_from_ingested_at(). This is what
#      flips raw.fec from 'never_materialized' to 'fresh' on the
#      v_data_freshness_summary view.
#   7. Verification block:
#        - row counts for raw.fec_candidate / raw.fec_committee
#        - per-signal observation counts in derived.fraud_signal_observation
#        - row count of derived.v_entity_fraud_risk
#        - row count + sample percentiles in
#          derived.v_anomaly_score_percentile_by_kind_cycle
#        - top-5 highest-anomaly entities (cycle-scoped)
#        - raw.fec entry in derived.v_data_freshness_summary
#
# Idempotency contract:
#   - Migrations / seeds: gated by sha256-checked governance.schema_migrations
#     rows; re-run is a no-op when nothing has changed.
#   - FEC bulk load: nj-ingest-fec issues HEAD with cached file size, so
#     unchanged remotes are not re-downloaded; the COPY is wrapped in
#     DELETE WHERE cycle = <cycle> + INSERT, so re-running for the same
#     cycle is a clean replace.
#   - Refresher: each per-signal function does DELETE WHERE signal_id=...
#     AND cycle=... before INSERT, so re-running is a clean replace.
#   - Freshness backfill: emits a new 'materialized' health signal only
#     when no prior signal at-or-after MAX(ingested_at) is on file.
#
# Pre-requisites:
#   - NEON_DATABASE_URL exported in this shell (Vercel dashboard ->
#     nj-unchained -> Settings -> Environment Variables -> reveal).
#   - .venv set up: `python -m venv .venv && .venv/bin/pip install -e .`
#   - Public internet (FEC bulk endpoint is fec.gov / cloudfront).
#
# Runtime: ~10-30 seconds against a typical Neon endpoint. The cn + cm
# zips are <2 MB combined; the COPY in psycopg streams in well under
# a second. The refresh runs over ~30K committees + ~10K candidates and
# completes in ~1-2 seconds.
#
# Usage:
#   export NEON_DATABASE_URL='postgresql://user:pass@ep-xxx-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require'
#   bash scripts/deploy_neon_pillar2_substrate.sh           # default cycle 2024
#   FEC_CYCLE=2026 bash scripts/deploy_neon_pillar2_substrate.sh
# =============================================================================

set -euo pipefail

# ----------------------------------------------------------------------------
# 0. Pre-flight
# ----------------------------------------------------------------------------

if [[ -z "${NEON_DATABASE_URL:-}" ]]; then
    echo "ERROR: NEON_DATABASE_URL is not set." >&2
    echo "       Pull it from Vercel: dashboard -> nj-unchained -> Settings" >&2
    echo "       -> Environment Variables -> NEON_DATABASE_URL -> reveal." >&2
    echo "       Then: export NEON_DATABASE_URL='postgresql://...'" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -x .venv/bin/nj-migrate ]]; then
    echo "ERROR: .venv/bin/nj-migrate not found. Run:" >&2
    echo "         python -m venv .venv && .venv/bin/pip install -e ." >&2
    exit 1
fi

if [[ ! -x .venv/bin/nj-ingest-fec ]]; then
    echo "ERROR: .venv/bin/nj-ingest-fec not found. Re-install: pip install -e ." >&2
    exit 1
fi

DSN="$NEON_DATABASE_URL"
PYTHON=.venv/bin/python
NJ_MIGRATE=.venv/bin/nj-migrate
NJ_FEC=.venv/bin/nj-ingest-fec
FEC_CYCLE="${FEC_CYCLE:-2024}"

if [[ ! "$FEC_CYCLE" =~ ^[0-9]{4}$ ]]; then
    echo "ERROR: FEC_CYCLE must be a 4-digit year; got: $FEC_CYCLE" >&2
    exit 1
fi

echo "[deploy:p2] Repo root: $REPO_ROOT"
echo "[deploy:p2] Target DB host: $(echo "$DSN" | sed -E 's,.*@([^/?]+).*,\1,')"
echo "[deploy:p2] FEC cycle:  $FEC_CYCLE"
echo

# ----------------------------------------------------------------------------
# 1. Reachability check
# ----------------------------------------------------------------------------

echo "[1/7] Pinging Neon..."
"$PYTHON" - <<'PY'
import os, psycopg
url = os.environ["NEON_DATABASE_URL"]
with psycopg.connect(url, connect_timeout=20) as conn, conn.cursor() as cur:
    cur.execute("SELECT current_database(), current_user, version()")
    db, user, ver = cur.fetchone()
    print(f"      db={db}, user={user}, pg={ver.split(',')[0]}")
PY
echo

# ----------------------------------------------------------------------------
# 2. Migrations
# ----------------------------------------------------------------------------

echo "[2/7] Applying migrations (db/migrations/*.sql)..."
"$NJ_MIGRATE" apply --dsn "$DSN"
echo

# ----------------------------------------------------------------------------
# 3. Seeds
# ----------------------------------------------------------------------------

echo "[3/7] Applying seeds (db/seeds/*.sql)..."
"$NJ_MIGRATE" seed --dsn "$DSN"
echo

# ----------------------------------------------------------------------------
# 4. FEC bulk load (cn + cm; indiv excluded -- see header for rationale)
# ----------------------------------------------------------------------------

echo "[4/7] Bulk-loading FEC cn$FEC_CYCLE + cm$FEC_CYCLE..."
"$NJ_FEC" load \
    --cycle "$FEC_CYCLE" \
    --files cn,cm \
    --dsn   "$DSN"
echo

# ----------------------------------------------------------------------------
# 5. Materialize structural fraud signals
# ----------------------------------------------------------------------------

echo "[5/7] Refreshing structural fraud signals for cycle $FEC_CYCLE..."
"$PYTHON" - "$FEC_CYCLE" <<'PY'
import os, sys, psycopg
url = os.environ["NEON_DATABASE_URL"]
cycle = sys.argv[1]
with psycopg.connect(url, connect_timeout=20, autocommit=True) as conn, \
     conn.cursor() as cur:
    cur.execute(
        "SELECT derived.refresh_all_fraud_signal_observations(%s)", (cycle,),
    )
    n_total = cur.fetchone()[0]
    print(f"      {n_total:,} structural fraud observations materialized.")
    cur.execute("""
        SELECT signal_id, COUNT(*) AS n
        FROM derived.fraud_signal_observation
        WHERE cycle = %s
        GROUP BY signal_id
        ORDER BY signal_id
    """, (cycle,))
    for sig, n in cur.fetchall():
        print(f"        {sig:<40} {n:>6,}")
PY
echo

# ----------------------------------------------------------------------------
# 6. Backfill freshness signal for raw.fec
# ----------------------------------------------------------------------------

echo "[6/7] Backfilling raw.fec freshness signal..."
"$PYTHON" - <<'PY'
import os, psycopg
url = os.environ["NEON_DATABASE_URL"]
with psycopg.connect(url, connect_timeout=20, autocommit=True) as conn, \
     conn.cursor() as cur:
    cur.execute("""
        SELECT source_id, action,
               to_char(max_ingested_at, 'YYYY-MM-DD HH24:MI'),
               rows_in_table
        FROM derived.f_backfill_freshness_from_ingested_at()
        WHERE source_id = 'raw.fec'
    """)
    rows = cur.fetchall()
    if not rows:
        print("      raw.fec NOT found in v_freshness_backfill_candidates "
              "-- check derived.v_freshness_backfill_candidates definition.")
    else:
        for src, act, ts, n in rows:
            print(f"      {src:<14} action={act:<28} max_ingested_at={ts}  "
                  f"rows={n:,}")
PY
echo

# ----------------------------------------------------------------------------
# 7. Verification
# ----------------------------------------------------------------------------

echo "[7/7] Verifying Pillar 2 production state..."
"$PYTHON" - "$FEC_CYCLE" <<'PY'
import os, sys, psycopg
url = os.environ["NEON_DATABASE_URL"]
cycle = sys.argv[1]
with psycopg.connect(url, connect_timeout=20) as conn, conn.cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM raw.fec_candidate WHERE cycle = %s",
                (cycle,))
    n_cand = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM raw.fec_committee WHERE cycle = %s",
                (cycle,))
    n_cmte = cur.fetchone()[0]
    print(f"    raw.fec_candidate (cycle={cycle}):    {n_cand:,} rows")
    print(f"    raw.fec_committee (cycle={cycle}):    {n_cmte:,} rows")

    cur.execute("""
        SELECT COUNT(*),
               COUNT(DISTINCT entity_id),
               COUNT(DISTINCT signal_id)
        FROM derived.fraud_signal_observation
        WHERE cycle = %s
    """, (cycle,))
    n_obs, n_ent, n_sigs = cur.fetchone()
    print(f"    derived.fraud_signal_observation:    {n_obs:,} obs / "
          f"{n_ent:,} distinct entities / {n_sigs} signals firing")

    cur.execute("""
        SELECT COUNT(*) FROM derived.v_entity_fraud_risk WHERE cycle = %s
    """, (cycle,))
    n_risk = cur.fetchone()[0]
    print(f"    derived.v_entity_fraud_risk:         {n_risk:,} entities scored")

    cur.execute("""
        SELECT COUNT(*),
               MIN(pctile_within_kind_cycle)::FLOAT8,
               MAX(pctile_within_kind_cycle)::FLOAT8
        FROM derived.v_anomaly_score_percentile_by_kind_cycle
        WHERE cycle = %s
    """, (cycle,))
    n_pct, p_lo, p_hi = cur.fetchone()
    print(f"    v_anomaly_score_percentile_by_kind_cycle: "
          f"{n_pct:,} entries; pctile range {p_lo:.3f}..{p_hi:.3f}")

    cur.execute("""
        SELECT entity_kind, entity_id, risk_score::FLOAT8, n_signals_fired
        FROM derived.v_entity_fraud_risk
        WHERE cycle = %s
        ORDER BY risk_score DESC NULLS LAST, entity_id
        LIMIT 5
    """, (cycle,))
    print("    Top-5 highest structural-anomaly entities:")
    for kind, eid, score, n_s in cur.fetchall():
        print(f"      {kind:<10} {eid[:55]:<55} score={score:>7.2f}  "
              f"signals={n_s}")

    cur.execute("""
        SELECT freshness_status,
               COALESCE(round(hours_since_materialized::NUMERIC, 1)::TEXT, '-')
        FROM derived.v_data_freshness_summary
        WHERE source_id = 'raw.fec'
    """)
    row = cur.fetchone()
    if row:
        status, hours = row
        print(f"    raw.fec freshness:                   {status} "
              f"({hours}h since materialization)")
PY
echo
echo "[deploy:p2] Done. Pillar 2 substrate live for cycle $FEC_CYCLE."
echo "             Next: F6/F7 (UI rewrite), F8 (LEIE/SAM/USAspending)."
