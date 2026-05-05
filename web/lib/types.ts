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

export interface PlatformStatus {
  db_reachable: boolean;
  error?: string;
  cycle_default: string;
  total_entities: number;
  total_signals_fired: number;
  signal_count_by_family: Record<string, number>;
  vintage_iso: string | null;
}
