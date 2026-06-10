-- ============================================================================
-- Migration: 114_raw_cms_partd_behavioral_columns
--
-- FRAUD-F8 "more healthcare data": enrich raw.cms_partd_prescriber with the
-- behavioral prescribing fields CMS already ships in the Part D Prescriber
-- "by Provider" file but the ingester was dropping.
--
-- WHY
-- ---
-- The undetected-lead reframe (mig 113) needs detectors that flag providers
-- BEFORE any enforcement action. The richest free source of such patterns is
-- already in the file we load:
--   * long-acting opioids (Opioid_LA_*)            -> diversion risk
--   * antipsychotics in the elderly (Antpsyct_GE65_*) -> nursing-home
--     "chemical restraint" / medically-unnecessary prescribing
--   * brand-vs-generic (Brnd_* / Gnrc_*)           -> overbilling / kickback
--   * beneficiary HCC risk score (Bene_Avg_Risk_Scre) -> risk-vs-cost mismatch
-- This migration only LANDS the columns (nullable NUMERIC). The detectors that
-- read them ship in later migrations; loading the columns once means future
-- detectors need no further reload of the 600 MB source file.
--
-- VERIFIABLE-DATA DISCIPLINE
-- --------------------------
-- All columns NULLABLE: CMS suppresses any cell whose beneficiary count is
-- <11 (the *_Sprsn_Flag columns), and the no-silent-imputation invariant
-- requires those to load as SQL NULL, never 0. The ingester maps blank -> NULL
-- via COPY ... NULL ''. Adding columns is append-only and backward compatible:
-- existing rows get NULL until reloaded with the widened column map.
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS. Safe to re-run.
-- ============================================================================

BEGIN;


INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '3.2.0-cms-partd-behavioral-columns-v1',
    'Pillar 2 (civic integrity) FRAUD-F8. Enriches raw.cms_partd_prescriber '
    'with CMS-published behavioral prescribing fields (long-acting opioids, '
    'antibiotics, antipsychotics-in-elderly, brand vs generic claims/cost, '
    'GE65 totals, beneficiary HCC risk score) that the ingester previously '
    'dropped. Substrate for prospective (pre-enforcement) detectors. All '
    'columns nullable; CMS-suppressed (<11-beneficiary) cells load as NULL.',
    '2026-06-09',
    'Stacks on 2.8.x CMS Part D substrate; requires ingester column-map '
    'widening + reload to populate.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- Long-acting opioids (diversion-risk lane).
ALTER TABLE raw.cms_partd_prescriber
    ADD COLUMN IF NOT EXISTS opioid_la_tot_clms     NUMERIC,  -- Opioid_LA_Tot_Clms
    ADD COLUMN IF NOT EXISTS opioid_la_prscrbr_rate NUMERIC,  -- Opioid_LA_Prscrbr_Rate
-- Antibiotics (stewardship / overprescribing lane).
    ADD COLUMN IF NOT EXISTS antbtc_tot_clms        NUMERIC,  -- Antbtc_Tot_Clms
-- Antipsychotics in beneficiaries >=65 (chemical-restraint lane). CMS suppresses
-- these when <11 elderly antipsychotic beneficiaries -> NULL.
    ADD COLUMN IF NOT EXISTS antpsyct_ge65_tot_clms  NUMERIC, -- Antpsyct_GE65_Tot_Clms
    ADD COLUMN IF NOT EXISTS antpsyct_ge65_tot_benes NUMERIC, -- Antpsyct_GE65_Tot_Benes
-- GE65 denominators (the elderly-population base for the antipsychotic rate).
    ADD COLUMN IF NOT EXISTS ge65_tot_clms          NUMERIC,  -- GE65_Tot_Clms
    ADD COLUMN IF NOT EXISTS ge65_tot_benes         NUMERIC,  -- GE65_Tot_Benes
-- Brand vs generic (overbilling / kickback lane).
    ADD COLUMN IF NOT EXISTS brnd_tot_clms          NUMERIC,  -- Brnd_Tot_Clms
    ADD COLUMN IF NOT EXISTS brnd_tot_drug_cst      NUMERIC,  -- Brnd_Tot_Drug_Cst
    ADD COLUMN IF NOT EXISTS gnrc_tot_clms          NUMERIC,  -- Gnrc_Tot_Clms
    ADD COLUMN IF NOT EXISTS gnrc_tot_drug_cst      NUMERIC,  -- Gnrc_Tot_Drug_Cst
-- Beneficiary average HCC risk score (acuity context).
    ADD COLUMN IF NOT EXISTS bene_avg_risk_scre     NUMERIC;  -- Bene_Avg_Risk_Scre

COMMENT ON COLUMN raw.cms_partd_prescriber.antpsyct_ge65_tot_benes IS
    'Antpsyct_GE65_Tot_Benes: beneficiaries >=65 with >=1 antipsychotic claim. '
    'NULL when CMS-suppressed (<11). Numerator for the antipsychotic-in-elderly '
    'chemical-restraint outlier detector.';
COMMENT ON COLUMN raw.cms_partd_prescriber.ge65_tot_benes IS
    'GE65_Tot_Benes: total beneficiaries >=65. Denominator for the '
    'antipsychotic-in-elderly rate. NULL when CMS-suppressed (<11).';


COMMIT;
