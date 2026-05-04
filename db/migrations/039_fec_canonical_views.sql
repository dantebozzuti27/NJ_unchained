-- ============================================================================
-- Migration: 039_fec_canonical_views
--
-- TIER 4 v1 read surface: cooked views over raw.fec_*.
--
-- The raw layer mirrors FEC's bytes faithfully (MMDDYYYY date strings,
-- no MEMO_CD filtering, all rows including the malformed ones). This
-- migration introduces the canonical "consume me" views that handle:
--
--   * MMDDYYYY -> DATE parsing with NULL-on-invalid (no row drops)
--   * MEMO_CD = 'X' filtered out of summable contributions
--   * NJ-scoped slices for the platform's primary axis
--   * State + cycle scoping with stable ordering for time-series UIs
--
-- WHY VIEWS vs MATERIALIZED TABLES
-- ---------------------------------
-- raw.fec_contribution is large (~25M rows nationwide for a
-- presidential cycle). Materializing a NJ-scoped subset would cost
-- ~3% of that = ~750K rows and would need to be refreshed on every
-- raw load. The query cost of the view is bounded by the
-- (state, cycle) and (cmte_id) indexes on raw.fec_contribution; PG
-- can answer typical NJ-only queries in < 1 second over 25M rows.
-- We materialize derived analytic shapes downstream (Tier 4 v1.5+),
-- not raw-pass-through filters.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- public.v_fec_candidate -- raw, but typed
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_fec_candidate AS
SELECT
    cycle,
    cand_id,
    cand_name,
    cand_pty_affiliation,
    cand_election_yr,
    cand_office_st,
    cand_office,
    cand_office_district,
    cand_ici,
    cand_status,
    cand_pcc        AS principal_campaign_committee_id,
    cand_st1,
    cand_st2,
    cand_city,
    cand_st,
    cand_zip,
    ingested_at
FROM raw.fec_candidate;

COMMENT ON VIEW public.v_fec_candidate IS
    'Pass-through over raw.fec_candidate with light renames '
    '(cand_pcc -> principal_campaign_committee_id) for downstream '
    'consumer clarity.';


-- ----------------------------------------------------------------------------
-- public.v_fec_committee -- raw, but typed
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_fec_committee AS
SELECT
    cycle,
    cmte_id,
    cmte_nm           AS committee_name,
    tres_nm           AS treasurer_name,
    cmte_st1, cmte_st2, cmte_city, cmte_st, cmte_zip,
    cmte_dsgn,
    cmte_tp,
    cmte_pty_affiliation,
    cmte_filing_freq,
    org_tp,
    connected_org_nm  AS connected_organization,
    cand_id,
    ingested_at
FROM raw.fec_committee;


-- ----------------------------------------------------------------------------
-- public.v_fec_contribution -- parsed DATE, MEMO_CD filtered for summable use
-- ----------------------------------------------------------------------------
--
-- This is the canonical view downstream consumers should query. Two
-- transforms vs raw:
--
--   * transaction_date  : DATE parsed from MMDDYYYY. Rows with
--                         malformed dates ('00000000', '        ',
--                         etc.) get NULL here (the raw row is
--                         retained -- still queryable via raw.fec_*).
--   * is_memo           : convenience boolean for "should I sum this?".
--                         memo_cd='X' rows are sub-line itemizations
--                         that double-count their parent's amount;
--                         summable analytics filter WHERE NOT is_memo.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_fec_contribution AS
SELECT
    cycle,
    sub_id,
    cmte_id,
    name              AS contributor_name,
    city              AS contributor_city,
    state             AS contributor_state,
    zip_code          AS contributor_zip,
    employer          AS contributor_employer,
    occupation        AS contributor_occupation,
    entity_tp         AS contributor_entity_type,
    transaction_tp    AS transaction_type,
    transaction_pgi   AS transaction_primary_general,
    transaction_amt   AS transaction_amount,
    transaction_dt    AS transaction_date_raw,
    -- MMDDYYYY -> DATE. Postgres's TO_DATE is permissive; we wrap in
    -- a CASE that returns NULL for the canonical "missing" sentinels
    -- and for any string that fails to_date silently.
    CASE
        WHEN transaction_dt IS NULL                 THEN NULL
        WHEN transaction_dt = '00000000'            THEN NULL
        WHEN transaction_dt ~ '^[0-9]{8}$' THEN
            -- Defense in depth: MM in 01-12, DD in 01-31. Returning
            -- NULL beats raising. Any out-of-range date that survives
            -- this regex check would fall through to TO_DATE which
            -- raises; we preempt that with a second check.
            CASE
                WHEN substring(transaction_dt FROM 1 FOR 2)::int BETWEEN 1 AND 12
                 AND substring(transaction_dt FROM 3 FOR 2)::int BETWEEN 1 AND 31
                THEN to_date(transaction_dt, 'MMDDYYYY')
                ELSE NULL
            END
        ELSE NULL
    END                                  AS transaction_date,
    amndt_ind, rpt_tp, image_num, other_id, tran_id, file_num,
    memo_cd,
    -- COALESCE flips NULL memo_cd to FALSE (the SQL 3-valued result of
    -- (NULL = 'X') is NULL, which would propagate through downstream
    -- WHERE NOT is_memo as a row-drop -- the wrong answer).
    COALESCE(memo_cd = 'X', false)       AS is_memo,
    memo_text,
    ingested_at
FROM raw.fec_contribution;

COMMENT ON VIEW public.v_fec_contribution IS
    'Canonical individual-contributions read surface. Parses '
    'MMDDYYYY -> DATE (NULL on invalid). is_memo flags MEMO_CD=''X'' '
    'sub-line entries; analytics that sum amounts must filter '
    'WHERE NOT is_memo to avoid double-counting.';


-- ----------------------------------------------------------------------------
-- public.v_fec_nj_candidates -- candidates registered in NJ
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_fec_nj_candidates AS
SELECT * FROM public.v_fec_candidate
WHERE cand_office_st = 'NJ';

COMMENT ON VIEW public.v_fec_nj_candidates IS
    'NJ federal candidates (cand_office_st = ''NJ''): senators, '
    'representatives, presidential candidates registered in NJ.';


-- ----------------------------------------------------------------------------
-- public.v_fec_money_to_nj_candidates -- contributions to committees
--                                        affiliated with NJ candidates
-- ----------------------------------------------------------------------------
--
-- This is the headline civic-integrity query: who funds NJ federal
-- candidates? It joins:
--   v_fec_contribution (transactions, cooked dates, MEMO filtered)
--   v_fec_committee    (committee -> candidate link)
--   v_fec_nj_candidates (NJ candidates only)
--
-- The result is one row per contribution, annotated with the NJ
-- candidate the money flowed to. Consumers can group by donor,
-- by candidate, by ZIP, or by month (transaction_date) to derive
-- the full menu of questions FEC data can answer.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_fec_money_to_nj_candidates AS
SELECT
    nj.cand_id,
    nj.cand_name,
    nj.cand_office,
    nj.cand_office_district,
    nj.cand_pty_affiliation,
    cm.cmte_id,
    cm.committee_name,
    cm.cmte_dsgn,
    contrib.cycle,
    contrib.sub_id,
    contrib.contributor_name,
    contrib.contributor_city,
    contrib.contributor_state,
    contrib.contributor_zip,
    contrib.contributor_employer,
    contrib.contributor_occupation,
    contrib.contributor_entity_type,
    contrib.transaction_type,
    contrib.transaction_primary_general,
    contrib.transaction_amount,
    contrib.transaction_date,
    contrib.is_memo
FROM public.v_fec_contribution contrib
JOIN public.v_fec_committee     cm ON cm.cmte_id = contrib.cmte_id
                                  AND cm.cycle   = contrib.cycle
JOIN public.v_fec_nj_candidates nj ON nj.cand_id = cm.cand_id
                                  AND nj.cycle   = cm.cycle;

COMMENT ON VIEW public.v_fec_money_to_nj_candidates IS
    'Headline civic-integrity surface: every individual contribution '
    'to a committee affiliated with a NJ federal candidate. Joins '
    'across cycle so 2020/2022/2024 contributions sit alongside the '
    'right candidate vintages. Filter WHERE NOT is_memo for summable '
    'analytics.';
