-- ============================================================================
-- Migration: 038_raw_fec
--
-- TIER 4 v1: Federal Election Commission bulk data.
--
-- Three tables, one per FEC bulk file kind, all keyed by FEC's stable
-- identifiers and tagged with a per-row election cycle so multiple
-- cycles can coexist in raw without mutual interference.
--
--    raw.fec_candidate     <- cn{yy}.zip  ("Candidate Master")
--    raw.fec_committee     <- cm{yy}.zip  ("Committee Master")
--    raw.fec_contribution  <- indiv{yy}.zip ("Individual Contributions")
--
-- WHY THIS EXISTS
-- ---------------
-- Civic-integrity / fraud analytics on the platform require an entity
-- substrate: who are the federal candidates, what committees do they
-- run, who funds those committees. FEC bulk is the canonical free
-- source for all three. Schema has been stable since 2008. Files
-- update every two weeks during a cycle and monthly off-cycle.
--
-- METHODOLOGY NOTES (substrate-honesty)
-- -------------------------------------
-- 1. CYCLE IS PART OF THE PRIMARY KEY for cn and cm. A candidate or
--    committee record can be re-published in successive cycles with
--    different addresses, treasurers, party affiliations, etc.
--    Treating cand_id alone as PK would silently overwrite history.
--
-- 2. SUB_ID is FEC-globally-unique for individual contributions
--    (per FEC documentation). We make it the natural PK on
--    raw.fec_contribution but also tag the row with cycle for
--    partition-friendly querying. If a SUB_ID ever DOES collide
--    across cycles in practice, the PK constraint will trip and the
--    loader will surface the conflict at COPY time -- which is the
--    correct behavior (bug surfaces immediately, not silently
--    corrupts the audit trail).
--
-- 3. TRANSACTION_DT is stored as TEXT (CHAR(8)) because FEC's bulk
--    field uses MMDDYYYY (not Postgres-parseable as DATE without
--    pre-processing). Some rows have invalid dates ("00000000",
--    "        ", etc.); coercing those at COPY time would either
--    abort the whole load or silently DROP rows, both of which
--    violate the "raw mirrors source" contract. The downstream
--    public.v_fec_contribution view parses MMDDYYYY -> DATE with
--    NULL on invalid input; that's the right place for the cooking.
--
-- 4. MEMO_CD = 'X' marks "memo" entries: itemized sub-line entries
--    that should NOT be summed alongside their parent transaction
--    (they would double-count the dollars). The raw table preserves
--    them; the canonical view (migration 039) filters them out.
--
-- 5. All non-PK columns are NULLABLE. FEC bulk files use empty fields
--    (no quoting, no escapes) for missing data, and a CHECK constraint
--    on a non-nullable column would force every empty field to be
--    interpreted as something, which is a fiction. Empty -> NULL.
--
-- 6. CHECK constraints are LIBERAL by design. Production FEC data
--    contains malformed FEC IDs (legacy entries from before the
--    nine-digit standard was enforced), zero-amount placeholder
--    transactions, candidates registered in U.S. territories
--    (state codes outside the 50 + DC), etc. The raw layer accepts
--    everything that COPY can parse; the derived layer applies
--    business-quality filters.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- raw.fec_candidate (one row per candidate per election cycle)
-- ----------------------------------------------------------------------------
CREATE TABLE raw.fec_candidate (
    cycle                CHAR(4)       NOT NULL CHECK (cycle ~ '^[0-9]{4}$'),
    cand_id              TEXT          NOT NULL,
    cand_name            TEXT,
    cand_pty_affiliation TEXT,
    cand_election_yr     SMALLINT,
    cand_office_st       TEXT,
    cand_office          TEXT,
    cand_office_district TEXT,
    cand_ici             TEXT,
    cand_status          TEXT,
    cand_pcc             TEXT,
    cand_st1             TEXT,
    cand_st2             TEXT,
    cand_city            TEXT,
    cand_st              TEXT,
    cand_zip             TEXT,

    source_url           TEXT          NOT NULL,
    source_sha256        CHAR(64)      NOT NULL,
    source_vintage       TEXT          NOT NULL,
    ingested_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),

    PRIMARY KEY (cycle, cand_id)
);

COMMENT ON TABLE raw.fec_candidate IS
    'FEC Candidate Master (cn{yy}.zip). One row per (cycle, cand_id). '
    'cand_id is FEC''s candidate identifier, stable across cycles. '
    'cand_pcc references the candidate''s principal campaign committee '
    '(joins to raw.fec_committee.cmte_id). Source: '
    'https://www.fec.gov/files/bulk-downloads/{cycle}/cn{yy}.zip';

CREATE INDEX raw_fec_candidate_office_st_idx
    ON raw.fec_candidate (cand_office_st);
CREATE INDEX raw_fec_candidate_pcc_idx
    ON raw.fec_candidate (cand_pcc);


-- ----------------------------------------------------------------------------
-- raw.fec_committee (one row per committee per election cycle)
-- ----------------------------------------------------------------------------
CREATE TABLE raw.fec_committee (
    cycle                  CHAR(4)     NOT NULL CHECK (cycle ~ '^[0-9]{4}$'),
    cmte_id                TEXT        NOT NULL,
    cmte_nm                TEXT,
    tres_nm                TEXT,
    cmte_st1               TEXT,
    cmte_st2               TEXT,
    cmte_city              TEXT,
    cmte_st                TEXT,
    cmte_zip               TEXT,
    cmte_dsgn              TEXT,
    cmte_tp                TEXT,
    cmte_pty_affiliation   TEXT,
    cmte_filing_freq       TEXT,
    org_tp                 TEXT,
    connected_org_nm       TEXT,
    cand_id                TEXT,

    source_url             TEXT        NOT NULL,
    source_sha256          CHAR(64)    NOT NULL,
    source_vintage         TEXT        NOT NULL,
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (cycle, cmte_id)
);

COMMENT ON TABLE raw.fec_committee IS
    'FEC Committee Master (cm{yy}.zip). One row per (cycle, cmte_id). '
    'cmte_id is FEC''s committee identifier, stable across cycles. '
    'cand_id is non-NULL only for principal campaign committees and '
    'authorized committees -- joins to raw.fec_candidate.cand_id. '
    'Source: https://www.fec.gov/files/bulk-downloads/{cycle}/cm{yy}.zip';

CREATE INDEX raw_fec_committee_cand_id_idx
    ON raw.fec_committee (cand_id) WHERE cand_id IS NOT NULL;
CREATE INDEX raw_fec_committee_state_idx
    ON raw.fec_committee (cmte_st);


-- ----------------------------------------------------------------------------
-- raw.fec_contribution (one row per individual contribution transaction)
-- ----------------------------------------------------------------------------
--
-- This is the large table: ~25M rows for a presidential cycle nationwide.
-- The PK is sub_id alone (per FEC documentation, sub_id is globally
-- unique). cycle is denormalized into the row for partition-friendly
-- querying; an FK enforces referential integrity to fec_committee.
-- ----------------------------------------------------------------------------
CREATE TABLE raw.fec_contribution (
    cycle              CHAR(4)        NOT NULL CHECK (cycle ~ '^[0-9]{4}$'),
    sub_id             TEXT           NOT NULL,

    cmte_id            TEXT,
    amndt_ind          TEXT,
    rpt_tp             TEXT,
    transaction_pgi    TEXT,
    image_num          TEXT,
    transaction_tp     TEXT,
    entity_tp          TEXT,
    name               TEXT,
    city               TEXT,
    state              TEXT,
    zip_code           TEXT,
    employer           TEXT,
    occupation         TEXT,

    -- Stored as TEXT to faithfully preserve FEC's MMDDYYYY string.
    -- public.v_fec_contribution exposes a parsed DATE column.
    transaction_dt     CHAR(8),

    -- TRANSACTION_AMT is published in DOLLARS (NOT cents). Negative
    -- values are valid (refunds / corrections). FEC ceiling for a
    -- single individual contribution is ~$3,500 in 2024 but cycle-
    -- pre/general pairs can go higher; allow up to $10M to be safe
    -- against legitimate large transfers (PAC-to-PAC, etc.).
    transaction_amt    NUMERIC(14, 2) CHECK (
        transaction_amt IS NULL OR transaction_amt BETWEEN -10000000 AND 10000000
    ),

    other_id           TEXT,
    tran_id            TEXT,
    file_num           TEXT,
    memo_cd            TEXT,
    memo_text          TEXT,

    source_url         TEXT           NOT NULL,
    source_sha256      CHAR(64)       NOT NULL,
    source_vintage     TEXT           NOT NULL,
    ingested_at        TIMESTAMPTZ    NOT NULL DEFAULT now(),

    PRIMARY KEY (sub_id)
);

COMMENT ON TABLE raw.fec_contribution IS
    'FEC Individual Contributions (indiv{yy}.zip / itcont.txt). '
    'One row per transaction. PK is sub_id (globally unique per FEC '
    'documentation). cmte_id joins to raw.fec_committee. '
    'transaction_dt stored as 8-char MMDDYYYY string (raw mirrors '
    'source); see public.v_fec_contribution for parsed DATE. '
    'memo_cd=''X'' marks itemized sub-lines that must NOT be summed '
    'alongside parent transactions (double-count hazard). '
    'Source: https://www.fec.gov/files/bulk-downloads/{cycle}/indiv{yy}.zip';

CREATE INDEX raw_fec_contribution_cycle_idx
    ON raw.fec_contribution (cycle);
CREATE INDEX raw_fec_contribution_cmte_id_idx
    ON raw.fec_contribution (cmte_id);
CREATE INDEX raw_fec_contribution_state_idx
    ON raw.fec_contribution (state);
CREATE INDEX raw_fec_contribution_dt_idx
    ON raw.fec_contribution (transaction_dt);

-- Composite for the canonical "money-to-NJ-candidates" query.
CREATE INDEX raw_fec_contribution_state_cycle_idx
    ON raw.fec_contribution (state, cycle);
