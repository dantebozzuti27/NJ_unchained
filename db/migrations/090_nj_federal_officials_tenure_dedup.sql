-- =============================================================================
-- Migration 090: derived.v_nj_federal_officials — tenure-aware deduplication
--
-- Problem (caught in cycle-2026 ingest, May 2026):
--   FEC's `cand_ici='I'` flag is self-declared on Form 2 and is NOT validated
--   by FEC. Misfilers (e.g. challengers who tick the wrong box) appear as
--   `ici='I' AND status='C'`, while the actual sitting member of Congress
--   appears as `ici='I' AND status='N'` if they are NOT seeking re-election
--   in the current cycle (e.g. Sherrill in cycle 2026 — sitting NJ-11 Rep
--   through Jan 2027, but running for NJ Governor in Nov 2025).
--
--   Migration 088's view filtered on `ici='I' AND status='C'`, which dropped
--   Sherrill and surfaced challenger Mejia for NJ-11. That is wrong: the
--   "sitting delegation" should reflect who currently holds the seat, not
--   who is filing for re-election in this cycle.
--
-- Fix:
--   Replace the view with a tenure-aware deduplication that picks, per
--   (cycle, office, district):
--     1) the candidate with the most prior cycles where they ran as the
--        actual incumbent (ici='I' AND status='C') — this is the strongest
--        signal that they really hold the seat
--     2) tiebreak: prefer status='C' (active filer) over 'N' / 'F'
--     3) final tiebreak: lowest cand_id (deterministic)
--
--   For US Senate (office='S'), partition by cand_id rather than district,
--   because both senators share office_district='00' and we must surface
--   both seats independently.
--
--   We KEEP the substrate-honest filter `ici='I'` and `status IN ('C','N','F')`
--   so we do not invent incumbents who never self-declared.
--
-- Idempotent: DROP + CREATE (we are adding a new column position, which
-- CREATE OR REPLACE cannot do). Safe to re-run; nothing else depends on
-- the view yet (callers query it through the SQL client at request time).
-- =============================================================================

DROP VIEW IF EXISTS derived.v_nj_federal_officials;

CREATE VIEW derived.v_nj_federal_officials AS
WITH base AS (
    SELECT
        c.cycle,
        c.cand_id,
        c.cand_name,
        c.cand_office,
        c.cand_office_st,
        c.cand_office_district,
        c.cand_pty_affiliation,
        c.cand_ici,
        c.cand_status,
        c.cand_election_yr,
        -- Tenure proxy: in how many earlier cycles did this exact cand_id
        -- run as a true incumbent (ici='I' AND status='C')? Higher = more
        -- likely the candidate is the actual sitting member.
        (
            SELECT COUNT(*)
            FROM raw.fec_candidate c2
            WHERE c2.cand_id = c.cand_id
              AND c2.cycle < c.cycle
              AND c2.cand_ici = 'I'
              AND c2.cand_status = 'C'
        )::INT AS prior_incumbent_cycles,
        (
            SELECT COUNT(*)
            FROM raw.fec_candidate c3
            WHERE c3.cand_id = c.cand_id
              AND c3.cycle <= c.cycle
        )::INT AS total_cycles_filed
    FROM raw.fec_candidate c
    WHERE c.cand_office_st = 'NJ'
      AND c.cand_office IN ('S', 'H')
      AND c.cand_ici = 'I'
      -- Allow 'N' (incumbent not seeking re-election this cycle, e.g.
      -- Sherrill running for Governor) and 'F' (future candidate) so we
      -- catch sitting members who do not have an active 'C' filing.
      AND c.cand_status IN ('C', 'N', 'F')
),
ranked AS (
    SELECT
        b.*,
        ROW_NUMBER() OVER (
            PARTITION BY
                b.cycle,
                b.cand_office,
                CASE
                    -- Senate has 2 seats per state both at district '00';
                    -- use cand_id so each senator is its own partition.
                    WHEN b.cand_office = 'S' THEN b.cand_id
                    ELSE b.cand_office_district
                END
            ORDER BY
                b.prior_incumbent_cycles DESC,
                (b.cand_status = 'C')::INT DESC,
                b.total_cycles_filed DESC,
                b.cand_id ASC
        ) AS rn
    FROM base b
)
SELECT
    r.cycle,
    r.cand_id AS entity_id,
    r.cand_name AS official_name,
    r.cand_office AS office_code,
    r.cand_office_district AS office_district,
    r.cand_pty_affiliation AS office_party,
    r.cand_ici AS incumbent_status,
    r.cand_election_yr AS election_year,
    -- New: how confident are we that this is really the sitting member?
    -- prior_incumbent_cycles > 0 = strong evidence (won prior election)
    -- prior_incumbent_cycles = 0 = newcomer (relies on FEC self-declaration
    -- alone — worth flagging for a manual cross-check against
    -- clerk.house.gov / senate.gov rosters)
    r.prior_incumbent_cycles,
    CASE r.cand_office
        WHEN 'S' THEN 'U.S. Senator'
        WHEN 'H' THEN 'U.S. Representative'
        WHEN 'P' THEN 'U.S. President'
        ELSE r.cand_office
    END AS office_label,
    COALESCE(risk.risk_score, 0)::NUMERIC AS risk_score,
    COALESCE(risk.n_signals_fired, 0) AS n_signals_fired,
    COALESCE(risk.signals_fired, ARRAY[]::TEXT[]) AS signals_fired,
    COALESCE(risk.max_severity::INT, 0) AS max_severity,
    risk.last_observation_at
FROM ranked r
LEFT JOIN derived.v_entity_fraud_risk risk
  ON risk.entity_kind = 'candidate'
 AND risk.cycle = r.cycle
 AND risk.entity_id = r.cand_id
WHERE r.rn = 1
ORDER BY r.cand_office DESC, r.cand_office_district NULLS FIRST, r.cand_id ASC;

COMMENT ON VIEW derived.v_nj_federal_officials IS
'NJ federal incumbents (Senate + House) for a given FEC cycle, deduplicated
per (cycle, office, district) using a tenure proxy (count of prior cycles
where the cand_id actually ran as ici=I AND status=C). Catches sitting
members who are not seeking re-election in the current cycle (e.g. Sherrill
running for governor). Senate partitions by cand_id since both NJ senators
share office_district=00. Substrate-honest: relies on FEC self-declaration
and historical filings; for newcomers (prior_incumbent_cycles=0) the result
is only as accurate as FEC Form 2 self-reporting.';
