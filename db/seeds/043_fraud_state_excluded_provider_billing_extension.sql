-- ============================================================================
-- Seed: 043_fraud_state_excluded_provider_billing_extension
--
-- Companion to migration 105 (mig 105 ships the signal_family +
-- calibration_basis CHECK widenings, the refresher, the fraud_signal_config
-- row, and the master-refresher wiring). This seed ships the THREE
-- reference rows the evidence-card UI needs to render the
-- state_excluded_provider_billing signal end-to-end:
--
--   * ref.fraud_signal_human_explanation     (rule_text + plain-English)
--   * ref.fraud_signal_severity_calibration  (severity_level + precedent)
--   * ref.fraud_signal_evidence_url_template (upstream-verify button)
--
-- Mirrors seed 041 (provider_excluded_billing) in shape and structure.
--
-- IDEMPOTENT VIA ON CONFLICT DO UPDATE on every INSERT.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. formula_version (FK anchor; mig 105 already INSERT-ON-CONFLICTed this --
--    duplicated here as a fresh-deploy ordering safety net).
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version (
    formula_version,
    description,
    effective_date,
    notes
) VALUES (
    '2.8.5-fraud-state-excluded-provider-billing-v1',
    'Pillar 2 (civic integrity) FRAUD-F7 Phase-3 signal slice. Adds the '
    'state_excluded_provider_billing cross-source signal: a currently-active '
    'NJ Medicaid/OSC exclusion (with a real NPI) present in CMS Medicare '
    'billing (Part D OR Part B) for the cycle data year. Exact NPI equijoin; '
    'severity 4 (HIGH lead). Introduces signal_family and calibration_basis '
    'value state_exclusion.',
    '2026-06-09',
    'Stacks on 2.8.4-nj-medicaid-exclusion-substrate-v1.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- 2. Human explanation: NJ state-exclusion authority + plain English
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_human_explanation (
    signal_id,
    rule_text,
    citation_authority,
    citation_section,
    citation_url,
    plain_english_template,
    formula_version,
    effective_date
) VALUES (
    'state_excluded_provider_billing',
    'A provider on New Jersey''s currently-active Medicaid/OSC exclusion '
    '(debarment) list, matched by exact NPI, appears in CMS Medicare billing '
    '(Part D prescriber or Part B practitioner) for the cycle data year. The '
    'NJ Office of the State Comptroller debars providers from the NJ Medicaid '
    'program for fraud, abuse, or related misconduct. A NJ Medicaid debarment '
    'does NOT by itself prohibit Medicare (a separate federal program) '
    'billing, so this is a HIGH-PRIORITY LEAD rather than a per-se payment '
    'prohibition: NJ debarments frequently track conduct that also warrants '
    'federal exclusion. The NPI match is exact (not name-based), so identity '
    'is high confidence; severity 4 routes every match to analyst review.',
    'NJ-OSC',
    'NJSA 30:4D-17 (Medicaid program integrity); NJ OSC Medicaid Provider '
    'Debarment List',
    'https://www.nj.gov/comptroller/divisions/medicaid/',
    'Provider NPI {{entity_id}} is on New Jersey''s active Medicaid/OSC '
    'exclusion list AND appears in CMS Medicare billing for {{cycle}} with '
    'combined Part D + Part B Medicare exposure of ${{raw_value}}. A NJ '
    'Medicaid debarment is a strong lead -- it does not itself bar Medicare '
    'billing, but it signals conduct that warrants review. This is an exact '
    'NPI match (high-confidence identity), not a name guess. Bucket = all '
    'Medicare billers in {{cycle}}; percentile {{peer_percentile}}% reflects '
    'how rare a state-excluded-provider overlap is in that population. Verify '
    'on the NJ OSC debarment list and the CMS provider-data file before any '
    'finding -- the match flags a lead for review, not a proven violation.',
    '2.8.5-fraud-state-excluded-provider-billing-v1',
    '2026-06-09'
)
ON CONFLICT (signal_id) DO UPDATE SET
    rule_text              = EXCLUDED.rule_text,
    citation_authority     = EXCLUDED.citation_authority,
    citation_section       = EXCLUDED.citation_section,
    citation_url           = EXCLUDED.citation_url,
    plain_english_template = EXCLUDED.plain_english_template,
    formula_version        = EXCLUDED.formula_version,
    effective_date         = EXCLUDED.effective_date,
    updated_at             = now();


-- ----------------------------------------------------------------------------
-- 3. Severity calibration: severity_level=4 (HIGH lead), state_exclusion basis
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_severity_calibration (
    signal_id,
    severity_level,
    calibration_basis,
    precedent_url,
    precedent_summary,
    formula_version,
    effective_date
) VALUES (
    'state_excluded_provider_billing',
    4,
    'state_exclusion',
    'https://nj.gov/comptroller/doc/nj_debarment_list.pdf',
    'Severity 4 (HIGH), one tier below the federal provider_excluded_billing '
    '(severity 5). Rationale: a NJ OSC Medicaid debarment is a state-program '
    'exclusion, not a federal-payment prohibition, so a Medicare billing '
    'overlap is a high-priority lead -- not the per-se statutory violation '
    'that a federal HHS-OIG LEIE overlap is under 42 USC 1320a-7a. NJ '
    'debarments nonetheless frequently track fraud/abuse conduct that also '
    'draws federal exclusion, so the overlap strongly warrants analyst '
    'review. Severity captures "if true, how bad"; peer_percentile captures '
    'rarity in the combined Medicare-biller population.',
    '2.8.5-fraud-state-excluded-provider-billing-v1',
    '2026-06-09'
)
ON CONFLICT (signal_id) DO UPDATE SET
    severity_level     = EXCLUDED.severity_level,
    calibration_basis  = EXCLUDED.calibration_basis,
    precedent_url      = EXCLUDED.precedent_url,
    precedent_summary  = EXCLUDED.precedent_summary,
    formula_version    = EXCLUDED.formula_version,
    effective_date     = EXCLUDED.effective_date,
    updated_at         = now();


-- ----------------------------------------------------------------------------
-- 4. Evidence URL template: NJ OSC Medicaid debarment list
-- ----------------------------------------------------------------------------
INSERT INTO ref.fraud_signal_evidence_url_template (
    signal_id,
    url_template,
    button_label,
    upstream_source,
    formula_version,
    effective_date
) VALUES (
    'state_excluded_provider_billing',
    'https://nj.gov/comptroller/doc/nj_debarment_list.pdf',
    'View NJ Medicaid debarment list',
    'NJ.gov',
    '2.8.5-fraud-state-excluded-provider-billing-v1',
    '2026-06-09'
)
ON CONFLICT (signal_id) DO UPDATE SET
    url_template     = EXCLUDED.url_template,
    button_label     = EXCLUDED.button_label,
    upstream_source  = EXCLUDED.upstream_source,
    formula_version  = EXCLUDED.formula_version,
    effective_date   = EXCLUDED.effective_date,
    updated_at       = now();
