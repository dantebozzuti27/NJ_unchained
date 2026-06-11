"""Populate derived.high_value_leads_snapshot from a full-substrate source DB.

The lead ranking (derived.v_high_value_leads) can only be computed where the
national CMS substrate lives (a local Docker Postgres or the Oracle box). A
free-tier serving DB (Neon) holds only NJ raw, so it cannot rank national
leads live. This loader bridges the two: it reads the top-N ranked leads from
the SOURCE (with display_name / state resolved against the substrate that has
them), then atomically refreshes the snapshot table on the TARGET.

Provenance (verifiable-data invariant): every row is stamped with
formula_version, source_scope, a reproducible source_vintage_hash (sha256 over
the distinct CMS source shas), snapshot_at, and data_quality = 'computed'.

Usage:
    SOURCE_PG_DSN='postgresql://postgres:njlocal@localhost:5433/nj' \
    TARGET_PG_DSN='<neon-dsn>' \
    nj-load-leads-snapshot --scope national --n-undetected 60 --n-caught 15
"""

from __future__ import annotations

import hashlib
import logging

import click
import psycopg

log = logging.getLogger(__name__)

SNAPSHOT_FORMULA_VERSION = "3.7.0-fraud-national-leads-snapshot-v1"

# Column order shared by the SELECT projection and the INSERT.
_LEAD_COLUMNS = [
    "lead_rank", "entity_kind", "entity_id", "display_name", "provider_state",
    "is_nj", "latest_cycle", "n_cycles", "n_signals", "n_families",
    "max_severity", "best_reward_tier", "reward_eligible", "has_prior_sanction",
    "repeat_violator", "multi_source", "provider_scale_usd", "peak_exposure_usd",
    "total_exposure_usd", "reward_low_usd", "reward_high_usd", "driver_signal_id",
    "driver_signal_family", "recovery_program", "recovery_channel",
    "recovery_channel_url", "statute_citation", "statute_url",
]

# Top-N ranked leads, with name/state resolved against the substrate. Mirrors the
# LATERAL name-resolution in lib/queries.ts listHighValueLeads, plus state.
_SOURCE_SQL = """
WITH sel AS (
    (SELECT * FROM derived.v_high_value_leads
       WHERE NOT has_prior_sanction ORDER BY lead_rank LIMIT %(n_undetected)s)
    UNION ALL
    (SELECT * FROM derived.v_high_value_leads
       WHERE has_prior_sanction ORDER BY lead_rank LIMIT %(n_caught)s)
)
SELECT
    s.lead_rank, s.entity_kind, s.entity_id,
    COALESCE(
        pd.nm, pb.nm, cand.cand_name, cmte.cmte_nm,
        CASE s.entity_kind
            WHEN 'treasurer' THEN s.entity_id
            WHEN 'address'   THEN split_part(s.entity_id, '|', 1)
            ELSE NULL
        END
    )                                                       AS display_name,
    COALESCE(pb.st, pd.st)                                  AS provider_state,
    COALESCE(pd.is_nj, pb.is_nj, cand.is_nj, cmte.is_nj, FALSE) AS is_nj,
    s.latest_cycle, s.n_cycles, s.n_signals, s.n_families, s.max_severity,
    s.best_reward_tier, s.reward_eligible, s.has_prior_sanction,
    s.repeat_violator, s.multi_source,
    s.provider_scale_usd, s.peak_exposure_usd, s.total_exposure_usd,
    s.reward_low_usd, s.reward_high_usd,
    s.driver_signal_id, s.driver_signal_family, s.recovery_program,
    s.recovery_channel, s.recovery_channel_url, s.statute_citation, s.statute_url
FROM sel s
LEFT JOIN LATERAL (
    SELECT NULLIF(TRIM(COALESCE(prscrbr_first_name, '') || ' ' ||
                       COALESCE(prscrbr_last_org_name, '')), '') AS nm,
           prscrbr_state_abrvtn AS st,
           (prscrbr_state_abrvtn = 'NJ') AS is_nj
    FROM raw.cms_partd_prescriber
    WHERE s.entity_kind = 'provider' AND npi = s.entity_id
    ORDER BY data_year DESC LIMIT 1
) pd ON TRUE
LEFT JOIN LATERAL (
    SELECT NULLIF(TRIM(COALESCE(prvdr_first_name, '') || ' ' ||
                       COALESCE(prvdr_last_org_name, '')), '') AS nm,
           prvdr_state_abrvtn AS st,
           (prvdr_state_abrvtn = 'NJ') AS is_nj
    FROM raw.cms_physician_provider
    WHERE s.entity_kind = 'provider' AND npi = s.entity_id
    ORDER BY data_year DESC LIMIT 1
) pb ON TRUE
LEFT JOIN LATERAL (
    SELECT cand_name, (cand_office_st = 'NJ') AS is_nj
    FROM raw.fec_candidate
    WHERE s.entity_kind = 'candidate' AND cand_id = s.entity_id
    ORDER BY cycle DESC LIMIT 1
) cand ON TRUE
LEFT JOIN LATERAL (
    SELECT cmte_nm, (cmte_st = 'NJ') AS is_nj
    FROM raw.fec_committee
    WHERE s.entity_kind = 'committee' AND cmte_id = s.entity_id
    ORDER BY cycle DESC LIMIT 1
) cmte ON TRUE
ORDER BY s.lead_rank
"""

# Population-level totals over the FULL source ranking (not just the top-N).
_META_SQL = """
WITH base AS (SELECT * FROM derived.v_high_value_leads),
tier AS (
    SELECT COALESCE(jsonb_object_agg(t::text, n), '{}'::jsonb) AS count_by_tier
    FROM (
        SELECT best_reward_tier AS t, COUNT(*) AS n
        FROM base WHERE best_reward_tier IS NOT NULL GROUP BY best_reward_tier
    ) g
)
SELECT
    COUNT(*)::int                                                 AS n_total,
    COUNT(*) FILTER (WHERE NOT has_prior_sanction)::int           AS n_undetected,
    COUNT(*) FILTER (WHERE has_prior_sanction)::int               AS n_already_caught,
    COUNT(*) FILTER (WHERE multi_source)::int                     AS n_multi_source,
    COUNT(*) FILTER (WHERE repeat_violator)::int                  AS n_repeat_violators,
    COUNT(*) FILTER (WHERE reward_eligible)::int                  AS n_reward_eligible,
    MAX(COALESCE(peak_exposure_usd, provider_scale_usd))
        FILTER (WHERE NOT has_prior_sanction)                     AS max_undetected_scale_usd,
    MAX(peak_exposure_usd)                               AS max_exposure_usd,
    SUM(peak_exposure_usd) FILTER (WHERE reward_eligible)
        AS total_reward_eligible_exposure_usd,
    (SELECT count_by_tier FROM tier)                     AS count_by_tier
FROM base
"""

_VINTAGE_SQL = """
SELECT string_agg(t || ':' || dy || ':' || sha, '|' ORDER BY t, dy)
FROM (
    SELECT 'partd' AS t, data_year::text AS dy, source_sha256 AS sha
      FROM raw.cms_partd_prescriber GROUP BY data_year, source_sha256
    UNION ALL
    SELECT 'partb', data_year::text, source_sha256
      FROM raw.cms_physician_provider GROUP BY data_year, source_sha256
) v
"""


def _vintage_hash(conn: psycopg.Connection) -> str:
    row = conn.execute(_VINTAGE_SQL).fetchone()
    basis = (row[0] if row and row[0] else "no-cms-substrate")
    return hashlib.sha256(basis.encode()).hexdigest()


@click.command()
@click.option("--source-dsn", envvar="SOURCE_PG_DSN", required=True,
              help="Substrate DB with national raw + v_high_value_leads.")
@click.option("--target-dsn", envvar="TARGET_PG_DSN", required=True,
              help="Serving DB to receive the snapshot (e.g. Neon).")
@click.option("--scope", type=click.Choice(["national", "nj"]), default="national")
@click.option("--n-undetected", type=int, default=60, show_default=True)
@click.option("--n-caught", type=int, default=15, show_default=True)
def cli(source_dsn: str, target_dsn: str, scope: str,
        n_undetected: int, n_caught: int) -> None:
    """Refresh derived.high_value_leads_snapshot on the target from the source."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    with psycopg.connect(source_dsn) as src:
        vintage = _vintage_hash(src)
        rows = src.execute(
            _SOURCE_SQL,
            {"n_undetected": n_undetected, "n_caught": n_caught},
        ).fetchall()
        meta = src.execute(_META_SQL).fetchone()
    log.info("read %d leads from source (vintage=%s)", len(rows), vintage[:12])

    if not rows:
        raise click.ClickException(
            "source returned 0 leads -- is the substrate loaded and refreshed?")

    n_shown_undetected = sum(1 for r in rows if not r[13])  # has_prior_sanction col
    n_shown_caught = len(rows) - n_shown_undetected

    cols = ["source_scope", "formula_version", "source_vintage_hash",
            "data_quality", *_LEAD_COLUMNS]
    placeholders = ", ".join(["%s"] * len(cols))
    insert_sql = (
        f"INSERT INTO derived.high_value_leads_snapshot ({', '.join(cols)}) "
        f"VALUES ({placeholders})"
    )
    values = [
        (scope, SNAPSHOT_FORMULA_VERSION, vintage, "computed", *row)
        for row in rows
    ]

    from psycopg.types.json import Json
    if meta is None:  # the aggregate always returns one row; guard for typing
        raise click.ClickException("meta aggregate returned no row")
    (m_total, m_undet, m_caught, m_multi, m_repeat, m_reward,
     m_max_undet, m_max_exp, m_total_reward, m_tier) = meta
    meta_sql = """
        INSERT INTO derived.leads_snapshot_meta (
            source_scope, formula_version, source_vintage_hash, snapshot_at,
            data_quality, n_total, n_undetected, n_already_caught, n_multi_source,
            n_repeat_violators, n_reward_eligible, max_undetected_scale_usd,
            max_exposure_usd, total_reward_eligible_exposure_usd, count_by_tier,
            n_shown_undetected, n_shown_caught
        ) VALUES (%s,%s,%s, now(), 'computed', %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (source_scope) DO UPDATE SET
            formula_version=EXCLUDED.formula_version,
            source_vintage_hash=EXCLUDED.source_vintage_hash,
            snapshot_at=EXCLUDED.snapshot_at, data_quality=EXCLUDED.data_quality,
            n_total=EXCLUDED.n_total, n_undetected=EXCLUDED.n_undetected,
            n_already_caught=EXCLUDED.n_already_caught,
            n_multi_source=EXCLUDED.n_multi_source,
            n_repeat_violators=EXCLUDED.n_repeat_violators,
            n_reward_eligible=EXCLUDED.n_reward_eligible,
            max_undetected_scale_usd=EXCLUDED.max_undetected_scale_usd,
            max_exposure_usd=EXCLUDED.max_exposure_usd,
            total_reward_eligible_exposure_usd=EXCLUDED.total_reward_eligible_exposure_usd,
            count_by_tier=EXCLUDED.count_by_tier,
            n_shown_undetected=EXCLUDED.n_shown_undetected,
            n_shown_caught=EXCLUDED.n_shown_caught
    """
    # Atomic refresh: replace only this scope's rows + meta in one transaction.
    with psycopg.connect(target_dsn) as tgt:
        with tgt.cursor() as cur:
            cur.execute(
                "DELETE FROM derived.high_value_leads_snapshot "
                "WHERE source_scope = %s",
                (scope,),
            )
            cur.executemany(insert_sql, values)
            cur.execute(meta_sql, (
                scope, SNAPSHOT_FORMULA_VERSION, vintage,
                m_total, m_undet, m_caught, m_multi, m_repeat, m_reward,
                m_max_undet, m_max_exp, m_total_reward, Json(m_tier),
                n_shown_undetected, n_shown_caught,
            ))
        tgt.commit()
    log.info("wrote %d %s leads + meta (n_total=%s, n_undetected=%s) into snapshot",
             len(values), scope, m_total, m_undet)


if __name__ == "__main__":
    cli()
