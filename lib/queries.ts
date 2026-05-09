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
  CycleSummary,
  EntityDetail,
  EntityHeaderInfo,
  EntityKind,
  EvidenceCard,
  NjAnomalyCard,
  NjFederalOfficial,
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

/**
 * Sitting NJ federal incumbents (2 US Senators + 12 US Representatives).
 * Reads derived.v_nj_federal_officials. Substrate-honesty: this view is
 * federal-only; NJ Governor and state legislature live at NJ ELEC,
 * scoped to the F8.5 ingester (deferred work item).
 */
export async function getNjFederalOfficials(
  cycle: string,
): Promise<NjFederalOfficial[]> {
  const sql = getSql();
  const rows = (await sql`
    SELECT
      cycle,
      entity_id,
      official_name,
      office_code,
      office_label,
      office_district,
      office_party,
      incumbent_status,
      election_year,
      prior_incumbent_cycles::INT AS prior_incumbent_cycles,
      risk_score::FLOAT8 AS risk_score,
      n_signals_fired::INT AS n_signals_fired,
      COALESCE(signals_fired, ARRAY[]::TEXT[]) AS signals_fired,
      max_severity::INT AS max_severity
    FROM derived.v_nj_federal_officials
    WHERE cycle = ${cycle}
  `) as Record<string, unknown>[];

  return rows.map((r) => ({
    cycle: String(r.cycle),
    entity_id: String(r.entity_id),
    official_name: String(r.official_name),
    office_code: String(r.office_code),
    office_label: String(r.office_label),
    office_district: r.office_district == null ? null : String(r.office_district),
    office_party: r.office_party == null ? null : String(r.office_party),
    incumbent_status: String(r.incumbent_status),
    election_year: r.election_year == null ? null : Number(r.election_year),
    prior_incumbent_cycles: Number(r.prior_incumbent_cycles ?? 0),
    risk_score: Number(r.risk_score),
    n_signals_fired: Number(r.n_signals_fired),
    signals_fired: Array.isArray(r.signals_fired)
      ? (r.signals_fired as string[])
      : [],
    max_severity: Number(r.max_severity),
  }));
}

/**
 * Top-N most anomalous NJ-relevant entities for the /risk overview
 * Section 2. One row per entity (DISTINCT ON entity_kind+entity_id),
 * with the highest-severity firing signal selected as the preview to
 * surface on the card. The user clicks through to /risk/[kind]/[id]
 * to see all firing signals.
 *
 * Why aggregate v_entity_fraud_evidence rather than v_entity_fraud_risk:
 * the evidence view already carries the is_nj flag (via raw.fec_*
 * joins) AND the rendered plain-English preview text, so this is one
 * query rather than three with extra JOINs.
 */
export async function listTopNjAnomalies(opts: {
  cycle: string;
  limit?: number;
}): Promise<NjAnomalyCard[]> {
  const sql = getSql();
  const limit = Math.min(Math.max(opts.limit ?? 20, 1), 100);
  const rows = (await sql`
    WITH deduped AS (
      SELECT DISTINCT ON (entity_kind, entity_id)
        cycle, entity_kind, entity_id,
        display_name,
        signal_id, severity, peer_percentile,
        rendered_explanation,
        citation_authority, citation_section,
        office_code, office_district, office_party,
        office_incumbent_status
      FROM derived.v_entity_fraud_evidence
      WHERE cycle = ${opts.cycle}
        AND is_nj = TRUE
      ORDER BY entity_kind, entity_id,
               severity DESC NULLS LAST,
               peer_percentile DESC NULLS LAST
    )
    SELECT
      d.cycle,
      d.entity_kind,
      d.entity_id,
      d.display_name,
      d.signal_id                                  AS preview_signal_id,
      d.severity::INT                              AS preview_severity,
      d.peer_percentile::FLOAT8                    AS preview_peer_percentile,
      d.rendered_explanation                       AS preview_explanation,
      d.citation_authority                         AS preview_citation_authority,
      d.citation_section                           AS preview_citation_section,
      d.office_code, d.office_district, d.office_party,
      d.office_incumbent_status,
      COALESCE(r.risk_score, 0)::FLOAT8            AS risk_score,
      COALESCE(r.n_signals_fired, 0)::INT          AS n_signals
    FROM deduped d
    LEFT JOIN derived.v_entity_fraud_risk r
      ON  r.cycle = d.cycle
      AND r.entity_kind = d.entity_kind
      AND r.entity_id = d.entity_id
    ORDER BY r.risk_score DESC NULLS LAST, d.severity DESC, d.entity_id ASC
    LIMIT ${limit}
  `) as Record<string, unknown>[];

  return rows.map((r) => ({
    cycle: String(r.cycle),
    entity_kind: r.entity_kind as EntityKind,
    entity_id: String(r.entity_id),
    display_name: r.display_name == null ? null : String(r.display_name),
    risk_score: Number(r.risk_score),
    n_signals: Number(r.n_signals),
    preview_signal_id: String(r.preview_signal_id),
    preview_severity: Number(r.preview_severity),
    preview_peer_percentile:
      r.preview_peer_percentile == null
        ? null
        : Number(r.preview_peer_percentile),
    preview_explanation: String(r.preview_explanation),
    preview_citation_authority:
      r.preview_citation_authority == null
        ? null
        : String(r.preview_citation_authority),
    preview_citation_section:
      r.preview_citation_section == null
        ? null
        : String(r.preview_citation_section),
    office_code: r.office_code == null ? null : String(r.office_code),
    office_district:
      r.office_district == null ? null : String(r.office_district),
    office_party: r.office_party == null ? null : String(r.office_party),
    office_incumbent_status:
      r.office_incumbent_status == null
        ? null
        : String(r.office_incumbent_status),
  }));
}

/**
 * Cycles that currently have FEC data loaded. Ordered most-recent-first.
 * Powers the cycle picker on /risk so the user can switch between
 * (e.g.) the current cycle 2026 and the historical cycle 2024.
 */
export async function listAvailableCycles(): Promise<string[]> {
  const sql = getSql();
  try {
    const rows = (await sql`
      SELECT DISTINCT cycle
      FROM raw.fec_candidate
      ORDER BY cycle DESC
    `) as Record<string, unknown>[];
    return rows.map((r) => String(r.cycle));
  } catch {
    return [];
  }
}

/**
 * Per-cycle scope + freshness summary. Renders the "cycle 2026 — N
 * candidates, M committees, refreshed Xh ago" line above the /risk
 * sections so the user can immediately tell how current the data is.
 */
export async function getCycleSummary(
  cycle: string,
): Promise<CycleSummary> {
  const sql = getSql();
  let n_candidates = 0;
  let n_committees = 0;
  let ingested_at_iso: string | null = null;
  let hours_since_ingest: number | null = null;

  try {
    const rows = (await sql`
      SELECT
        (SELECT COUNT(*)::INT FROM raw.fec_candidate WHERE cycle = ${cycle})
          AS n_candidates,
        (SELECT COUNT(*)::INT FROM raw.fec_committee WHERE cycle = ${cycle})
          AS n_committees,
        GREATEST(
          (SELECT MAX(ingested_at) FROM raw.fec_candidate WHERE cycle = ${cycle}),
          (SELECT MAX(ingested_at) FROM raw.fec_committee WHERE cycle = ${cycle})
        ) AS max_ingested_at
    `) as Record<string, unknown>[];
    if (rows.length > 0) {
      const r = rows[0];
      n_candidates = Number(r.n_candidates ?? 0);
      n_committees = Number(r.n_committees ?? 0);
      if (r.max_ingested_at) {
        const t = new Date(String(r.max_ingested_at));
        ingested_at_iso = t.toISOString();
        hours_since_ingest = (Date.now() - t.getTime()) / 3_600_000;
      }
    }
  } catch {
    /* fall through with zeros */
  }

  return {
    cycle,
    n_candidates,
    n_committees,
    ingested_at_iso,
    hours_since_ingest,
  };
}

/**
 * Bare entity metadata for the /risk/[kind]/[id] header. Used when the
 * entity has no observations (clean incumbent, etc.) so the page can
 * render a substrate-honest "no signals firing" state instead of 404.
 *
 * Looks the entity up in raw.fec_candidate / raw.fec_committee. For
 * treasurer + address kinds the entity_id IS the human-readable label,
 * so we synthesize the row without a join.
 */
export async function getEntityHeader(opts: {
  cycle: string;
  kind: EntityKind;
  id: string;
}): Promise<EntityHeaderInfo | null> {
  const sql = getSql();
  const { cycle, kind, id } = opts;

  if (kind === "candidate") {
    const rows = (await sql`
      SELECT
        cycle,
        cand_id            AS entity_id,
        cand_name          AS display_name,
        cand_office        AS office_code,
        cand_office_st     AS office_state,
        cand_office_district AS office_district,
        cand_pty_affiliation AS office_party,
        cand_ici           AS office_incumbent_status,
        (cand_office_st = 'NJ') AS is_nj
      FROM raw.fec_candidate
      WHERE cycle = ${cycle} AND cand_id = ${id}
      LIMIT 1
    `) as Record<string, unknown>[];
    if (rows.length === 0) return null;
    const r = rows[0];
    return {
      cycle: String(r.cycle),
      entity_kind: "candidate",
      entity_id: String(r.entity_id),
      display_name: r.display_name == null ? null : String(r.display_name),
      is_nj: Boolean(r.is_nj),
      office_code: r.office_code == null ? null : String(r.office_code),
      office_state: r.office_state == null ? null : String(r.office_state),
      office_district:
        r.office_district == null ? null : String(r.office_district),
      office_party: r.office_party == null ? null : String(r.office_party),
      office_incumbent_status:
        r.office_incumbent_status == null
          ? null
          : String(r.office_incumbent_status),
    };
  }

  if (kind === "committee") {
    const rows = (await sql`
      SELECT
        cycle,
        cmte_id      AS entity_id,
        cmte_nm      AS display_name,
        (cmte_st = 'NJ') AS is_nj
      FROM raw.fec_committee
      WHERE cycle = ${cycle} AND cmte_id = ${id}
      LIMIT 1
    `) as Record<string, unknown>[];
    if (rows.length === 0) return null;
    const r = rows[0];
    return {
      cycle: String(r.cycle),
      entity_kind: "committee",
      entity_id: String(r.entity_id),
      display_name: r.display_name == null ? null : String(r.display_name),
      is_nj: Boolean(r.is_nj),
      office_code: null,
      office_state: null,
      office_district: null,
      office_party: null,
      office_incumbent_status: null,
    };
  }

  if (kind === "treasurer") {
    const rows = (await sql`
      SELECT
        ${cycle}::CHAR(4) AS cycle,
        ${id}::TEXT AS entity_id,
        ${id}::TEXT AS display_name,
        EXISTS (
          SELECT 1 FROM raw.fec_committee c
          WHERE c.cycle = ${cycle}
            AND UPPER(TRIM(c.tres_nm)) = UPPER(TRIM(${id}))
            AND c.cmte_st = 'NJ'
        ) AS is_nj
    `) as Record<string, unknown>[];
    const r = rows[0];
    return {
      cycle: String(r.cycle),
      entity_kind: "treasurer",
      entity_id: String(r.entity_id),
      display_name: String(r.display_name),
      is_nj: Boolean(r.is_nj),
      office_code: null,
      office_state: null,
      office_district: null,
      office_party: null,
      office_incumbent_status: null,
    };
  }

  if (kind === "address") {
    // Address entity_id format: "address|city|state|zip5"
    const parts = id.split("|");
    const [addr, city, state, zip] = [parts[0], parts[1], parts[2], parts[3]];
    const display = [addr, city, state, zip]
      .filter((s) => s && s.length > 0)
      .join(" ");
    return {
      cycle,
      entity_kind: "address",
      entity_id: id,
      display_name: display || id,
      is_nj: state === "NJ",
      office_code: null,
      office_state: state ?? null,
      office_district: null,
      office_party: null,
      office_incumbent_status: null,
    };
  }

  return null;
}

/**
 * Per-entity evidence cards for the /risk/[kind]/[id] detail page.
 * Reads derived.v_entity_fraud_evidence; one row per firing signal.
 * Each row carries a fully-rendered plain-English explanation, the
 * federal-authority citation, severity precedent, and the upstream-
 * verify URL the UI links out to.
 */
export async function getEntityEvidenceCards(opts: {
  cycle: string;
  kind: EntityKind;
  id: string;
}): Promise<EvidenceCard[]> {
  const sql = getSql();
  const rows = (await sql`
    SELECT
      cycle,
      entity_kind,
      entity_id,
      signal_id,
      raw_value::FLOAT8                  AS raw_value,
      severity::INT                       AS severity,
      peer_bucket,
      peer_percentile::FLOAT8             AS peer_percentile,
      is_nj,
      display_name,
      office_code, office_state, office_district,
      office_party, office_incumbent_status,
      rendered_explanation,
      rule_text, citation_authority, citation_section, citation_url,
      severity_basis, severity_precedent_url, severity_precedent_summary,
      upstream_verify_url, upstream_verify_label, upstream_source
    FROM derived.v_entity_fraud_evidence
    WHERE cycle = ${opts.cycle}
      AND entity_kind = ${opts.kind}
      AND entity_id = ${opts.id}
    ORDER BY severity DESC NULLS LAST,
             peer_percentile DESC NULLS LAST,
             signal_id ASC
    LIMIT 100
  `) as Record<string, unknown>[];

  return rows.map((r) => ({
    cycle: String(r.cycle),
    entity_kind: r.entity_kind as EntityKind,
    entity_id: String(r.entity_id),
    signal_id: String(r.signal_id),
    raw_value: r.raw_value == null ? null : Number(r.raw_value),
    severity: Number(r.severity),
    peer_bucket: r.peer_bucket == null ? null : String(r.peer_bucket),
    peer_percentile:
      r.peer_percentile == null ? null : Number(r.peer_percentile),
    is_nj: Boolean(r.is_nj),
    display_name: r.display_name == null ? null : String(r.display_name),
    office_code: r.office_code == null ? null : String(r.office_code),
    office_state: r.office_state == null ? null : String(r.office_state),
    office_district:
      r.office_district == null ? null : String(r.office_district),
    office_party: r.office_party == null ? null : String(r.office_party),
    office_incumbent_status:
      r.office_incumbent_status == null
        ? null
        : String(r.office_incumbent_status),
    rendered_explanation: String(r.rendered_explanation ?? ""),
    rule_text: r.rule_text == null ? null : String(r.rule_text),
    citation_authority:
      r.citation_authority == null ? null : String(r.citation_authority),
    citation_section:
      r.citation_section == null ? null : String(r.citation_section),
    citation_url: r.citation_url == null ? null : String(r.citation_url),
    severity_basis:
      r.severity_basis == null ? null : String(r.severity_basis),
    severity_precedent_url:
      r.severity_precedent_url == null
        ? null
        : String(r.severity_precedent_url),
    severity_precedent_summary:
      r.severity_precedent_summary == null
        ? null
        : String(r.severity_precedent_summary),
    upstream_verify_url: String(r.upstream_verify_url ?? ""),
    upstream_verify_label:
      r.upstream_verify_label == null
        ? null
        : String(r.upstream_verify_label),
    upstream_source:
      r.upstream_source == null ? null : String(r.upstream_source),
  }));
}
