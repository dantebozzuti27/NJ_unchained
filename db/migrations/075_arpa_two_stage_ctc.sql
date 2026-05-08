-- ============================================================================
-- Migration: 075_arpa_two_stage_ctc
--
-- Schema migration to support the ARPA-2021 two-stage Child Tax Credit
-- phaseout (P.L. 117-2 s.9611, codified at IRC s.24(i)).
--
-- BACKGROUND:
--
-- ARPA temporarily replaced the TCJA single-stage CTC phaseout with a
-- two-stage structure for tax year 2021 ONLY (ARPA expansion was not
-- extended). The two stages are:
--
--   Stage 1 (the "ARPA bump" phaseout): the EXCESS of the ARPA credit
--     ($3,000 / $3,600 per child) over the prior-law CTC ($2,000 per
--     child) phases out at 5% of MAGI above:
--       Single, MFS:    $75,000
--       HOH:            $112,500
--       MFJ, QSS:       $150,000
--     This phases the credit DOWN TO the pre-ARPA $2,000 floor.
--
--   Stage 2 (the standard pre-ARPA phaseout, retained):
--     The remaining $2,000-per-child credit phases out at 5% of MAGI above:
--       Single, HOH, MFS: $200,000
--       MFJ, QSS:         $400,000
--     This phases the floor down to $0.
--
-- The HOH Stage-1 threshold ($112,500) is unique to ARPA -- pre-ARPA CTC
-- had only Single ($200K) and MFJ ($400K) thresholds, with HOH treated
-- as Single. Thus this migration must add a NEW HOH-specific threshold
-- column for the ARPA-only Stage-1 phaseout.
--
-- ALSO: ARPA made the CTC FULLY refundable for TY 2021 -- the
-- refundable_max_per_child = amount_under_6 = $3,600 (or amount_6_to_17 =
-- $3,000) for that one year. The existing schema already supports this
-- via the refundable_max_per_child column.
--
-- This migration is FULLY ADDITIVE and PRESERVES backwards compatibility:
-- the new columns default to NULL, so all existing seeds (TY 2010-2020,
-- TY 2022+) continue to work without modification. Only TY 2021 will
-- populate the new columns.
-- ============================================================================

ALTER TABLE ref.irs_child_tax_credit
    ADD COLUMN IF NOT EXISTS arpa_stage1_threshold_single NUMERIC(12,2)
        CHECK (arpa_stage1_threshold_single IS NULL
               OR arpa_stage1_threshold_single >= 0),
    ADD COLUMN IF NOT EXISTS arpa_stage1_threshold_mfj NUMERIC(12,2)
        CHECK (arpa_stage1_threshold_mfj IS NULL
               OR arpa_stage1_threshold_mfj >= 0),
    ADD COLUMN IF NOT EXISTS arpa_stage1_threshold_hoh NUMERIC(12,2)
        CHECK (arpa_stage1_threshold_hoh IS NULL
               OR arpa_stage1_threshold_hoh >= 0);

COMMENT ON COLUMN ref.irs_child_tax_credit.arpa_stage1_threshold_single IS
    'ARPA TY 2021 only: Stage-1 phaseout AGI threshold for Single/MFS '
    '($75,000). NULL for non-ARPA years. Per IRC s.24(i)(4)(B)(i).';

COMMENT ON COLUMN ref.irs_child_tax_credit.arpa_stage1_threshold_mfj IS
    'ARPA TY 2021 only: Stage-1 phaseout AGI threshold for MFJ/QSS '
    '($150,000). NULL for non-ARPA years. Per IRC s.24(i)(4)(B)(ii).';

COMMENT ON COLUMN ref.irs_child_tax_credit.arpa_stage1_threshold_hoh IS
    'ARPA TY 2021 only: Stage-1 phaseout AGI threshold for HOH '
    '($112,500). NULL for non-ARPA years. Per IRC s.24(i)(4)(B)(iii). '
    'Note: pre-ARPA HOH threshold = Single threshold; this column is '
    'only consulted when arpa_stage1_threshold_single IS NOT NULL.';
