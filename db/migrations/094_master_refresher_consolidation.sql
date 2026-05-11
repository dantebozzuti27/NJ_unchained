-- =============================================================================
-- Migration 094: derived.refresh_all_fraud_signal_observations -- consolidate
--                ALL 18 seeded fraud signals into the master refresher
--
-- VISION_2026 Pillar 2 (civic integrity) -- Phase F-internal substrate hygiene.
-- Closes the silent-omission gap surfaced during the F5b-strict deploy: the
-- master refresher (mig 051, vintage 2025-Q4 when only 8 structural FEC-only
-- signals existed) invokes 8 of 18 currently-seeded fraud signals. The 10
-- missing signals (4 LEIE-bearing, 3 SAM-bearing, 3 USAspending/contractor)
-- were each shipped via standalone migrations (054, 056, 057, 058, 059, 060,
-- 062, 064, 065, 066, 092) that registered their refresher function in the
-- `derived.refresh_signal_*` namespace BUT did not wire it into the
-- orchestrator. Net effect: a Dagster sensor or operator that calls only
-- `derived.refresh_all_fraud_signal_observations(p_cycle)` silently produces
-- 8/18 signal coverage. The 10 missing refreshers DO get called individually
-- by the deploy script `scripts/deploy_neon_pillar2_substrate.sh` (and by hand
-- during ad-hoc invocations), so production today is NOT broken -- but the
-- master orchestrator is no longer the single source of truth for "which
-- signals exist on this platform," which violates substrate-honesty rule 4
-- (no shadow code paths).
--
-- WHAT THIS MIGRATION SHIPS
-- -------------------------
-- A CREATE OR REPLACE of `derived.refresh_all_fraud_signal_observations`
-- that invokes ALL 18 seeded signal refreshers in deterministic order,
-- grouped by substrate dependency tier:
--
--   Tier 1 (FEC-bulk-only structural -- 8 signals): reads from raw.fec_*
--     only; no cross-source dependencies; was the previous master scope.
--   Tier 2 (LEIE-bearing -- 4 signals): reads from raw.hhs_oig_leie +
--     raw.fec_*; requires LEIE substrate loaded (mig 053 + ingester).
--   Tier 3 (SAM-bearing -- 3 signals): reads from raw.sam_gov_exclusion +
--     raw.fec_*; requires SAM substrate loaded (mig 063 + ingester);
--     ingester deferred today, refreshers safely emit 0 obs on empty
--     raw.sam_gov_exclusion.
--   Tier 4 (USAspending-bearing -- 3 signals): reads from
--     raw.usaspending_award + raw.fec_*; requires USAspending substrate
--     loaded (mig 055 + ingester); ingester deferred today, refreshers
--     safely emit 0 obs on empty raw.usaspending_award.
--
-- SUBSTRATE-HONEST DESIGN
-- -----------------------
-- 1. NO PER-REFRESHER EXCEPTION SWALLOWING. The refresher functions are
--    each DELETE+INSERT idempotent slices that return 0 cleanly when their
--    underlying raw table is empty (no rows match the SELECT, INSERT is
--    a no-op). If a refresher RAISES (e.g. its raw table was dropped, a
--    schema drifted, or a referenced view no longer exists), the master
--    should propagate that error so the operator notices. Swallowing
--    would create the same shadow-code-path failure mode this migration
--    fixes -- the master would silently return 0 for the failed signal
--    while the operator believed "all 18 signals refreshed successfully."
--
-- 2. INVOCATION ORDER IS BY SUBSTRATE-DEPENDENCY TIER, NOT ALPHABETICAL.
--    Refreshers do NOT read each other's fraud_signal_observation rows
--    (each reads raw + derived tables directly), so cross-refresher
--    ordering does not affect correctness. The tier grouping IS the
--    documentation: an operator reading the function body sees the
--    substrate-coverage map at a glance ("we have 8 FEC signals, 4 LEIE
--    signals, 3 SAM signals, 3 USAspending signals -- total 18").
--
-- 3. NO RAISE NOTICE PER SIGNAL. The deploy script
--    (scripts/deploy_neon_pillar2_substrate.sh) already iterates
--    derived.fraud_signal_observation post-refresh to print per-signal
--    counts -- adding NOTICE in the function would duplicate that output
--    and pollute Dagster sensor logs where the runner manages logging.
--    The master returns SUM(per-refresher returns) so the caller has a
--    single integer to assert on.
--
-- 4. FORMULA-VERSION STAMP MARKS THE CONSOLIDATION POINT. Future
--    audits can identify which fraud_signal_observation rows were
--    produced by the consolidated master (post-2.5.0) vs. by the
--    8-signal master (pre-2.5.0). The observation rows themselves do
--    not change shape; the formula_version applies to the orchestrator
--    contract, not the observation grain.
--
-- WHAT THIS MIGRATION DELIBERATELY DOES NOT DO
-- --------------------------------------------
-- * Change any per-signal refresher's body. Severities, percentile
--   formulas, and entity_kind emissions are unchanged. This is purely
--   an orchestrator-level rewiring.
-- * Add a Dagster sensor or asset that calls the master automatically.
--   The platform's current cadence is operator-driven; sensors are a
--   separate work item.
-- * Wire the master into a transaction. Each refresher runs in its own
--   transaction (Postgres-level), and the master function executes
--   them sequentially. A wrapping BEGIN/COMMIT around the master would
--   ROLLBACK ALL 18 refreshers if any one fails -- not the right
--   semantics for an idempotent DELETE+INSERT orchestrator; a partial
--   success (e.g. 8 FEC signals fresh + 10 cross-source signals still
--   at the last vintage) is preferable to all-or-nothing.
--
-- IDEMPOTENT VIA CREATE OR REPLACE FUNCTION + governance.schema_migrations
-- sha256 ledger. Safe to re-run.
-- =============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.5.0-master-refresher-consolidation-v1',
    'Pillar 2 (civic integrity) substrate hygiene: '
    'derived.refresh_all_fraud_signal_observations rewritten to invoke '
    'ALL 18 seeded fraud signals (was 8 -- the original FEC-bulk-only '
    'cohort). Adds 10 invocations: 4 LEIE-bearing (entity_on_leie, '
    'entity_on_leie_strict_address, donor_on_leie, '
    'candidate_funded_by_excluded_donors), 3 SAM-bearing '
    '(entity_excluded_via_sam_uei, donor_on_sam, '
    'candidate_funded_by_sam_excluded_donors), 3 USAspending-bearing '
    '(entity_funded_and_excluded, '
    'candidate_funded_by_nj_contractor_employees, '
    'donor_employed_by_nj_contractor). No per-refresher body changes; '
    'this is purely orchestrator-level rewiring. Closes the silent-'
    'omission gap where a Dagster sensor calling only the master would '
    'produce 8/18 signal coverage.',
    '2026-05-11'::DATE,
    'Stacks on 2.4.0-nj-state-candidate-substrate-v1. No-op on a deploy '
    'where the LEIE / SAM / USAspending raw tables are empty -- those '
    'refreshers safely emit 0 observations against empty substrate.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- derived.refresh_all_fraud_signal_observations
--
-- Consolidates all 18 seeded fraud signals under a single orchestrator.
-- Each per-signal refresher is an idempotent DELETE+INSERT slice that
-- returns INT (rows inserted). The master sums and returns total.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.refresh_all_fraud_signal_observations(
    p_cycle CHAR(4)
) RETURNS INT AS $$
DECLARE
    n_total INT := 0;
    n_each  INT;
BEGIN
    -- ----------------------------------------------------------------
    -- TIER 1: FEC-bulk-only structural signals (8)
    -- Substrate: raw.fec_candidate + raw.fec_committee
    -- ----------------------------------------------------------------
    SELECT derived.refresh_treasurer_concentration_observations(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_no_pcc_observations(p_cycle)           INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_broken_pcc_observations(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_multiple_pccs_observations(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_committee_address_clusters_observations(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_committee_name_collisions_observations(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_candidate_namesakes_observations(p_cycle)        INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_treasurer_is_candidate_observations(p_cycle)     INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 2: LEIE-bearing signals (4)
    -- Substrate: raw.hhs_oig_leie + raw.fec_*
    -- (refreshers are no-ops if raw.hhs_oig_leie is empty)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_entity_on_leie(p_cycle)                   INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_entity_on_leie_strict_address(p_cycle)    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_donor_on_leie(p_cycle)                    INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_candidate_funded_by_excluded_donors(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 3: SAM-bearing signals (3)
    -- Substrate: raw.sam_gov_exclusion + raw.fec_*
    -- (refreshers are no-ops if raw.sam_gov_exclusion is empty;
    --  SAM ingester is deferred)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_entity_excluded_via_sam_uei(p_cycle)      INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_donor_on_sam(p_cycle)                     INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_candidate_funded_by_sam_excluded_donors(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    -- ----------------------------------------------------------------
    -- TIER 4: USAspending-bearing signals (3)
    -- Substrate: raw.usaspending_award + raw.fec_* / raw.hhs_oig_leie
    -- (refreshers are no-ops if raw.usaspending_award is empty;
    --  USAspending ingester is deferred)
    -- ----------------------------------------------------------------
    SELECT derived.refresh_signal_entity_funded_and_excluded(p_cycle)       INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_candidate_funded_by_nj_contractor_employees(p_cycle) INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    SELECT derived.refresh_signal_donor_employed_by_nj_contractor(p_cycle)  INTO n_each;
    n_total := n_total + COALESCE(n_each, 0);

    RETURN n_total;
END;
$$ LANGUAGE plpgsql;


COMMENT ON FUNCTION derived.refresh_all_fraud_signal_observations(CHAR(4)) IS
'Master fraud-signal refresher. Invokes all 18 seeded signal refreshers '
'in substrate-dependency tier order (FEC -> LEIE -> SAM -> USAspending) '
'for the given cycle. Each per-signal refresher is an idempotent '
'DELETE+INSERT slice that returns INT (rows inserted). The master '
'returns SUM. Refreshers against empty raw substrate (SAM, USAspending '
'today) safely return 0. Formula version 2.5.0-master-refresher-'
'consolidation-v1 marks the consolidation point; before this version '
'the master invoked only the 8 FEC-bulk structural signals and the 10 '
'cross-source signals required standalone invocation. The deploy '
'script scripts/deploy_neon_pillar2_substrate.sh prints per-signal '
'counts post-refresh via a separate query loop -- this function stays '
'quiet to not pollute Dagster sensor logs.';


COMMIT;
