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
  HealthcareSignalCatalogEntry,
  HealthcareSubstrateStatus,
  H1bEmployerLead,
  HighValueLead,
  HighValueLeadsSummary,
  LeadsSnapshotMeta,
  NjAnomalyCard,
  NjCivicIntegritySummary,
  NjFederalOfficial,
  NjStateCandidate,
  PlatformStatus,
  ProviderRiskCard,
  RiskRow,
  SignalRow,
  SignalValidationRow,
} from "./types";

/**
 * The seven NPI-keyed healthcare-fraud signals (FRAUD-F7). Pinned here so
 * the catalog + queue read exactly the provider-domain signals and never
 * accidentally fold in FEC/SAM signals that share the leie_bearing family.
 */
export const HEALTHCARE_SIGNAL_IDS = [
  "provider_excluded_billing",
  "provider_excluded_billing_partb",
  "state_excluded_provider_billing",
  "opioid_prescribing_outlier",
  "services_per_beneficiary_outlier",
  "name_resolved_excluded_provider_billing",
  "excluded_provider_received_open_payments",
] as const;

const VALID_KINDS: ReadonlySet<EntityKind> = new Set([
  "candidate",
  "committee",
  "treasurer",
  "donor",
  "donor_cluster",
  "contractor",
  "address",
  "nj_state_candidate",
  "provider",
  "employer",
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
 * Publicly-announced NJ statewide candidates for a given election year
 * (defaults to the most-recent year present in ref.nj_state_candidate).
 * Reads from derived.v_nj_state_candidates, which exposes the
 * campaign_finance_ingest_pending flag the UI renders as a badge.
 *
 * The platform makes NO contribution / expenditure / anomaly-signal
 * claims about these entities -- the NJ ELEC ingester has not shipped,
 * so the badge is TRUE for every row. When the ingester lands and
 * elec_filing_id is populated, the badge flips off automatically and
 * the candidate becomes eligible for donor-graph fraud signals.
 *
 * Substrate: ref.nj_state_candidate (mig 093) seeded by 022.
 */
export async function getNjStateCandidates(
  opts: { electionYear?: number; office?: string } = {},
): Promise<NjStateCandidate[]> {
  const sql = getSql();
  const electionYear = opts.electionYear ?? null;
  const office = opts.office ?? null;
  const rows = (await sql`
    SELECT
      entity_id,
      full_name,
      party,
      office,
      office_label,
      election_year::INT     AS election_year,
      primary_date::TEXT     AS primary_date,
      general_date::TEXT     AS general_date,
      announced_candidate,
      announcement_date::TEXT AS announcement_date,
      announcement_url,
      prior_office,
      campaign_committee_name,
      campaign_finance_ingest_pending,
      primary_winner,
      primary_result_url,
      general_winner,
      general_result_url,
      source_url,
      source_authority,
      source_doc_date::TEXT  AS source_doc_date,
      notes
    FROM derived.v_nj_state_candidates
    WHERE (${electionYear}::INT  IS NULL OR election_year = ${electionYear}::INT)
      AND (${office}::TEXT       IS NULL OR office        = ${office}::TEXT)
  `) as Record<string, unknown>[];

  return rows.map((r) => ({
    entity_id: String(r.entity_id),
    full_name: String(r.full_name),
    party: String(r.party),
    office: String(r.office),
    office_label: String(r.office_label),
    election_year: Number(r.election_year),
    primary_date: r.primary_date == null ? null : String(r.primary_date),
    general_date: r.general_date == null ? null : String(r.general_date),
    announced_candidate: Boolean(r.announced_candidate),
    announcement_date:
      r.announcement_date == null ? null : String(r.announcement_date),
    announcement_url:
      r.announcement_url == null ? null : String(r.announcement_url),
    prior_office: r.prior_office == null ? null : String(r.prior_office),
    campaign_committee_name:
      r.campaign_committee_name == null
        ? null
        : String(r.campaign_committee_name),
    campaign_finance_ingest_pending: Boolean(
      r.campaign_finance_ingest_pending,
    ),
    primary_winner:
      r.primary_winner == null ? null : Boolean(r.primary_winner),
    primary_result_url:
      r.primary_result_url == null ? null : String(r.primary_result_url),
    general_winner:
      r.general_winner == null ? null : Boolean(r.general_winner),
    general_result_url:
      r.general_result_url == null ? null : String(r.general_result_url),
    source_url: String(r.source_url),
    source_authority: String(r.source_authority),
    source_doc_date: String(r.source_doc_date),
    notes: r.notes == null ? null : String(r.notes),
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
 * State-wide NJ civic-integrity summary for a given FEC cycle. Powers
 * the cross-pillar callout on /housing/[id]. Returns null if the view
 * is missing (mig 091 not applied) or if the cycle has no NJ-keyed FEC
 * data; the housing page treats nulls as "skip the callout" rather
 * than rendering an empty box.
 */
export async function getNjCivicIntegritySummary(
  cycle: string,
): Promise<NjCivicIntegritySummary | null> {
  const sql = getSql();
  try {
    const rows = (await sql`
      SELECT
        cycle,
        n_candidates_total,
        n_candidates_with_signals,
        max_candidate_risk_score::FLOAT8 AS max_candidate_risk_score,
        n_committees_total,
        n_committees_with_signals,
        max_committee_risk_score::FLOAT8 AS max_committee_risk_score,
        n_addresses_with_signals,
        max_address_risk_score::FLOAT8 AS max_address_risk_score,
        total_nj_entities_with_signals,
        max_nj_risk_score::FLOAT8 AS max_nj_risk_score
      FROM derived.v_nj_civic_integrity_state_summary
      WHERE cycle = ${cycle}
    `) as Record<string, unknown>[];
    if (rows.length === 0) return null;
    const r = rows[0];
    return {
      cycle: String(r.cycle),
      n_candidates_total: Number(r.n_candidates_total ?? 0),
      n_candidates_with_signals: Number(r.n_candidates_with_signals ?? 0),
      max_candidate_risk_score: Number(r.max_candidate_risk_score ?? 0),
      n_committees_total: Number(r.n_committees_total ?? 0),
      n_committees_with_signals: Number(r.n_committees_with_signals ?? 0),
      max_committee_risk_score: Number(r.max_committee_risk_score ?? 0),
      n_addresses_with_signals: Number(r.n_addresses_with_signals ?? 0),
      max_address_risk_score: Number(r.max_address_risk_score ?? 0),
      total_nj_entities_with_signals: Number(
        r.total_nj_entities_with_signals ?? 0,
      ),
      max_nj_risk_score: Number(r.max_nj_risk_score ?? 0),
    };
  } catch {
    return null;
  }
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

  if (kind === "nj_state_candidate") {
    // Query derived.v_nj_state_candidates (NOT ref.nj_state_candidate)
    // because the view: (a) aliases candidate_id -> entity_id to match the
    // universal entity-key contract that v_entity_fraud_evidence + the
    // /risk URL space depend on; (b) synthesizes office_label as a human-
    // readable string from the office enum (governor / lt_governor / ...).
    // The /risk URL space uses cycle = election_year::text (mig 098).
    // is_nj is unconditionally TRUE because ref.nj_state_candidate is by
    // construction NJ state-level; the L3 evidence view uses the same
    // constant for the firing case.
    const rows = (await sql`
      SELECT
        ${cycle}::CHAR(4)        AS cycle,
        entity_id,
        full_name                AS display_name,
        party                    AS office_party,
        office_label
      FROM derived.v_nj_state_candidates
      WHERE entity_id = ${id}
        AND election_year::text = ${cycle}
      LIMIT 1
    `) as Record<string, unknown>[];
    if (rows.length === 0) return null;
    const r = rows[0];
    return {
      cycle: String(r.cycle),
      entity_kind: "nj_state_candidate",
      entity_id: String(r.entity_id),
      display_name: r.display_name == null ? null : String(r.display_name),
      is_nj: true,
      office_code: r.office_label == null ? null : String(r.office_label),
      office_state: "NJ",
      office_district: null,
      office_party: r.office_party == null ? null : String(r.office_party),
      office_incumbent_status: null,
    };
  }

  if (kind === "provider") {
    // NPI-keyed healthcare provider (entity_id = the 10-digit NPI). The
    // /risk URL space uses cycle = the CMS data_year::text (mig 100/101).
    // Resolve the human-readable name + practice state from the CMS Part D
    // prescriber substrate; display_name falls back to the NPI when the
    // provider has no Part D row for that year. is_nj is derived from the
    // prescriber's practice state, NOT a constant (a provider can be billing
    // Medicare from anywhere), matching the L3 evidence view's CASE branch.
    const rows = (await sql`
      SELECT
        ${cycle}::CHAR(4)                                      AS cycle,
        npi                                                    AS entity_id,
        NULLIF(TRIM(
          COALESCE(prscrbr_first_name, '') || ' ' ||
          COALESCE(prscrbr_last_org_name, '')
        ), '')                                                 AS display_name,
        (prscrbr_state_abrvtn = 'NJ')                          AS is_nj,
        prscrbr_type                                           AS prscrbr_type
      FROM raw.cms_partd_prescriber
      WHERE npi = ${id}
        AND data_year::text = ${cycle}
      LIMIT 1
    `) as Record<string, unknown>[];
    if (rows.length === 0) return null;
    const r = rows[0];
    return {
      cycle: String(r.cycle),
      entity_kind: "provider",
      entity_id: String(r.entity_id),
      display_name: r.display_name == null ? null : String(r.display_name),
      is_nj: r.is_nj === true,
      office_code: r.prscrbr_type == null ? null : String(r.prscrbr_type),
      office_state: r.is_nj === true ? "NJ" : null,
      office_district: null,
      office_party: null,
      office_incumbent_status: null,
    };
  }

  if (kind === "employer") {
    const rows = (await sql`
      SELECT
        ${cycle}::CHAR(4) AS cycle,
        employer_canonical_name AS entity_id,
        employer_name AS display_name,
        TRUE AS is_nj
      FROM derived.v_lca_nj_h1b
      WHERE employer_canonical_name = ${id}
        AND fiscal_year::text = ${cycle}
      LIMIT 1
    `) as Record<string, unknown>[];
    if (rows.length === 0) {
      const uscis = (await sql`
        SELECT
          ${cycle}::CHAR(4) AS cycle,
          employer_canonical_name AS entity_id,
          employer_name AS display_name,
          (UPPER(TRIM(petitioner_state)) = 'NJ') AS is_nj
        FROM raw.uscis_h1b_employer
        WHERE employer_canonical_name = ${id}
          AND fiscal_year::text = ${cycle}
        LIMIT 1
      `) as Record<string, unknown>[];
      if (uscis.length === 0) return null;
      const u = uscis[0];
      return {
        cycle: String(u.cycle),
        entity_kind: "employer",
        entity_id: String(u.entity_id),
        display_name: u.display_name == null ? null : String(u.display_name),
        is_nj: u.is_nj === true,
        office_code: "H-1B petitioner",
        office_state: u.is_nj === true ? "NJ" : null,
        office_district: null,
        office_party: null,
        office_incumbent_status: null,
      };
    }
    const r = rows[0];
    return {
      cycle: String(r.cycle),
      entity_kind: "employer",
      entity_id: String(r.entity_id),
      display_name: r.display_name == null ? null : String(r.display_name),
      is_nj: true,
      office_code: "H-1B employer",
      office_state: "NJ",
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

/* ================================================================== */
/*  Healthcare-provider fraud (FRAUD-F7) — powers /fraud              */
/* ================================================================== */

/**
 * Top flagged healthcare providers across ALL provider cycles (provider
 * cycle = CMS data_year, which differs from the FEC cycle, so this is
 * intentionally NOT cycle-filtered). One row per NPI (worst observation
 * wins), ordered by composite risk score. Reads v_entity_fraud_evidence
 * for display/preview and v_entity_fraud_risk for the score. Returns []
 * cleanly when no provider data is loaded.
 */
export async function listTopProviderRisk(opts: {
  limit?: number;
}): Promise<ProviderRiskCard[]> {
  const sql = getSql();
  const limit = Math.min(Math.max(opts.limit ?? 25, 1), 100);
  // PERFORMANCE: drive from the risk view (fast: WHERE entity_kind +
  // LIMIT), then resolve preview/identity for ONLY the top-N from the
  // real observation table + indexed CMS PK lookups + ref tables. Joining
  // the aggregating v_entity_fraud_risk view to the full evidence view
  // (whose unfiltered provider_meta UNION scans ~74k roster rows) made the
  // planner re-evaluate it per row -> ~36s. This shape is <0.15s. The
  // plain-English explanation is token-substituted inline (same REPLACE
  // chain as v_entity_fraud_evidence) on the ~25 surviving rows.
  const rows = (await sql`
    WITH top AS (
      SELECT cycle, entity_id, risk_score, n_signals_fired
      FROM derived.v_entity_fraud_risk
      WHERE entity_kind = 'provider'
      ORDER BY risk_score DESC, entity_id ASC
      LIMIT ${limit}
    ),
    prev AS (
      SELECT DISTINCT ON (o.entity_id)
        o.entity_id, o.cycle, o.signal_id, o.severity,
        o.peer_bucket, o.peer_percentile, o.raw_value
      FROM derived.fraud_signal_observation o
      JOIN top t ON t.entity_id = o.entity_id AND t.cycle = o.cycle
      WHERE o.entity_kind = 'provider'
      ORDER BY o.entity_id,
               o.severity DESC NULLS LAST,
               o.peer_percentile DESC NULLS LAST
    )
    SELECT
      t.cycle,
      t.entity_id,
      COALESCE(pd.nm, pb.nm)                        AS display_name,
      COALESCE(pd.is_nj, pb.is_nj, FALSE)          AS is_nj,
      p.signal_id                                  AS preview_signal_id,
      p.severity::INT                              AS preview_severity,
      p.peer_percentile::FLOAT8                    AS preview_peer_percentile,
      p.raw_value::FLOAT8                          AS preview_raw_value,
      he.citation_authority                        AS preview_citation_authority,
      REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
        COALESCE(he.plain_english_template, ''),
        '{{entity_id}}',       t.entity_id),
        '{{cycle}}',           t.cycle),
        '{{raw_value}}',       COALESCE(p.raw_value::TEXT, '')),
        '{{peer_percentile}}', COALESCE(ROUND(p.peer_percentile * 100, 1)::TEXT, '')),
        '{{entity_kind}}',     'provider'),
        '{{peer_bucket}}',     COALESCE(p.peer_bucket, ''))
                                                   AS preview_explanation,
      t.risk_score::FLOAT8                         AS risk_score,
      t.n_signals_fired::INT                       AS n_signals
    FROM top t
    LEFT JOIN prev p
      ON p.entity_id = t.entity_id AND p.cycle = t.cycle
    LEFT JOIN LATERAL (
      SELECT
        NULLIF(TRIM(COALESCE(prscrbr_first_name, '') || ' ' ||
                    COALESCE(prscrbr_last_org_name, '')), '') AS nm,
        (prscrbr_state_abrvtn = 'NJ')                          AS is_nj
      FROM raw.cms_partd_prescriber
      WHERE npi = t.entity_id AND data_year = t.cycle::INT
      LIMIT 1
    ) pd ON TRUE
    LEFT JOIN LATERAL (
      SELECT
        NULLIF(TRIM(COALESCE(prvdr_first_name, '') || ' ' ||
                    COALESCE(prvdr_last_org_name, '')), '')    AS nm,
        (prvdr_state_abrvtn = 'NJ')                            AS is_nj
      FROM raw.cms_physician_provider
      WHERE npi = t.entity_id AND data_year = t.cycle::INT
      LIMIT 1
    ) pb ON TRUE
    LEFT JOIN ref.fraud_signal_human_explanation he
      ON he.signal_id = p.signal_id
    ORDER BY t.risk_score DESC, t.entity_id ASC
  `) as Record<string, unknown>[];

  return rows.map((r) => ({
    cycle: String(r.cycle),
    entity_id: String(r.entity_id),
    display_name: r.display_name == null ? null : String(r.display_name),
    is_nj: Boolean(r.is_nj),
    risk_score: Number(r.risk_score),
    n_signals: Number(r.n_signals),
    preview_signal_id: String(r.preview_signal_id),
    preview_severity: Number(r.preview_severity),
    preview_peer_percentile:
      r.preview_peer_percentile == null
        ? null
        : Number(r.preview_peer_percentile),
    preview_explanation: String(r.preview_explanation ?? ""),
    preview_citation_authority:
      r.preview_citation_authority == null
        ? null
        : String(r.preview_citation_authority),
    preview_raw_value:
      r.preview_raw_value == null ? null : Number(r.preview_raw_value),
  }));
}

/**
 * The healthcare-fraud signal CATALOG: the seven provider-domain signals
 * with their severity, calibration basis, federal/state predicate, and
 * citation. Reads ref.fraud_signal_* (populated by the seeds) so the
 * catalog is real content even before any provider data is loaded.
 */
export async function getHealthcareSignalCatalog(): Promise<
  HealthcareSignalCatalogEntry[]
> {
  const sql = getSql();
  const ids = [...HEALTHCARE_SIGNAL_IDS];
  const rows = (await sql`
    SELECT
      cfg.signal_id,
      cfg.signal_family,
      sc.severity_level::INT                AS severity_level,
      sc.calibration_basis,
      he.rule_text,
      he.citation_authority,
      he.citation_section,
      he.citation_url,
      sc.precedent_summary,
      sc.precedent_url,
      eut.upstream_source
    FROM derived.fraud_signal_config cfg
    LEFT JOIN ref.fraud_signal_severity_calibration sc
      ON sc.signal_id = cfg.signal_id
    LEFT JOIN ref.fraud_signal_human_explanation he
      ON he.signal_id = cfg.signal_id
    LEFT JOIN ref.fraud_signal_evidence_url_template eut
      ON eut.signal_id = cfg.signal_id
    WHERE cfg.signal_id = ANY(${ids})
    ORDER BY sc.severity_level DESC NULLS LAST, cfg.signal_id ASC
  `) as Record<string, unknown>[];

  return rows.map((r) => ({
    signal_id: String(r.signal_id),
    signal_family: String(r.signal_family ?? ""),
    severity_level: Number(r.severity_level ?? 0),
    calibration_basis:
      r.calibration_basis == null ? null : String(r.calibration_basis),
    rule_text: r.rule_text == null ? null : String(r.rule_text),
    citation_authority:
      r.citation_authority == null ? null : String(r.citation_authority),
    citation_section:
      r.citation_section == null ? null : String(r.citation_section),
    citation_url: r.citation_url == null ? null : String(r.citation_url),
    precedent_summary:
      r.precedent_summary == null ? null : String(r.precedent_summary),
    precedent_url:
      r.precedent_url == null ? null : String(r.precedent_url),
    upstream_source:
      r.upstream_source == null ? null : String(r.upstream_source),
  }));
}

/**
 * Substrate-honesty status for /fraud: row counts of the loaded CMS/NPPES
 * tables + the count of provider observations the engine has emitted.
 * to_regclass guards keep this from throwing if a table is somehow absent
 * (partial deploy); a hard failure degrades to all-zeros in the caller.
 */
export async function getHealthcareSubstrateStatus(): Promise<HealthcareSubstrateStatus> {
  const sql = getSql();
  const rows = (await sql`
    SELECT
      (SELECT COUNT(*) FROM raw.cms_partd_prescriber)::INT      AS n_partd,
      (SELECT COUNT(*) FROM raw.cms_physician_provider)::INT    AS n_physician,
      (SELECT COUNT(*) FROM raw.cms_open_payments_general)::INT AS n_open_payments,
      (SELECT COUNT(*) FROM raw.nj_medicaid_exclusion)::INT     AS n_nj_med,
      (SELECT COUNT(*) FROM raw.nppes_provider)::INT            AS n_nppes,
      (SELECT COUNT(*) FROM raw.hhs_oig_leie)::INT              AS n_leie,
      (SELECT COUNT(*) FROM derived.fraud_signal_observation
         WHERE entity_kind = 'provider')::INT                   AS n_obs
  `) as Record<string, unknown>[];
  const r = rows[0] ?? {};
  return {
    n_partd_prescriber: Number(r.n_partd ?? 0),
    n_physician_provider: Number(r.n_physician ?? 0),
    n_open_payments: Number(r.n_open_payments ?? 0),
    n_nj_medicaid_exclusion: Number(r.n_nj_med ?? 0),
    n_nppes_provider: Number(r.n_nppes ?? 0),
    n_leie: Number(r.n_leie ?? 0),
    n_provider_observations: Number(r.n_obs ?? 0),
  };
}

/**
 * The /leads "highest-value fraud" queue. Reads the pre-ranked
 * derived.v_high_value_leads (ordered by lead_rank: reportability reward
 * tier, then measured USD exposure, then cross-cycle prior-sanction
 * recurrence, then multi-source breadth — all grounded, no magic score),
 * then resolves a display name + NJ flag for ONLY the top-N rows via indexed
 * per-kind lookups (provider→CMS, candidate/committee→FEC master). The whole
 * view materializes off small tables in <0.4s; name resolution touches ~N
 * rows. Degrades to [] on any failure so a fresh deploy serves an empty
 * queue, not a 500.
 */
export async function listHighValueLeads(opts: {
  limit?: number;
  /**
   * Enforcement-status lane:
   *   false ⇒ UNDETECTED (no prior exclusion/debarment) — the prospective queue.
   *   true  ⇒ ALREADY-CAUGHT (on an exclusion/debarment list) — demoted lane.
   *   undefined ⇒ both, in rank order (undetected first).
   */
  priorEnforcement?: boolean;
}): Promise<HighValueLead[]> {
  const sql = getSql();
  const limit = Math.min(Math.max(opts.limit ?? 50, 1), 200);
  // Single parameterized predicate (the Neon HTTP driver does NOT support
  // nesting sql`` fragments): NULL ⇒ both lanes, else filter by status.
  const priorFilter = opts.priorEnforcement ?? null;
  const rows = (await sql`
    WITH lead AS (
      SELECT * FROM derived.v_high_value_leads
      WHERE (${priorFilter}::boolean IS NULL
             OR has_prior_sanction = ${priorFilter}::boolean)
      ORDER BY lead_rank
      LIMIT ${limit}
    )
    SELECT
      l.lead_rank::INT                               AS lead_rank,
      l.entity_kind,
      l.entity_id,
      COALESCE(
        pd.nm, pb.nm, cand.cand_name, cmte.cmte_nm,
        CASE l.entity_kind
          WHEN 'treasurer' THEN l.entity_id
          WHEN 'address'   THEN SPLIT_PART(l.entity_id, '|', 1)
          ELSE NULL
        END
      )                                              AS display_name,
      COALESCE(pd.is_nj, pb.is_nj, cand.is_nj, cmte.is_nj, FALSE) AS is_nj,
      l.latest_cycle,
      l.n_cycles::INT                                AS n_cycles,
      l.n_signals::INT                               AS n_signals,
      l.n_families::INT                              AS n_families,
      l.max_severity::INT                            AS max_severity,
      l.best_reward_tier::INT                        AS best_reward_tier,
      l.reward_eligible,
      l.has_prior_sanction                           AS prior_enforcement,
      l.repeat_violator,
      l.multi_source,
      l.provider_scale_usd::FLOAT8                   AS provider_scale_usd,
      l.peak_exposure_usd::FLOAT8                    AS peak_exposure_usd,
      l.total_exposure_usd::FLOAT8                   AS total_exposure_usd,
      l.reward_low_usd::FLOAT8                       AS reward_low_usd,
      l.reward_high_usd::FLOAT8                      AS reward_high_usd,
      l.driver_signal_id,
      l.driver_signal_family,
      l.recovery_program,
      l.recovery_channel,
      l.recovery_channel_url,
      l.statute_citation,
      l.statute_url
    FROM lead l
    LEFT JOIN LATERAL (
      SELECT
        NULLIF(TRIM(COALESCE(prscrbr_first_name, '') || ' ' ||
                    COALESCE(prscrbr_last_org_name, '')), '') AS nm,
        (prscrbr_state_abrvtn = 'NJ')                          AS is_nj
      FROM raw.cms_partd_prescriber
      WHERE l.entity_kind = 'provider' AND npi = l.entity_id
      ORDER BY data_year DESC
      LIMIT 1
    ) pd ON TRUE
    LEFT JOIN LATERAL (
      SELECT
        NULLIF(TRIM(COALESCE(prvdr_first_name, '') || ' ' ||
                    COALESCE(prvdr_last_org_name, '')), '')    AS nm,
        (prvdr_state_abrvtn = 'NJ')                            AS is_nj
      FROM raw.cms_physician_provider
      WHERE l.entity_kind = 'provider' AND npi = l.entity_id
      ORDER BY data_year DESC
      LIMIT 1
    ) pb ON TRUE
    LEFT JOIN LATERAL (
      SELECT cand_name, (cand_office_st = 'NJ') AS is_nj
      FROM raw.fec_candidate
      WHERE l.entity_kind = 'candidate' AND cand_id = l.entity_id
      ORDER BY cycle DESC
      LIMIT 1
    ) cand ON TRUE
    LEFT JOIN LATERAL (
      SELECT cmte_nm, (cmte_st = 'NJ') AS is_nj
      FROM raw.fec_committee
      WHERE l.entity_kind = 'committee' AND cmte_id = l.entity_id
      ORDER BY cycle DESC
      LIMIT 1
    ) cmte ON TRUE
    ORDER BY l.lead_rank
  `) as Record<string, unknown>[];

  return rows.map((r) => ({
    lead_rank: Number(r.lead_rank),
    entity_kind: r.entity_kind as EntityKind,
    entity_id: String(r.entity_id),
    display_name: r.display_name == null ? null : String(r.display_name),
    is_nj: Boolean(r.is_nj),
    latest_cycle: String(r.latest_cycle),
    n_cycles: Number(r.n_cycles),
    n_signals: Number(r.n_signals),
    n_families: Number(r.n_families),
    max_severity: Number(r.max_severity),
    best_reward_tier: Number(r.best_reward_tier),
    reward_eligible: Boolean(r.reward_eligible),
    prior_enforcement: Boolean(r.prior_enforcement),
    repeat_violator: Boolean(r.repeat_violator),
    multi_source: Boolean(r.multi_source),
    provider_scale_usd:
      r.provider_scale_usd == null ? null : Number(r.provider_scale_usd),
    peak_exposure_usd:
      r.peak_exposure_usd == null ? null : Number(r.peak_exposure_usd),
    total_exposure_usd:
      r.total_exposure_usd == null ? null : Number(r.total_exposure_usd),
    reward_low_usd: r.reward_low_usd == null ? null : Number(r.reward_low_usd),
    reward_high_usd:
      r.reward_high_usd == null ? null : Number(r.reward_high_usd),
    driver_signal_id: String(r.driver_signal_id),
    driver_signal_family: String(r.driver_signal_family ?? ""),
    recovery_program: String(r.recovery_program ?? ""),
    recovery_channel: String(r.recovery_channel ?? ""),
    recovery_channel_url: String(r.recovery_channel_url ?? ""),
    statute_citation: String(r.statute_citation ?? ""),
    statute_url: String(r.statute_url ?? ""),
  }));
}

/**
 * Provenance + population totals for a served leads snapshot
 * (derived.leads_snapshot_meta). Returns null when no snapshot exists for the
 * scope (table empty or absent) — the caller then falls back to the live view.
 * Guarded so a serving DB without the snapshot migrations degrades gracefully.
 */
export async function getLeadsSnapshotMeta(
  scope: "national" | "nj" = "national",
): Promise<LeadsSnapshotMeta | null> {
  const sql = getSql();
  let rows: Record<string, unknown>[];
  try {
    rows = (await sql`
      SELECT source_scope, formula_version, source_vintage_hash,
             snapshot_at, n_total, n_undetected, n_already_caught,
             n_multi_source, n_repeat_violators, n_reward_eligible,
             max_undetected_scale_usd::FLOAT8 AS max_undetected_scale_usd,
             max_exposure_usd::FLOAT8         AS max_exposure_usd,
             total_reward_eligible_exposure_usd::FLOAT8
               AS total_reward_eligible_exposure_usd,
             count_by_tier, n_shown_undetected, n_shown_caught
      FROM derived.leads_snapshot_meta
      WHERE source_scope = ${scope}
      LIMIT 1
    `) as Record<string, unknown>[];
  } catch {
    return null; // snapshot migrations not applied on this DB
  }
  const r = rows[0];
  if (!r) return null;
  return {
    source_scope: String(r.source_scope) as "national" | "nj",
    formula_version: String(r.formula_version),
    source_vintage_hash: String(r.source_vintage_hash),
    snapshot_at: String(r.snapshot_at),
    n_total: Number(r.n_total ?? 0),
    n_undetected: Number(r.n_undetected ?? 0),
    n_already_caught: Number(r.n_already_caught ?? 0),
    n_multi_source: Number(r.n_multi_source ?? 0),
    n_repeat_violators: Number(r.n_repeat_violators ?? 0),
    n_reward_eligible: Number(r.n_reward_eligible ?? 0),
    max_undetected_scale_usd:
      r.max_undetected_scale_usd == null
        ? null
        : Number(r.max_undetected_scale_usd),
    max_exposure_usd:
      r.max_exposure_usd == null ? null : Number(r.max_exposure_usd),
    total_reward_eligible_exposure_usd:
      r.total_reward_eligible_exposure_usd == null
        ? null
        : Number(r.total_reward_eligible_exposure_usd),
    count_by_tier: (r.count_by_tier ?? {}) as Record<string, number>,
    n_shown_undetected: Number(r.n_shown_undetected ?? 0),
    n_shown_caught: Number(r.n_shown_caught ?? 0),
  };
}

/**
 * Serve high-value leads from the pre-resolved snapshot cache
 * (derived.high_value_leads_snapshot) instead of the live view. Used when a
 * national snapshot is present, so a free-tier serving DB can show national
 * leads it could not compute locally. Same shape as listHighValueLeads.
 */
export async function listHighValueLeadsFromSnapshot(opts: {
  scope?: "national" | "nj";
  limit?: number;
  priorEnforcement?: boolean;
}): Promise<HighValueLead[]> {
  const sql = getSql();
  const scope = opts.scope ?? "national";
  const limit = Math.min(Math.max(opts.limit ?? 50, 1), 200);
  const priorFilter = opts.priorEnforcement ?? null;
  const rows = (await sql`
    SELECT
      lead_rank::INT                 AS lead_rank,
      entity_kind, entity_id, display_name, provider_state, is_nj,
      latest_cycle,
      n_cycles::INT                  AS n_cycles,
      n_signals::INT                 AS n_signals,
      n_families::INT                AS n_families,
      max_severity::INT              AS max_severity,
      best_reward_tier::INT          AS best_reward_tier,
      reward_eligible,
      has_prior_sanction             AS prior_enforcement,
      repeat_violator, multi_source,
      provider_scale_usd::FLOAT8     AS provider_scale_usd,
      peak_exposure_usd::FLOAT8      AS peak_exposure_usd,
      total_exposure_usd::FLOAT8     AS total_exposure_usd,
      reward_low_usd::FLOAT8         AS reward_low_usd,
      reward_high_usd::FLOAT8        AS reward_high_usd,
      driver_signal_id, driver_signal_family,
      recovery_program, recovery_channel, recovery_channel_url,
      statute_citation, statute_url
    FROM derived.high_value_leads_snapshot
    WHERE source_scope = ${scope}
      AND (${priorFilter}::boolean IS NULL
           OR has_prior_sanction = ${priorFilter}::boolean)
    ORDER BY lead_rank
    LIMIT ${limit}
  `) as Record<string, unknown>[];

  return rows.map((r) => ({
    lead_rank: Number(r.lead_rank),
    entity_kind: r.entity_kind as EntityKind,
    entity_id: String(r.entity_id),
    display_name: r.display_name == null ? null : String(r.display_name),
    provider_state: r.provider_state == null ? null : String(r.provider_state),
    is_nj: Boolean(r.is_nj),
    latest_cycle: String(r.latest_cycle),
    n_cycles: Number(r.n_cycles),
    n_signals: Number(r.n_signals),
    n_families: Number(r.n_families),
    max_severity: Number(r.max_severity),
    best_reward_tier: Number(r.best_reward_tier),
    reward_eligible: Boolean(r.reward_eligible),
    prior_enforcement: Boolean(r.prior_enforcement),
    repeat_violator: Boolean(r.repeat_violator),
    multi_source: Boolean(r.multi_source),
    provider_scale_usd:
      r.provider_scale_usd == null ? null : Number(r.provider_scale_usd),
    peak_exposure_usd:
      r.peak_exposure_usd == null ? null : Number(r.peak_exposure_usd),
    total_exposure_usd:
      r.total_exposure_usd == null ? null : Number(r.total_exposure_usd),
    reward_low_usd: r.reward_low_usd == null ? null : Number(r.reward_low_usd),
    reward_high_usd:
      r.reward_high_usd == null ? null : Number(r.reward_high_usd),
    driver_signal_id: String(r.driver_signal_id),
    driver_signal_family: String(r.driver_signal_family ?? ""),
    recovery_program: String(r.recovery_program ?? ""),
    recovery_channel: String(r.recovery_channel ?? ""),
    recovery_channel_url: String(r.recovery_channel_url ?? ""),
    statute_citation: String(r.statute_citation ?? ""),
    statute_url: String(r.statute_url ?? ""),
  }));
}

/**
 * Aggregate framing for the /leads page header: tier counts, headline
 * totals, and the count of itemized FEC contributions loaded (0 ⇒ the
 * political-flow / 501c4-527 lane is dormant, which the page states plainly
 * rather than implying a ranking it cannot produce).
 */
export async function getHighValueLeadsSummary(): Promise<HighValueLeadsSummary> {
  const sql = getSql();
  const rows = (await sql`
    SELECT
      best_reward_tier::INT                                       AS tier,
      COUNT(*)::INT                                               AS n,
      COUNT(*) FILTER (WHERE reward_eligible)::INT                AS n_reward,
      COUNT(*) FILTER (WHERE repeat_violator)::INT               AS n_repeat,
      COUNT(*) FILTER (WHERE multi_source)::INT                  AS n_multi,
      MAX(peak_exposure_usd)::FLOAT8                              AS max_exposure,
      SUM(peak_exposure_usd) FILTER (WHERE reward_eligible)::FLOAT8 AS reward_exposure
    FROM derived.v_high_value_leads
    GROUP BY ROLLUP (best_reward_tier)
  `) as Record<string, unknown>[];

  // Enforcement-status split: the headline reframe. Undetected = no prior
  // exclusion/debarment (the prospective queue); already-caught = on a list.
  const statusRows = (await sql`
    SELECT
      COUNT(*) FILTER (WHERE NOT has_prior_sanction)::INT         AS n_undetected,
      COUNT(*) FILTER (WHERE has_prior_sanction)::INT             AS n_caught,
      MAX(COALESCE(peak_exposure_usd, provider_scale_usd))
        FILTER (WHERE NOT has_prior_sanction)::FLOAT8             AS max_undetected_scale
    FROM derived.v_high_value_leads
  `) as Record<string, unknown>[];

  // FEC itemized-contribution substrate (the political-flow lane); a guarded
  // count so a missing table degrades to 0 rather than throwing.
  const fecRows = (await sql`
    SELECT COALESCE(
      (SELECT COUNT(*) FROM raw.fec_contribution), 0
    )::INT AS n_fec
  `) as Record<string, unknown>[];

  const count_by_tier: Record<string, number> = {};
  let n_total = 0;
  let n_reward_eligible = 0;
  let n_repeat_violators = 0;
  let n_multi_source = 0;
  let max_exposure_usd: number | null = null;
  let total_reward_eligible_exposure_usd: number | null = null;

  for (const r of rows) {
    if (r.tier == null) {
      // The ROLLUP grand-total row.
      n_total = Number(r.n ?? 0);
      n_reward_eligible = Number(r.n_reward ?? 0);
      n_repeat_violators = Number(r.n_repeat ?? 0);
      n_multi_source = Number(r.n_multi ?? 0);
      max_exposure_usd = r.max_exposure == null ? null : Number(r.max_exposure);
      total_reward_eligible_exposure_usd =
        r.reward_exposure == null ? null : Number(r.reward_exposure);
    } else {
      count_by_tier[String(r.tier)] = Number(r.n ?? 0);
    }
  }

  const sr = statusRows[0] ?? {};

  return {
    count_by_tier,
    n_total,
    n_reward_eligible,
    n_repeat_violators,
    n_multi_source,
    max_exposure_usd,
    total_reward_eligible_exposure_usd,
    n_fec_contribution: Number(fecRows[0]?.n_fec ?? 0),
    n_undetected: Number(sr.n_undetected ?? 0),
    n_already_caught: Number(sr.n_caught ?? 0),
    max_undetected_scale_usd:
      sr.max_undetected_scale == null ? null : Number(sr.max_undetected_scale),
  };
}

/**
 * Detector validation harness (derived.v_signal_validation, migration 117).
 * Returns one row per (cycle, behavioral signal): precision / base-rate / lift
 * of the anomaly detector measured against the prior-sanction ground-truth set,
 * with a Wilson lower bound and the raw counts. Surfaced so the platform is
 * honest about how well its anomaly detectors actually predict known fraud.
 * Most-recent cycle first, then by lift (nulls last).
 */
export async function getSignalValidation(): Promise<SignalValidationRow[]> {
  const sql = getSql();
  const rows = (await sql`
    SELECT
      cycle,
      signal_id,
      signal_family,
      n_universe::INT          AS n_universe,
      n_positives::INT         AS n_positives,
      n_flagged::INT           AS n_flagged,
      n_true_positive::INT     AS n_true_positive,
      base_rate::FLOAT8        AS base_rate,
      precision::FLOAT8        AS precision,
      lift::FLOAT8             AS lift,
      precision_wilson_lo95::FLOAT8 AS precision_wilson_lo95
    FROM derived.v_signal_validation
    ORDER BY cycle DESC, lift DESC NULLS LAST, signal_id
  `) as Record<string, unknown>[];

  return rows.map((r) => ({
    cycle: String(r.cycle),
    signal_id: String(r.signal_id),
    signal_family: String(r.signal_family),
    n_universe: Number(r.n_universe ?? 0),
    n_positives: Number(r.n_positives ?? 0),
    n_flagged: Number(r.n_flagged ?? 0),
    n_true_positive: Number(r.n_true_positive ?? 0),
    base_rate: r.base_rate == null ? null : Number(r.base_rate),
    precision: r.precision == null ? null : Number(r.precision),
    lift: r.lift == null ? null : Number(r.lift),
    precision_wilson_lo95:
      r.precision_wilson_lo95 == null ? null : Number(r.precision_wilson_lo95),
  }));
}

export const H1B_SIGNAL_IDS = [
  "employer_below_prevailing_wage",
  "employer_h1b_denial_rate_outlier",
  "employer_lca_uscis_volume_gap",
  "employer_certified_withdrawn_rate_outlier",
  "employer_on_whd_willful_or_debarred",
  "employer_level1_wage_share_outlier",
  "employer_secondary_entity_share_outlier",
  "employer_h1b_dependent_plus_anomaly",
] as const;

export async function listH1bEmployerLeads(opts: {
  cycle?: string;
  limit?: number;
}): Promise<H1bEmployerLead[]> {
  const sql = getSql();
  const limit = opts.limit ?? 80;
  try {
    const rows = opts.cycle
      ? ((await sql`
          SELECT
            cycle, entity_id, display_name, is_nj,
            risk_score::FLOAT8 AS risk_score,
            n_signals, max_severity,
            below_pw_gap_usd::FLOAT8 AS below_pw_gap_usd,
            denial_rate::FLOAT8 AS denial_rate,
            lca_uscis_gap_ratio::FLOAT8 AS lca_uscis_gap_ratio,
            certified_withdrawn_rate::FLOAT8 AS certified_withdrawn_rate,
            on_whd_list::FLOAT8 AS on_whd_list,
            level1_wage_share::FLOAT8 AS level1_wage_share,
            secondary_entity_share::FLOAT8 AS secondary_entity_share,
            dependent_anomaly_count::FLOAT8 AS dependent_anomaly_count,
            preview_signal_id
          FROM derived.v_h1b_employer_leads
          WHERE cycle = ${opts.cycle}
          ORDER BY max_severity DESC, risk_score DESC NULLS LAST, n_signals DESC
          LIMIT ${limit}
        `) as Record<string, unknown>[])
      : ((await sql`
          SELECT
            cycle, entity_id, display_name, is_nj,
            risk_score::FLOAT8 AS risk_score,
            n_signals, max_severity,
            below_pw_gap_usd::FLOAT8 AS below_pw_gap_usd,
            denial_rate::FLOAT8 AS denial_rate,
            lca_uscis_gap_ratio::FLOAT8 AS lca_uscis_gap_ratio,
            certified_withdrawn_rate::FLOAT8 AS certified_withdrawn_rate,
            on_whd_list::FLOAT8 AS on_whd_list,
            level1_wage_share::FLOAT8 AS level1_wage_share,
            secondary_entity_share::FLOAT8 AS secondary_entity_share,
            dependent_anomaly_count::FLOAT8 AS dependent_anomaly_count,
            preview_signal_id
          FROM derived.v_h1b_employer_leads
          ORDER BY cycle DESC, max_severity DESC, risk_score DESC NULLS LAST
          LIMIT ${limit}
        `) as Record<string, unknown>[]);
    return rows.map((r) => ({
      cycle: String(r.cycle),
      entity_id: String(r.entity_id),
      display_name: r.display_name == null ? null : String(r.display_name),
      is_nj: r.is_nj === true,
      risk_score: r.risk_score == null ? null : Number(r.risk_score),
      n_signals: Number(r.n_signals ?? 0),
      max_severity: Number(r.max_severity ?? 0),
      below_pw_gap_usd:
        r.below_pw_gap_usd == null ? null : Number(r.below_pw_gap_usd),
      denial_rate: r.denial_rate == null ? null : Number(r.denial_rate),
      lca_uscis_gap_ratio:
        r.lca_uscis_gap_ratio == null ? null : Number(r.lca_uscis_gap_ratio),
      certified_withdrawn_rate:
        r.certified_withdrawn_rate == null
          ? null
          : Number(r.certified_withdrawn_rate),
      on_whd_list: r.on_whd_list == null ? null : Number(r.on_whd_list),
      level1_wage_share:
        r.level1_wage_share == null ? null : Number(r.level1_wage_share),
      secondary_entity_share:
        r.secondary_entity_share == null
          ? null
          : Number(r.secondary_entity_share),
      dependent_anomaly_count:
        r.dependent_anomaly_count == null
          ? null
          : Number(r.dependent_anomaly_count),
      preview_signal_id:
        r.preview_signal_id == null ? null : String(r.preview_signal_id),
    }));
  } catch {
    return [];
  }
}

export async function getH1bLaneSummary(): Promise<{
  n_employers: number;
  n_below_pw: number;
  latest_cycle: string | null;
}> {
  const sql = getSql();
  try {
    const rows = (await sql`
      SELECT
        COUNT(DISTINCT entity_id)::INT AS n_employers,
        COUNT(*) FILTER (
          WHERE below_pw_gap_usd IS NOT NULL
        )::INT AS n_below_pw,
        MAX(cycle) AS latest_cycle
      FROM derived.v_h1b_employer_leads
    `) as Record<string, unknown>[];
    const r = rows[0] ?? {};
    return {
      n_employers: Number(r.n_employers ?? 0),
      n_below_pw: Number(r.n_below_pw ?? 0),
      latest_cycle: r.latest_cycle == null ? null : String(r.latest_cycle),
    };
  } catch {
    return { n_employers: 0, n_below_pw: 0, latest_cycle: null };
  }
}
