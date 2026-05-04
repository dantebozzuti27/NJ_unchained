-- ============================================================================
-- Migration: 052_fraud_risk_score
--
-- TIER 4 v3 STEP 3: L3a rule-based scoring function.
--
-- Reads only the (severities, peer_percentiles) parallel arrays produced
-- by derived.v_entity_fraud_features (L2). Pure function: deterministic,
-- IMMUTABLE, PARALLEL SAFE. The new derived.v_entity_fraud_risk view
-- composes L2 with the score column and is the natural read surface
-- the /fec/risk/entities API will hit.
--
-- FORMULA (pinned in work_left.txt 2026-05-04 design)
-- ---------------------------------------------------
--   phi(p, sev)     = sev * max(0, p - 0.95)^gamma
--   raw_sum         = SUM_s phi(percentile_s, severity_s)
--   risk_score      = 100 * (1 - exp(-k * raw_sum))
--   gamma           = 2
--   k               = 50  -- tunable; calibrate against analyst-confirmed cases
--
-- WHY THESE CHOICES (substrate-honesty)
-- -------------------------------------
-- 1. TAIL-ONLY CONTRIBUTION via max(0, p - 0.95).
--    A signal at the 60th percentile is "normal", not "lightly suspicious".
--    Anything below 0.95 contributes zero. This keeps the score insensitive
--    to noise below threshold and sensitive to extreme outliers, which is
--    the operator-intuitive behavior. Squaring (gamma=2) makes the tail
--    contribution superlinear: (p=0.99) contributes 4x more than (p=0.97)
--    after the threshold.
--
-- 2. ADDITIVE FUSION, NOT MULTIPLICATIVE. We deliberately do NOT do
--    AND-fusion (e.g., score = 1 - PRODUCT(1 - phi)). Multiplicative
--    fusion explodes when 3+ signals fire and is indefensible in a
--    courtroom or at a public-records hearing. Additive composition is
--    the conservative choice and prints cleanly on the evidence panel
--    (each signal's contribution is a literal addend).
--
-- 3. exp-form NOT sigmoid. sigmoid(0) = 0.5 -> 0 inputs would map to
--    score 50, breaking the "no signals fired = no risk" invariant.
--    1 - exp(-k*x) has the right zero-input semantics, the same
--    bounded-monotone shape, and a single tunable scale parameter k.
--
-- 4. RETURNS NUMERIC(5,2). Two decimal places of precision is enough
--    for percentile-rank-on-score to be unique within typical bucket
--    sizes (~few hundred entities); printing more digits suggests
--    false precision.
--
-- 5. NULL / EMPTY HANDLING. An entity with zero fired signals is absent
--    from L2 by construction (the view's GROUP BY excludes empty groups),
--    so the function should never see empty arrays in production. If a
--    caller does pass NULL or empty: return 0 (defensive). Mismatched
--    array lengths raise -- that is a programming bug in the caller.
--
-- 6. IMMUTABLE + PARALLEL SAFE. Function reads no tables and uses no
--    volatile inputs (now(), random(), etc.). Marking it IMMUTABLE lets
--    Postgres cache function calls in the plan and run them in
--    parallel-safe contexts (the read API will benefit at scale).
-- ============================================================================


-- ----------------------------------------------------------------------------
-- derived.fraud_risk_score(severities, peer_percentiles)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION derived.fraud_risk_score(
    severities       SMALLINT[],
    peer_percentiles NUMERIC[]
)
RETURNS NUMERIC(5, 2)
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    n_sev INT;
    n_pct INT;
    raw_sum NUMERIC := 0;
    score NUMERIC;
BEGIN
    -- Defensive NULL / empty handling (entity with zero signals -> score 0).
    IF severities IS NULL OR peer_percentiles IS NULL THEN
        RETURN 0::NUMERIC(5, 2);
    END IF;

    n_sev := COALESCE(array_length(severities,       1), 0);
    n_pct := COALESCE(array_length(peer_percentiles, 1), 0);

    IF n_sev = 0 AND n_pct = 0 THEN
        RETURN 0::NUMERIC(5, 2);
    END IF;

    IF n_sev <> n_pct THEN
        RAISE EXCEPTION
            'fraud_risk_score: severities and peer_percentiles must have '
            'equal length; got % vs %', n_sev, n_pct;
    END IF;

    -- Tail-only contribution: phi(p, sev) = sev * max(0, p - 0.95)^2
    -- Sum across the parallel arrays. UNNEST with two arrays is row-aligned
    -- when array_length is equal (validated above).
    SELECT COALESCE(SUM(
               sev::NUMERIC * POWER(GREATEST(0::NUMERIC, p - 0.95), 2)
           ), 0)
      INTO raw_sum
      FROM UNNEST(severities, peer_percentiles) AS t(sev, p);

    -- 100 * (1 - exp(-k * raw_sum)); k = 50.
    score := 100::NUMERIC * (1::NUMERIC - EXP(-50::NUMERIC * raw_sum));

    -- Round once at the boundary; clamp defensively (numeric drift could
    -- in theory yield 100.000000001 or -0.0000001).
    RETURN LEAST(100::NUMERIC, GREATEST(0::NUMERIC, ROUND(score, 2)))::NUMERIC(5, 2);
END;
$$;

COMMENT ON FUNCTION derived.fraud_risk_score(SMALLINT[], NUMERIC[]) IS
'TIER 4 v3 L3a: composite risk score in [0, 100] computed from an '
'entity''s parallel (severities, peer_percentiles) arrays. Pure '
'function: phi(p,sev)=sev*max(0,p-0.95)^2; raw_sum=SUM(phi); '
'score=100*(1-exp(-50*raw_sum)). NOT a probability of fraud -- it is '
'a composite of analyst-curated severity weighted by peer-bucket '
'tail percentile. Probability-of-fraud requires labels (L3c, gated '
'on L5 triage outputs); see work_left.txt 2026-05-04 design pin.';


-- ----------------------------------------------------------------------------
-- derived.v_entity_fraud_risk
-- ----------------------------------------------------------------------------
-- The natural read surface for the /fec/risk/entities API and the
-- /fraud#risk default landing UI: every entity in L2, with the L3a score
-- column appended. Ordering by risk_score DESC gives the analyst queue
-- without any additional bookkeeping. All evidence-panel inputs (signals,
-- severities, percentiles, buckets, evidence_urls) are preserved so the
-- API can render the score decomposition without a second query.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_entity_fraud_risk AS
SELECT
    f.cycle,
    f.entity_kind,
    f.entity_id,

    derived.fraud_risk_score(f.severities, f.peer_percentiles) AS risk_score,

    f.n_signals_fired,
    f.max_severity,
    f.max_peer_percentile,
    f.avg_peer_percentile,
    f.primary_peer_bucket,

    f.signals_fired,
    f.severities,
    f.peer_percentiles,
    f.peer_buckets,
    f.raw_values,
    f.evidence_urls,

    f.last_observation_at
FROM derived.v_entity_fraud_features f;

COMMENT ON VIEW derived.v_entity_fraud_risk IS
'TIER 4 v3 read surface: per-entity feature vector + risk_score. '
'Sort DESC by risk_score for the analyst queue; filter by '
'(cycle, entity_kind) for the /fec/risk/entities API; deep-link to '
'/fec/risk/entities/{kind}/{id} for the evidence panel using all '
'parallel arrays (signals_fired, severities, peer_percentiles, '
'peer_buckets, raw_values, evidence_urls).';
