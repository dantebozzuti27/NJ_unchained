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
  | "address"
  | "nj_state_candidate"
  | "provider";

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
 * State-wide NJ civic-integrity roll-up for a given FEC cycle. Used by
 * the cross-pillar callout on /housing/[id] so a Pillar-1 housing page
 * surfaces the relevant Pillar-2 context without leaving the page.
 *
 * Per-county granularity is intentionally NOT exposed here: the HUD
 * USPS-County crosswalk that maps committee mailing-address ZIP ->
 * county_fips requires a HUD API key (huduser.gov returns HTTP 202 to
 * anonymous bulk-data requests). Until that crosswalk is loaded into
 * ref.zip_county, we surface state-level aggregates and link out to
 * /risk for drill-down rather than fabricate per-county counts.
 */
export interface NjCivicIntegritySummary {
  cycle: string;
  n_candidates_total: number;
  n_candidates_with_signals: number;
  max_candidate_risk_score: number;
  n_committees_total: number;
  n_committees_with_signals: number;
  max_committee_risk_score: number;
  n_addresses_with_signals: number;
  max_address_risk_score: number;
  total_nj_entities_with_signals: number;
  max_nj_risk_score: number;
}

/**
 * Per-cycle freshness + scope summary for the /risk page header.
 * Lets the user see "FEC data refreshed Nh ago, X candidates, Y
 * committees" and pick a different cycle if available.
 */
export interface CycleSummary {
  cycle: string;
  n_candidates: number;
  n_committees: number;
  /** ISO-8601 timestamp of the most recent ingested_at on raw.fec_*. */
  ingested_at_iso: string | null;
  /** Hours since the most recent ingest. */
  hours_since_ingest: number | null;
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
  /**
   * Number of prior FEC cycles where this exact cand_id ran as a true
   * incumbent (ici='I' AND status='C'). Higher = stronger evidence the
   * candidate really holds the seat. 0 = newcomer / appointee / special-
   * election winner whose incumbency is currently only attested by FEC
   * Form 2 self-declaration; analysts may want to cross-check against
   * clerk.house.gov / senate.gov rosters.
   */
  prior_incumbent_cycles: number;
  risk_score: number;
  n_signals_fired: number;
  signals_fired: string[];
  max_severity: number;
}

/**
 * One row of derived.v_nj_state_candidates -- a manually-curated NJ
 * state-level candidate for a major office (governor, lt_governor,
 * AG, state senate, state assembly). Sourced from ref.nj_state_candidate
 * with substrate-honest citation + provenance metadata.
 *
 * The platform makes NO contribution / expenditure / anomaly-signal
 * claims about these entities until the NJ ELEC ingester ships
 * (campaign_finance_ingest_pending = TRUE for every row today). The
 * UI must surface this gap explicitly.
 */
export interface NjStateCandidate {
  /** PK from ref.nj_state_candidate, format: NJ-STATE-<LAST>-<FIRST>-<YEAR>-<OFFICE>. */
  entity_id: string;
  full_name: string;
  /** DEM / REP / IND / LIB / GRN / CON / OTHER. */
  party: string;
  /** governor / lt_governor / attorney_general / state_senate / state_assembly / state_supreme_court. */
  office: string;
  /** Human-readable office label, e.g. "Governor of New Jersey". */
  office_label: string;
  election_year: number;
  /** ISO date string, e.g. "2025-06-10". */
  primary_date: string | null;
  /** ISO date string, e.g. "2025-11-04". */
  general_date: string | null;
  announced_candidate: boolean;
  /** ISO date string of public-announcement date. */
  announcement_date: string | null;
  announcement_url: string | null;
  /** Held office at time of announcement, e.g. "Mayor of Jersey City (2013-present)". */
  prior_office: string | null;
  campaign_committee_name: string | null;
  /** TRUE = platform has NO ELEC ingest for this candidate. */
  campaign_finance_ingest_pending: boolean;
  /** Certified primary winner; NULL until NJ Division of Elections ingest. */
  primary_winner: boolean | null;
  primary_result_url: string | null;
  general_winner: boolean | null;
  general_result_url: string | null;
  /** Canonical citation URL for the row. */
  source_url: string;
  /** Citation kind, e.g. "Wikipedia, 2025 New Jersey gubernatorial election". */
  source_authority: string;
  /** Date the maintainer last verified source_url. */
  source_doc_date: string;
  notes: string | null;
}

/**
 * One flagged healthcare provider for the /fraud queue. NPI-keyed; the
 * preview is the highest-severity firing signal (mirrors NjAnomalyCard
 * but provider-scoped and cross-cycle — provider cycle = CMS data_year,
 * which differs from the FEC cycle, so the queue is not cycle-filtered).
 */
export interface ProviderRiskCard {
  /** CMS data_year / program_year the observation belongs to. */
  cycle: string;
  /** 10-digit NPI. */
  entity_id: string;
  display_name: string | null;
  is_nj: boolean;
  risk_score: number;
  n_signals: number;
  preview_signal_id: string;
  preview_severity: number;
  preview_peer_percentile: number | null;
  preview_explanation: string;
  preview_citation_authority: string | null;
  /** Dollar exposure carried by the preview signal (Medicare paid, drug cost, transfers of value). */
  preview_raw_value: number | null;
}

/**
 * One entry in the healthcare-fraud SIGNAL CATALOG. Joins
 * derived.fraud_signal_config with the three ref.fraud_signal_* tables.
 * This is reference data that is populated the moment the seeds land —
 * so the catalog renders real, citation-backed content even before any
 * CMS/NPPES provider data is loaded (the queue is empty until then).
 */
export interface HealthcareSignalCatalogEntry {
  signal_id: string;
  signal_family: string;
  severity_level: number;
  calibration_basis: string | null;
  /** The federal/state predicate the signal codifies (human prose). */
  rule_text: string | null;
  citation_authority: string | null;
  citation_section: string | null;
  citation_url: string | null;
  precedent_summary: string | null;
  precedent_url: string | null;
  upstream_source: string | null;
}

/**
 * Substrate-honesty status for the /fraud page: how much CMS/NPPES data
 * is loaded and how many provider observations the engine has emitted.
 * Drives the "engine live, awaiting data" vs "N providers flagged"
 * branch — the platform never pretends to results it doesn't have.
 */
export interface HealthcareSubstrateStatus {
  n_partd_prescriber: number;
  n_physician_provider: number;
  n_open_payments: number;
  n_nj_medicaid_exclusion: number;
  n_nppes_provider: number;
  n_leie: number;
  /** Provider-kind rows in derived.fraud_signal_observation. */
  n_provider_observations: number;
}

/**
 * One ranked entity in the /leads "highest-value fraud" queue
 * (derived.v_high_value_leads, joined to per-kind name resolution for the
 * top-N only). The ranking is lexicographic over MEASURED dollars and a
 * CITED statute→reward mapping (ref.fraud_reportability_channel), never a
 * fabricated composite score — see migration 112.
 */
export interface HighValueLead {
  /** 1-based position in the full lexicographic ranking. */
  lead_rank: number;
  entity_kind: EntityKind;
  entity_id: string;
  display_name: string | null;
  is_nj: boolean;
  /** Most recent cycle this entity fired any signal (provider cycle = CMS data_year). */
  latest_cycle: string;
  /** Distinct cycles the entity appears in. */
  n_cycles: number;
  n_signals: number;
  /** Distinct signal FAMILIES — >=2 is a multi-source hit. */
  n_families: number;
  max_severity: number;
  /** 1 = highest reportability reward potential … 5 = lowest (no bounty). */
  best_reward_tier: number;
  reward_eligible: boolean;
  /** A prior-sanction signal recurred across ≥2 cycles (penalty failed to deter). */
  repeat_violator: boolean;
  multi_source: boolean;
  /** Peak single-cycle USD exposure (null where the driving signal isn't dollar-denominated). */
  peak_exposure_usd: number | null;
  /** USD exposure summed across cycles. */
  total_exposure_usd: number | null;
  /** Statutory relator-share floor on peak exposure (15% band); null when not reward-eligible. */
  reward_low_usd: number | null;
  /** Statutory relator-share ceiling on peak exposure (30% band). */
  reward_high_usd: number | null;
  driver_signal_id: string;
  driver_signal_family: string;
  /** Enforcement/reward program for the driving signal (e.g. "DOJ False Claims Act (qui tam)"). */
  recovery_program: string;
  /** Where a report is actually filed. */
  recovery_channel: string;
  recovery_channel_url: string;
  /** Governing statute (e.g. "31 U.S.C. § 3730(d)"). */
  statute_citation: string;
  statute_url: string;
}

/**
 * Aggregate framing for the /leads page: tier counts + headline totals, plus
 * the honest "lanes we cannot yet rank" (IRS 501c4/527 dark money, FEC
 * itemized flows) so the page never implies a ranking it lacks the substrate
 * to produce.
 */
export interface HighValueLeadsSummary {
  /** lead count by reward_tier (1..5). */
  count_by_tier: Record<string, number>;
  n_total: number;
  n_reward_eligible: number;
  n_repeat_violators: number;
  n_multi_source: number;
  /** Peak USD exposure across all leads. */
  max_exposure_usd: number | null;
  /** Sum of peak-exposure USD across reward-eligible leads. */
  total_reward_eligible_exposure_usd: number | null;
  /** Itemized FEC contribution rows loaded (0 ⇒ political-flow lane dormant). */
  n_fec_contribution: number;
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
