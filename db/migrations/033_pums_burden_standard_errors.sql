-- ============================================================================
-- Migration: 033_pums_burden_standard_errors
--
-- TIER 3 enhancement: add Successive Differences Replication (SDR)
-- standard errors to both PUMS-derived burden tables.
--
-- WHY THIS EXISTS
-- ---------------
-- The PUMS ingester already stores 80 replicate weights per row as
-- INTEGER[] arrays in raw.acs_pums_*. Until now, those arrays were
-- carrying storage cost without delivering analytical value: every
-- materialized burden cell was a point estimate without a confidence
-- band, so consumers could not distinguish signal from sampling
-- noise.
--
-- This migration adds SE columns alongside every percentile column.
-- Methodology: ACS Successive Differences Replication, SE = sqrt(
-- (4/80) * sum_r (theta_r - theta)^2). Reference: Census ACS PUMS
-- Accuracy of the Data tech doc.
--
-- WHY STORE SE, NOT CONFIDENCE INTERVALS
-- --------------------------------------
-- A CI is a function of (point, SE, alpha). Storing CIs forces the
-- choice of alpha at materialization time. Storing SE leaves the
-- choice to the consumer; any alpha-level CI is derivable as
-- ``theta +/- z_alpha * se``. This mirrors how Census publishes
-- ACS estimates -- tables ship with 90% MOEs but the underlying SE
-- is what's analytically useful.
--
-- NULL SEMANTICS
-- --------------
-- SE is NULL whenever the corresponding point estimate is NULL
-- (suppressed cells), AND also when more than half of the 80
-- replicates failed to produce a finite estimate (e.g., a cell with
-- a very small sample where many replicates have effectively zero
-- weight on the relevant rows). In the latter case, NULL is correct
-- because reporting an SE based on <=40 replicates would be biased
-- low.
--
-- IDEMPOTENCY
-- -----------
-- ALTER TABLE ... ADD COLUMN is idempotent under our migration runner
-- because the runner records SHA256 checksums; re-applying this
-- migration would be a no-op. The new columns are nullable so
-- existing rows simply get NULL until the next materialization.
-- After re-materialization, every non-suppressed cell will have
-- non-null SEs.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- derived.pums_burden_segmented (PUMA grain)
-- ----------------------------------------------------------------------------

ALTER TABLE derived.pums_burden_segmented
    ADD COLUMN household_income_p50_se NUMERIC(12, 2)
        CHECK (household_income_p50_se IS NULL OR household_income_p50_se >= 0),
    ADD COLUMN monthly_cost_p50_se     NUMERIC(10, 2)
        CHECK (monthly_cost_p50_se IS NULL OR monthly_cost_p50_se >= 0),
    ADD COLUMN burden_ratio_p50_se     NUMERIC(8, 4)
        CHECK (burden_ratio_p50_se IS NULL OR burden_ratio_p50_se >= 0);

COMMENT ON COLUMN derived.pums_burden_segmented.household_income_p50_se IS
    'SDR-based standard error of household_income_p50. NULL when the '
    'point estimate is NULL or when fewer than 40 of 80 replicates '
    'produced a finite estimate.';

COMMENT ON COLUMN derived.pums_burden_segmented.monthly_cost_p50_se IS
    'SDR-based standard error of monthly_cost_p50.';

COMMENT ON COLUMN derived.pums_burden_segmented.burden_ratio_p50_se IS
    'SDR-based standard error of burden_ratio_p50. Computed jointly '
    'from numerator + denominator under each replicate weight set, '
    'preserving their covariance (NOT a delta-method approximation).';


-- ----------------------------------------------------------------------------
-- derived.pums_burden_county_segmented (county grain)
-- ----------------------------------------------------------------------------

ALTER TABLE derived.pums_burden_county_segmented
    ADD COLUMN household_income_p50_se NUMERIC(12, 2)
        CHECK (household_income_p50_se IS NULL OR household_income_p50_se >= 0),
    ADD COLUMN monthly_cost_p50_se     NUMERIC(10, 2)
        CHECK (monthly_cost_p50_se IS NULL OR monthly_cost_p50_se >= 0),
    ADD COLUMN burden_ratio_p50_se     NUMERIC(8, 4)
        CHECK (burden_ratio_p50_se IS NULL OR burden_ratio_p50_se >= 0);

COMMENT ON COLUMN derived.pums_burden_county_segmented.burden_ratio_p50_se IS
    'SDR-based standard error of burden_ratio_p50. For multi-county '
    'PUMAs, each replicate weight is fractionally allocated by the '
    'PUMA-county crosswalk before percentile recomputation, so the '
    'SE correctly reflects the additional uncertainty from the '
    'allocation step.';


-- ----------------------------------------------------------------------------
-- Update the public convenience views
--
-- Both views currently project the unsuppressed columns; we extend
-- them to project SEs too. CREATE OR REPLACE only works if the
-- existing column order is preserved as a prefix; we are interleaving
-- new SE columns next to their point estimates, so DROP + CREATE.
-- The views are public reads only; downstream consumers will see a
-- one-shot atomic recreate inside the migration transaction.
-- ----------------------------------------------------------------------------

DROP VIEW IF EXISTS public.v_pums_burden_overall;
CREATE VIEW public.v_pums_burden_overall AS
SELECT
    year, product, puma, tenure_class,
    weighted_n, sample_n,
    household_income_p50, household_income_p50_se,
    monthly_cost_p50,     monthly_cost_p50_se,
    burden_ratio_p50,     burden_ratio_p50_se,
    suppressed
FROM derived.pums_burden_segmented
WHERE state_fips    = '34'
  AND segment_dim   = 'overall'
  AND segment_value = 'overall';

DROP VIEW IF EXISTS public.v_pums_burden_county_overall;
CREATE VIEW public.v_pums_burden_county_overall AS
SELECT
    year, product, county_fips, tenure_class,
    weighted_n, sample_n,
    household_income_p50, household_income_p50_se,
    monthly_cost_p50,     monthly_cost_p50_se,
    burden_ratio_p50,     burden_ratio_p50_se,
    suppressed,
    n_pumas_contributing
FROM derived.pums_burden_county_segmented
WHERE state_fips    = '34'
  AND segment_dim   = 'overall'
  AND segment_value = 'overall';
