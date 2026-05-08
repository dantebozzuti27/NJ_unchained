/**
 * SQL queries the screener UI runs against the L3a/L2/L1 fraud surface.
 *
 * Design rules:
 *  - Every query is parameterized via Neon's tagged-template syntax;
 *    NO string interpolation of user input. Neon's template tag
 *    drops the bind values into the protocol layer, eliminating
 *    SQL injection from the ground up.
 *  - Every query has a hard LIMIT and an explicit ORDER BY; the
 *    UI is read-only and the platform is research-tier (latency
 *    not user-perceived), but unbounded scans against multi-million-
 *    row tables would blow Neon's per-query timeouts.
 *  - Every query degrades gracefully on empty tables (returns []
 *    rather than throwing) so a fresh deployment without data
 *    serves an empty state, not a 500.
 *  - Display-name resolution is best-effort; we LEFT JOIN
 *    fec_candidate / fec_committee on the entity kind and fall
 *    back to entity_id when no name is found.
 */

import { getSql } from "./db";
import type {
  EntityDetail,
  EntityKind,
  PlatformStatus,
  RiskRow,
  SignalRow,
} from "./types";

const VALID_KINDS: ReadonlySet<EntityKind> = new Set([
  "candidate",
  "committee",
  "treasurer",
  "donor",
  "donor_cluster",
  "contractor",
  "address",
]);

export function isValidKind(k: string): k is EntityKind {
  return VALID_KINDS.has(k as EntityKind);
}

const CONTRIB_THRESHOLD = 0.95;

/**
 * Top-N entities by risk score, optionally filtered by kind.
 * Reads derived.v_entity_fraud_risk (L3a).
 */
export async function listTopRiskEntities(opts: {
  cycle: string;
  kind?: EntityKind;
  limit?: number;
}): Promise<RiskRow[]> {
  const sql = getSql();
  const limit = Math.min(Math.max(opts.limit ?? 100, 1), 500);
  const cycle = opts.cycle;
  const kind = opts.kind;

  // We use display-name resolution in two LEFT JOINs (candidate +
  // committee). The other entity kinds (treasurer, donor, etc.)
  // don't have a separate name table; their entity_id IS the name.
  // The COALESCE chain picks the first non-null label.
  //
  // Two derived columns (n_contributing_families, distinct
  // signal_families) are computed from the view's own parallel-aligned
  // (signal_families, peer_percentiles) arrays via unnest. The view does
  // NOT expose n_contributing_families as a column -- that count lives
  // only inside derived.fraud_risk_score(...) as a local plpgsql
  // variable for the diversity-bonus arithmetic. Computing it here from
  // arrays the view DOES expose keeps the per-row cost at zero I/O and
  // matches the contributing-threshold semantics the score formula uses
  // (peer_percentile >= 0.95). signal_families is also distinct'ed for
  // display because the raw column is parallel-aligned with signals_fired
  // (one entry per fired signal, NOT a deduplicated set).
  const rows = kind
    ? await sql`
        SELECT
          r.cycle,
          r.entity_kind,
          r.entity_id,
          COALESCE(cand.cand_name, cmte.cmte_nm, NULL) AS display_name,
          r.risk_score::FLOAT8 AS risk_score,
          COALESCE((
            SELECT COUNT(DISTINCT fam)::INT
            FROM unnest(r.signal_families, r.peer_percentiles) AS u(fam, pct)
            WHERE pct >= ${CONTRIB_THRESHOLD}
              AND fam IS NOT NULL
          ), 0) AS n_contributing_families,
          COALESCE((
            SELECT array_agg(DISTINCT fam ORDER BY fam)
            FROM unnest(r.signal_families) AS u(fam)
            WHERE fam IS NOT NULL
          ), ARRAY[]::TEXT[]) AS signal_families,
          r.n_signals_fired::INT AS n_signals
        FROM derived.v_entity_fraud_risk r
        LEFT JOIN raw.fec_candidate cand
          ON r.entity_kind = 'candidate'
         AND cand.cycle = r.cycle
         AND cand.cand_id = r.entity_id
        LEFT JOIN raw.fec_committee cmte
          ON r.entity_kind = 'committee'
         AND cmte.cycle = r.cycle
         AND cmte.cmte_id = r.entity_id
        WHERE r.cycle = ${cycle}
          AND r.entity_kind = ${kind}
        ORDER BY r.risk_score DESC, r.entity_id ASC
        LIMIT ${limit}
      `
    : await sql`
        SELECT
          r.cycle,
          r.entity_kind,
          r.entity_id,
          COALESCE(cand.cand_name, cmte.cmte_nm, NULL) AS display_name,
          r.risk_score::FLOAT8 AS risk_score,
          COALESCE((
            SELECT COUNT(DISTINCT fam)::INT
            FROM unnest(r.signal_families, r.peer_percentiles) AS u(fam, pct)
            WHERE pct >= ${CONTRIB_THRESHOLD}
              AND fam IS NOT NULL
          ), 0) AS n_contributing_families,
          COALESCE((
            SELECT array_agg(DISTINCT fam ORDER BY fam)
            FROM unnest(r.signal_families) AS u(fam)
            WHERE fam IS NOT NULL
          ), ARRAY[]::TEXT[]) AS signal_families,
          r.n_signals_fired::INT AS n_signals
        FROM derived.v_entity_fraud_risk r
        LEFT JOIN raw.fec_candidate cand
          ON r.entity_kind = 'candidate'
         AND cand.cycle = r.cycle
         AND cand.cand_id = r.entity_id
        LEFT JOIN raw.fec_committee cmte
          ON r.entity_kind = 'committee'
         AND cmte.cycle = r.cycle
         AND cmte.cmte_id = r.entity_id
        WHERE r.cycle = ${cycle}
        ORDER BY r.risk_score DESC, r.entity_id ASC
        LIMIT ${limit}
      `;

  return (rows as Record<string, unknown>[]).map((r) => ({
    cycle: String(r.cycle),
    entity_kind: r.entity_kind as EntityKind,
    entity_id: String(r.entity_id),
    display_name: r.display_name == null ? null : String(r.display_name),
    risk_score: Number(r.risk_score),
    n_contributing_families: Number(r.n_contributing_families),
    signal_families: Array.isArray(r.signal_families)
      ? (r.signal_families as string[])
      : [],
    n_signals: Number(r.n_signals),
  }));
}

/**
 * One-row detail for a specific entity, with all firing signals
 * and per-signal config (family + threshold) joined in.
 */
export async function getEntityDetail(opts: {
  cycle: string;
  kind: EntityKind;
  id: string;
}): Promise<EntityDetail | null> {
  const sql = getSql();
  const { cycle, kind, id } = opts;

  const headerRows = (await sql`
    SELECT
      r.cycle,
      r.entity_kind,
      r.entity_id,
      COALESCE(cand.cand_name, cmte.cmte_nm, NULL) AS display_name,
      r.risk_score::FLOAT8 AS risk_score,
      COALESCE((
        SELECT COUNT(DISTINCT fam)::INT
        FROM unnest(r.signal_families, r.peer_percentiles) AS u(fam, pct)
        WHERE pct >= ${CONTRIB_THRESHOLD}
          AND fam IS NOT NULL
      ), 0) AS n_contributing_families,
      COALESCE((
        SELECT array_agg(DISTINCT fam ORDER BY fam)
        FROM unnest(r.signal_families) AS u(fam)
        WHERE fam IS NOT NULL
      ), ARRAY[]::TEXT[]) AS signal_families
    FROM derived.v_entity_fraud_risk r
    LEFT JOIN raw.fec_candidate cand
      ON r.entity_kind = 'candidate'
     AND cand.cycle = r.cycle
     AND cand.cand_id = r.entity_id
    LEFT JOIN raw.fec_committee cmte
      ON r.entity_kind = 'committee'
     AND cmte.cycle = r.cycle
     AND cmte.cmte_id = r.entity_id
    WHERE r.cycle = ${cycle}
      AND r.entity_kind = ${kind}
      AND r.entity_id = ${id}
    LIMIT 1
  `) as Record<string, unknown>[];

  if (headerRows.length === 0) return null;
  const h = headerRows[0];

  const signalRows = (await sql`
    SELECT
      o.signal_id,
      cfg.signal_family,
      o.raw_value::FLOAT8 AS raw_value,
      o.severity::INT AS severity,
      o.peer_bucket,
      o.peer_percentile::FLOAT8 AS peer_percentile,
      o.evidence_url,
      cfg.min_actionable_threshold::FLOAT8 AS min_actionable_threshold
    FROM derived.fraud_signal_observation o
    LEFT JOIN derived.fraud_signal_config cfg
      ON cfg.signal_id = o.signal_id
    WHERE o.cycle = ${cycle}
      AND o.entity_kind = ${kind}
      AND o.entity_id = ${id}
    ORDER BY o.peer_percentile DESC NULLS LAST, o.signal_id ASC
    LIMIT 100
  `) as Record<string, unknown>[];

  const signals: SignalRow[] = signalRows.map((s) => {
    const pct = s.peer_percentile == null ? 0 : Number(s.peer_percentile);
    return {
      signal_id: String(s.signal_id),
      signal_family: s.signal_family == null ? null : String(s.signal_family),
      raw_value: Number(s.raw_value),
      severity: Number(s.severity),
      peer_bucket: String(s.peer_bucket),
      peer_percentile: pct,
      evidence_url: String(s.evidence_url),
      is_contributing: pct >= CONTRIB_THRESHOLD,
      min_actionable_threshold:
        s.min_actionable_threshold == null
          ? null
          : Number(s.min_actionable_threshold),
    };
  });

  return {
    cycle: String(h.cycle),
    entity_kind: h.entity_kind as EntityKind,
    entity_id: String(h.entity_id),
    display_name: h.display_name == null ? null : String(h.display_name),
    risk_score: Number(h.risk_score),
    n_contributing_families: Number(h.n_contributing_families),
    signal_families: Array.isArray(h.signal_families)
      ? (h.signal_families as string[])
      : [],
    signals,
  };
}

/**
 * Latest cycle present in derived.v_entity_fraud_risk, falling back to
 * the highest cycle in raw.fec_candidate, or '2024' if both empty.
 */
export async function resolveDefaultCycle(): Promise<string> {
  const sql = getSql();
  try {
    const rows = (await sql`
      SELECT MAX(cycle) AS c FROM derived.v_entity_fraud_risk
    `) as Record<string, unknown>[];
    if (rows[0]?.c) return String(rows[0].c);
  } catch {
    /* table missing on fresh install -- fall through */
  }
  try {
    const rows = (await sql`
      SELECT MAX(cycle) AS c FROM raw.fec_candidate
    `) as Record<string, unknown>[];
    if (rows[0]?.c) return String(rows[0].c);
  } catch {
    /* fall through */
  }
  return "2024";
}

/**
 * Aggregate platform health for the home page.
 */
export async function getPlatformStatus(): Promise<PlatformStatus> {
  const sql = getSql();
  let cycle = "2024";
  let totalEntities = 0;
  let totalSignalsFired = 0;
  const family: Record<string, number> = {};
  let vintage: string | null = null;

  try {
    cycle = await resolveDefaultCycle();
  } catch {
    /* leave default */
  }

  try {
    const rows = (await sql`
      SELECT COUNT(*)::INT AS n
      FROM derived.v_entity_fraud_risk
      WHERE cycle = ${cycle}
    `) as Record<string, unknown>[];
    totalEntities = Number(rows[0]?.n ?? 0);
  } catch {
    /* table missing -- leave 0 */
  }

  try {
    const rows = (await sql`
      SELECT COUNT(*)::INT AS n
      FROM derived.fraud_signal_observation
      WHERE cycle = ${cycle}
    `) as Record<string, unknown>[];
    totalSignalsFired = Number(rows[0]?.n ?? 0);
  } catch {
    /* table missing -- leave 0 */
  }

  try {
    const rows = (await sql`
      SELECT cfg.signal_family, COUNT(*)::INT AS n
      FROM derived.fraud_signal_observation o
      LEFT JOIN derived.fraud_signal_config cfg
        ON cfg.signal_id = o.signal_id
      WHERE o.cycle = ${cycle}
      GROUP BY cfg.signal_family
      ORDER BY n DESC
    `) as Record<string, unknown>[];
    for (const r of rows) {
      const f = r.signal_family == null ? "(unknown)" : String(r.signal_family);
      family[f] = Number(r.n ?? 0);
    }
  } catch {
    /* leave empty */
  }

  try {
    const rows = (await sql`
      SELECT MAX(materialized_at) AS t
      FROM derived.fraud_signal_observation
      WHERE cycle = ${cycle}
    `) as Record<string, unknown>[];
    if (rows[0]?.t) vintage = new Date(String(rows[0].t)).toISOString();
  } catch {
    /* leave null */
  }

  return {
    db_reachable: true,
    cycle_default: cycle,
    total_entities: totalEntities,
    total_signals_fired: totalSignalsFired,
    signal_count_by_family: family,
    vintage_iso: vintage,
  };
}
