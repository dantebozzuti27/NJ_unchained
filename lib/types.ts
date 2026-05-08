/**
 * Wire types for the screener.
 *
 * Kept narrow on purpose: each type mirrors EXACTLY the columns the
 * UI consumes, not the L1/L2/L3a database schema. This decouples the
 * UI from backend column-name churn.
 */

export type EntityKind =
  | "candidate"
  | "committee"
  | "treasurer"
  | "donor"
  | "donor_cluster"
  | "contractor"
  | "address";

export interface RiskRow {
  cycle: string;
  entity_kind: EntityKind;
  entity_id: string;
  /** Human-friendly label resolved from FEC tables when available. */
  display_name: string | null;
  /** 0-100, two decimal places. */
  risk_score: number;
  /** Number of distinct fraud_signal families above the 0.95 percentile. */
  n_contributing_families: number;
  /** Comma-separated list of signal family names that fired. */
  signal_families: string[];
  /** Number of signals (any percentile) that fired. */
  n_signals: number;
}

export interface SignalRow {
  signal_id: string;
  signal_family: string | null;
  raw_value: number;
  severity: number;
  peer_bucket: string;
  peer_percentile: number;
  evidence_url: string;
  /** Whether this signal is above the 0.95 contributing-family threshold. */
  is_contributing: boolean;
  /** Per-signal min_actionable_threshold from fraud_signal_config. */
  min_actionable_threshold: number | null;
}

export interface EntityDetail {
  cycle: string;
  entity_kind: EntityKind;
  entity_id: string;
  display_name: string | null;
  risk_score: number;
  n_contributing_families: number;
  signal_families: string[];
  signals: SignalRow[];
}

/**
 * One row of derived.v_entity_fraud_evidence -- a single firing signal
 * for a single entity with the rendered plain-English explanation,
 * federal-authority citation, severity precedent, and upstream-verify
 * URL all assembled at the SQL layer (no TS-side templating).
 */
export interface EvidenceCard {
  cycle: string;
  entity_kind: EntityKind;
  entity_id: string;
  signal_id: string;
  raw_value: number | null;
  severity: number;
  peer_bucket: string | null;
  peer_percentile: number | null;
  is_nj: boolean;
  display_name: string | null;
  /** Office context (candidate-kind only). NULL otherwise. */
  office_code: string | null;
  office_state: string | null;
  office_district: string | null;
  office_party: string | null;
  office_incumbent_status: string | null;
  /** Plain-English explanation, all tokens substituted. */
  rendered_explanation: string;
  rule_text: string | null;
  citation_authority: string | null;
  citation_section: string | null;
  citation_url: string | null;
  severity_basis: string | null;
  severity_precedent_url: string | null;
  severity_precedent_summary: string | null;
  upstream_verify_url: string;
  upstream_verify_label: string | null;
  upstream_source: string | null;
}

/**
 * Bare entity metadata used to render the /risk/[kind]/[id] header when
 * the entity has NO firing signals (clean incumbent, etc.). One row
 * pulled from raw.fec_candidate / raw.fec_committee / decoded directly
 * from entity_id for treasurer + address kinds. The detail page reads
 * this when both v_entity_fraud_risk and v_entity_fraud_evidence are
 * empty for the entity, so the user clicking a green-check incumbent
 * from the roster sees a substrate-honest "no signals firing" page
 * rather than a 404.
 */
export interface EntityHeaderInfo {
  cycle: string;
  entity_kind: EntityKind;
  entity_id: string;
  display_name: string | null;
  is_nj: boolean;
  office_code: string | null;
  office_state: string | null;
  office_district: string | null;
  office_party: string | null;
  office_incumbent_status: string | null;
}

/**
 * Preview card for a single NJ-relevant anomalous entity, used by the
 * /risk overview page Section 2. Aggregates over v_entity_fraud_evidence
 * (filtering is_nj=TRUE), picking the highest-severity firing signal as
 * the preview. The user clicks through to /risk/[kind]/[id] to see all
 * firing signals as full evidence cards.
 */
export interface NjAnomalyCard {
  cycle: string;
  entity_kind: EntityKind;
  entity_id: string;
  display_name: string | null;
  risk_score: number;
  n_signals: number;
  /** Highest-severity firing signal (the "preview" shown on the card). */
  preview_signal_id: string;
  preview_severity: number;
  preview_peer_percentile: number | null;
  preview_explanation: string;
  preview_citation_authority: string | null;
  preview_citation_section: string | null;
  /** Office context (candidate-kind only). NULL otherwise. */
  office_code: string | null;
  office_district: string | null;
  office_party: string | null;
  office_incumbent_status: string | null;
}

/**
 * One row of derived.v_nj_federal_officials -- a sitting NJ federal
 * incumbent (US Senator or US Representative). The /risk overview
 * page renders these as Section 1: a clean roster card grid with
 * green-check (no signals) or red-badge (N signals firing).
 */
export interface NjFederalOfficial {
  cycle: string;
  entity_id: string;
  official_name: string;
  office_code: string;
  office_label: string;
  office_district: string | null;
  office_party: string | null;
  incumbent_status: string;
  election_year: number | null;
  risk_score: number;
  n_signals_fired: number;
  signals_fired: string[];
  max_severity: number;
}

export interface PlatformStatus {
  db_reachable: boolean;
  error?: string;
  cycle_default: string;
  total_entities: number;
  total_signals_fired: number;
  signal_count_by_family: Record<string, number>;
  vintage_iso: string | null;
}
