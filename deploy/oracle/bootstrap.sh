#!/usr/bin/env bash
# One-command provisioning of the NJ platform's data substrate on a fresh box
# (Oracle Always Free, or any Postgres 16 host). Idempotent: safe to re-run.
#
# Prereqs (cloud-init.yaml handles these on Oracle):
#   - PostgreSQL 16 running, with a `nj` role + `nj` database
#   - Python 3.13 available
#   - this repo checked out (default /opt/nj)
#
# Usage:
#   PG_DSN='postgresql://nj:PASS@localhost:5432/nj' bash deploy/oracle/bootstrap.sh
#
# Optional env:
#   NJ_YEARS="2024 2023"   # CMS data years to load national (default below)
#   NJ_REPO_DIR=/opt/nj    # repo location
#   NJ_SKIP_INGEST=1       # apply schema only, skip the multi-GB national load
set -euo pipefail

: "${PG_DSN:?set PG_DSN, e.g. postgresql://nj:PASS@localhost:5432/nj}"
NJ_REPO_DIR="${NJ_REPO_DIR:-/opt/nj}"
NJ_YEARS="${NJ_YEARS:-2024 2023}"

cd "$NJ_REPO_DIR"
export PG_DSN

echo "==> [1/6] Python venv + editable install"
if [ ! -d .venv ]; then
  python3.13 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e .

echo "==> [2/6] Apply migrations"
nj-migrate apply

echo "==> [3/6] Apply seeds"
nj-migrate seed

if [ "${NJ_SKIP_INGEST:-0}" = "1" ]; then
  echo "==> NJ_SKIP_INGEST=1 -> schema only; stopping before national load."
  exit 0
fi

echo "==> [4/6] Load national CMS substrate for years: $NJ_YEARS"
for y in $NJ_YEARS; do
  echo "    - Part D $y (national)"
  nj-ingest-cms           fetch-and-load --year "$y"      --national
  echo "    - Part B $y (national)"
  nj-ingest-cms-physician fetch-and-load --data-year "$y" --national
done

echo "==> [5/6] Load national LEIE exclusion labels"
nj-ingest-leie fetch-and-load

echo "==> [6/6] Refresh fraud-signal observations per cycle"
for y in $NJ_YEARS; do
  psql "$PG_DSN" -v ON_ERROR_STOP=1 -tAc \
    "SELECT derived.refresh_all_fraud_signal_observations('$y');"
done

echo
echo "==> Done. Validation harness snapshot:"
psql "$PG_DSN" -v ON_ERROR_STOP=1 -P pager=off -c "
  SELECT cycle, signal_id, n_universe, n_positives, n_flagged,
         n_true_positive, round(lift::numeric,2) AS lift
  FROM derived.v_signal_validation
  ORDER BY cycle DESC, lift DESC NULLS LAST, signal_id;"

echo
echo "Substrate ready. ANALYZE recommended after the bulk load:"
echo "  psql \"\$PG_DSN\" -c 'ANALYZE;'"
