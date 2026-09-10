"""Snapshot FEC donor-lane fraud_signal_observation slices from a full-substrate
source DB into a free-tier serving DB (Neon).

WHY A SNAPSHOT (not a live recompute on the serving box):
The two donor signals -- ``donor_on_leie`` and
``candidate_funded_by_excluded_donors`` -- are peer-percentile detectors whose
denominator is COUNT(DISTINCT canonical donor) across the *entire* itemized
individual-contribution file (raw.fec_contribution, ~58M rows / cycle, ~15 GB
expanded). That file cannot live on the 512 MB free-tier Neon box, so the
signals are computed once on the local Docker substrate (which holds the full
file + LEIE) and only the resulting observation rows -- which already carry the
correctly-computed ``peer_percentile`` -- are copied here. Loading a filtered
subset of contributions into Neon and recomputing would corrupt the
denominator; therefore this loader performs a *verbatim column copy* of the
observation rows, never a recompute.

PROVENANCE: derived.fraud_signal_observation rows carry their own lineage via
(signal_id -> derived.fraud_signal_config.formula_version) and the deterministic
peer_percentile/peer_bucket the refresher stamped. This loader does not mint new
provenance; it transports rows the source already produced reproducibly. The
transfer is atomic per (cycle, signal_id) slice: the target slice is deleted and
re-inserted inside a single transaction so a partial load never leaves the
serving view showing a half-populated signal.

STORAGE NOTE ($0 free-tier invariant, AGENTS.md): Neon is capped at 512 MB.
``donor_on_leie`` is a ~24k-row national, known-noisy-v1 feed (~11 MB on disk);
``candidate_funded_by_excluded_donors`` is the ~1.7k-row NJ-relevant deliverable
(<1 MB). Pass only the slices you can afford. The loader refuses to write a slice
that would push the target within --min-free-mb of the cap.

Usage:
    SOURCE_PG_DSN='postgresql://postgres:njlocal@localhost:5433/nj' \
    TARGET_PG_DSN='<neon-dsn>' \
    python -m scripts.load_fec_donor_lane_snapshot \
        --cycle 2024 --signal candidate_funded_by_excluded_donors
"""

from __future__ import annotations

import logging

import click
import psycopg

log = logging.getLogger(__name__)

# Straight verbatim copy: the refresher already stamped every derived value.
_OBS_COLUMNS = [
    "cycle", "entity_kind", "entity_id", "signal_id", "raw_value", "severity",
    "peer_bucket", "peer_percentile", "evidence_url", "materialized_at",
]

_NEON_CAP_BYTES = 512 * 1024 * 1024

_SELECT_SQL = f"""
SELECT {", ".join(_OBS_COLUMNS)}
FROM derived.fraud_signal_observation
WHERE cycle = %(cycle)s AND signal_id = %(signal_id)s
"""


def _db_size_bytes(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT pg_database_size(current_database())").fetchone()
    return int(row[0]) if row else 0


@click.command()
@click.option("--source-dsn", envvar="SOURCE_PG_DSN", required=True,
              help="Substrate DB holding the full contribution file + computed observations.")
@click.option("--target-dsn", envvar="TARGET_PG_DSN", required=True,
              help="Serving DB to receive the observation slices (e.g. Neon).")
@click.option("--cycle", required=True, help="FEC cycle, e.g. 2024.")
@click.option("--signal", "signals", multiple=True, required=True,
              type=click.Choice(["donor_on_leie", "candidate_funded_by_excluded_donors"]),
              help="Signal slice(s) to snapshot. Repeatable.")
@click.option("--min-free-mb", type=int, default=6, show_default=True,
              help="Refuse to load a slice that would leave the target with less "
                   "than this much headroom under the 512 MB free-tier cap.")
def cli(source_dsn: str, target_dsn: str, cycle: str,
        signals: tuple[str, ...], min_free_mb: int) -> None:
    """Copy one or more donor-lane observation slices from source to target."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    insert_sql = (
        f"INSERT INTO derived.fraud_signal_observation ({', '.join(_OBS_COLUMNS)}) "
        f"VALUES ({', '.join(['%s'] * len(_OBS_COLUMNS))})"
    )

    with psycopg.connect(source_dsn) as src, psycopg.connect(target_dsn) as tgt:
        for signal_id in signals:
            rows = src.execute(
                _SELECT_SQL, {"cycle": cycle, "signal_id": signal_id}
            ).fetchall()
            if not rows:
                log.warning("source has 0 rows for (%s, %s) -- skipping",
                            cycle, signal_id)
                continue

            # Crude but safe pre-flight: ~payload*2.2 approximates on-disk cost
            # incl. row + index overhead. Refuse if it would breach the cap.
            payload = sum(
                len(str(r[2] or "")) + len(str(r[8] or "")) + 80 for r in rows
            )
            est_disk = int(payload * 2.2)
            free = _NEON_CAP_BYTES - _db_size_bytes(tgt)
            if free - est_disk < min_free_mb * 1024 * 1024:
                raise click.ClickException(
                    f"refusing ({cycle}, {signal_id}): ~{est_disk // 1024 // 1024} MB "
                    f"would leave <{min_free_mb} MB free (current free "
                    f"~{free // 1024 // 1024} MB). Load a smaller slice or free space."
                )

            with tgt.cursor() as cur:
                cur.execute(
                    "DELETE FROM derived.fraud_signal_observation "
                    "WHERE cycle = %s AND signal_id = %s",
                    (cycle, signal_id),
                )
                cur.executemany(insert_sql, rows)
            tgt.commit()
            log.info("snapshotted %d rows for (%s, %s) into target",
                     len(rows), cycle, signal_id)


if __name__ == "__main__":
    cli()
