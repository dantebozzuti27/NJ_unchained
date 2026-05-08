-- ============================================================================
-- Migration: 076_ctc_function_v2
--
-- Rewrite derived.f_federal_child_tax_credit (SAME 4-arg signature) to
-- handle BOTH:
--   (a) the pre-/post-ARPA single-stage phaseout (TY 2010-2020, TY 2022+)
--   (b) the ARPA TY 2021 two-stage phaseout (P.L. 117-2 s.9611,
--       IRC s.24(i))
--
-- DESIGN: keep the 4-arg signature unchanged so downstream functions
-- (f_federal_income_tax, f_household_taxes, f_piti_*, f_aei_*,
-- f_personalize_*) continue to work without DROP CASCADE. The $3,600
-- ARPA under-6 bonus ($600 per child under 6) is a V1 known-precision
-- limitation: this composite treats every qualifying child at the
-- amount_6_to_17 rate ($3,000 for TY 2021). Households with under-6
-- children would get $600 more per young child than this returns. A
-- future v3 with a 5-arg under-6 split would be a NEW function name to
-- preserve dependency stability.
--
-- ALL OTHER YEARS are unaffected because their amount_under_6 already
-- equals amount_6_to_17 in the seed (ARPA is the only year where they
-- differ); so the V1 limitation is precisely scoped to TY 2021.
-- ============================================================================

CREATE OR REPLACE FUNCTION derived.f_federal_child_tax_credit(
    p_modified_agi        NUMERIC,
    p_tax_year            SMALLINT,
    p_filing_status       TEXT,
    p_qualifying_children INT
) RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH params AS (
        SELECT
            -- V1 simplification: treat every qualifying child at the
            -- amount_6_to_17 rate. This loses the ARPA $600/under-6
            -- bonus for TY 2021 households with under-6 kids; documented
            -- in migration 076 header.
            amount_6_to_17 AS per_child,
            phaseout_rate,
            CASE WHEN p_filing_status IN ('mfj', 'qss')
                 THEN phaseout_threshold_mfj
                 ELSE phaseout_threshold_single
            END AS stage2_threshold,
            CASE
                WHEN arpa_stage1_threshold_single IS NULL THEN NULL
                WHEN p_filing_status IN ('mfj', 'qss')
                     THEN arpa_stage1_threshold_mfj
                WHEN p_filing_status = 'hoh'
                     THEN arpa_stage1_threshold_hoh
                ELSE arpa_stage1_threshold_single
            END AS stage1_threshold,
            -- ARPA-only: $2,000 per child is the floor that Stage-1
            -- phaseout walks the credit DOWN TO before Stage-2 begins
            -- to walk the floor itself down to $0.
            CASE WHEN arpa_stage1_threshold_single IS NULL
                 THEN NULL ELSE 2000.00
            END AS arpa_floor_per_child
        FROM ref.irs_child_tax_credit
        WHERE tax_year = p_tax_year
    ),
    counts AS (
        SELECT GREATEST(0, COALESCE(p_qualifying_children, 0)) AS total_kids
    ),
    income AS (
        SELECT GREATEST(0::NUMERIC, COALESCE(p_modified_agi, 0)) AS magi
    ),
    full_credit AS (
        SELECT
            (counts.total_kids * params.per_child)::NUMERIC AS amount
        FROM params, counts
    )
    SELECT
        CASE
            WHEN (SELECT total_kids FROM counts) = 0 THEN 0
            WHEN (SELECT per_child FROM params) IS NULL THEN NULL
            WHEN (SELECT stage1_threshold FROM params) IS NULL THEN
                -- Single-stage phaseout: full credit -> 0 at 5% above
                -- stage2_threshold.
                GREATEST(
                    0::NUMERIC,
                    (SELECT amount FROM full_credit)
                    - GREATEST(
                          0::NUMERIC,
                          (SELECT magi FROM income)
                          - (SELECT stage2_threshold FROM params)
                      ) * (SELECT phaseout_rate FROM params)
                )
            ELSE
                -- ARPA two-stage phaseout (TY 2021 only).
                GREATEST(
                    0::NUMERIC,
                    (SELECT amount FROM full_credit)
                    - LEAST(
                          GREATEST(
                              0::NUMERIC,
                              (SELECT amount FROM full_credit)
                              - (SELECT arpa_floor_per_child FROM params)
                                * (SELECT total_kids FROM counts)
                          ),
                          GREATEST(
                              0::NUMERIC,
                              (SELECT magi FROM income)
                              - (SELECT stage1_threshold FROM params)
                          ) * (SELECT phaseout_rate FROM params)
                      )
                    - GREATEST(
                          0::NUMERIC,
                          (SELECT magi FROM income)
                          - (SELECT stage2_threshold FROM params)
                      ) * (SELECT phaseout_rate FROM params)
                )
        END;
$$;

COMMENT ON FUNCTION derived.f_federal_child_tax_credit(NUMERIC, SMALLINT, TEXT, INT) IS
    'Federal Child Tax Credit, post-phaseout. Handles BOTH the standard '
    'TCJA / pre-TCJA single-stage phaseout AND the ARPA TY 2021 two-stage '
    'phaseout (P.L. 117-2 s.9611, IRC s.24(i)). V1 limitation: treats all '
    'qualifying children at amount_6_to_17 rate; for ARPA TY 2021 this '
    'under-credits households with under-6 kids by $600 per young child '
    '(the $3,600-vs-$3,000 ARPA age-tier bonus). Returns NULL when CTC '
    'params are not seeded for the given tax year (substrate-honesty).';
