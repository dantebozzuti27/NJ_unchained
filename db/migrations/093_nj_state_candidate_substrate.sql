-- =============================================================================
-- Migration 093: ref.nj_state_candidate + derived.v_nj_state_candidates
--
-- VISION_2026 Pillar 2 (civic integrity) -- Phase F8.5 stub: NJ state-and-
-- municipal officeholder substrate, the federal-incumbent roster's analog
-- for NJ ELEC-regulated seats.
--
-- USER-FACING PROBLEM STATEMENT (verbatim, prior session)
-- -------------------------------------------------------
--     "we also should surface more powerful politicians like federal
--      congressman senators and governors in NJ"
--     "data should be the most recent we can attain for this. such as new
--      senator andy kim who only got to office in 2025"
--
-- The federal roster (mig 088 + 090) already surfaces Senate + House. NJ
-- Governor + Lt. Governor + state legislature live at NJ ELEC, NOT FEC.
-- The NJ ELEC bulk-data endpoint is Imperva-gated (anonymous scrapers get
-- HTTP 403 + a JavaScript challenge), so the data path that fully closes
-- this gap is an authenticated ELEC ingester -- a separate work item.
--
-- WHAT THIS MIGRATION SHIPS (substrate-honest scope)
-- --------------------------------------------------
-- A manually-curated reference table of publicly-announced NJ candidates
-- for major statewide offices in a given election year, with the
-- following hard constraints:
--   * Every row carries a `source_url` the analyst can click to verify
--     the announcement against an external authority (Wikipedia article,
--     NJ Division of Elections page, NJ ELEC filing).
--   * Every row carries a `source_doc_date` -- the date the maintainer
--     last checked the citation. UI exposes this so users see how stale
--     the curated record is relative to the live ELEC reality.
--   * Every row carries a `campaign_finance_ingest_pending` flag
--     (computed in the derived view as elec_filing_id IS NULL) so the
--     UI can render a "campaign-finance ingest pending" badge -- the
--     platform makes ZERO claims about contributions / expenditures /
--     anomaly signals for these entities until ELEC ingest lands.
--   * The schema permits but does NOT seed certified election results
--     (primary_winner, general_winner). Those require either a
--     verified NJ Division of Elections certified-results ingest OR
--     human attestation; neither has shipped. The columns exist for
--     forward compatibility but stay NULL today.
--
-- WHAT THIS MIGRATION DELIBERATELY DOES NOT DO
-- --------------------------------------------
--   * Wire nj_state_candidate into derived.fraud_signal_observation as
--     a new entity_kind. The 17 signals in fraud_signal_config all
--     consume FEC-bulk substrate (raw.fec_candidate, raw.fec_committee,
--     raw.fec_indiv); none can fire against an NJ state candidate
--     without ELEC donor/committee substrate. Adding entity_kind=
--     'nj_state_candidate' to the observation taxonomy without an
--     ingester that can produce observations would create dead enum
--     values -- worse than waiting.
--   * Seed Lt. Governor running-mates. NJ Lt. Governor candidates are
--     selected by the gubernatorial nominee AFTER the primary; that's
--     not "announced primary candidate" data and would require results
--     attestation. Future-compatible via the office='lt_governor' enum.
--   * Seed state legislature (Senate + Assembly). 80+ assembly seats +
--     40 senate seats per cycle, no realistic manual-curate path; this
--     needs the ELEC ingester.
--   * Cite "Sherrill won the 2025 primary" or "took office Jan 2026."
--     Those are facts the platform cannot independently verify without
--     a results-ingester. The UI must say "publicly announced" only.
--
-- DESIGN DECISIONS
-- ----------------
-- * candidate_id format is 'NJ-STATE-<LAST>-<FIRST>-<YEAR>-<OFFICE>' to
--   parallel FEC's 'H4NJ09031' / 'S4NJ00027' opaque IDs but make the
--   manually-curated rows greppable. Enforced by CHECK constraint.
-- * party is enum-constrained (DEM/REP/IND/LIB/GRN/CON/OTHER) -- the
--   six historically-relevant NJ ballot lines plus IND for unaffiliated
--   filers; OTHER catches edge-case minor parties without exhausting
--   the enum for every state.
-- * office is enum-constrained (governor / lt_governor / state_senate /
--   state_assembly / attorney_general / state_supreme_court) -- the six
--   statewide / state-legislative office kinds. Municipal mayor / county
--   freeholder are intentionally OUT of scope for v1.
-- * source_url MUST be HTTPS (CHECK constraint) -- substrate-honesty rule
--   1 (every claim is independently verifiable). Length >= 15 catches
--   accidental 'https://' alone.
-- * No FK from nj_state_candidate to fraud_signal_config or observation:
--   nj_state_candidate is a leaf reference table, not part of the
--   anomaly-detection substrate. Forward compat is via entity_kind=
--   'nj_state_candidate' if and when the donor-graph ingester ships.
-- * formula_version FK preserves provenance: every row knows which
--   ref.formula_version stamping produced it, so the UI can render
--   "rule version 2.4.0-nj-state-candidate-substrate-v1, last verified
--   YYYY-MM-DD" alongside the candidate card.
-- * IMMUTABLE updated_at trigger mirrors the
--   _fraud_signal_evidence_url_template_set_updated_at pattern from
--   mig 088 so this surface ages consistently with the others.
-- =============================================================================

BEGIN;


-- ----------------------------------------------------------------------------
-- Formula version registration
-- ----------------------------------------------------------------------------
INSERT INTO ref.formula_version
    (formula_version, description, effective_date, notes)
VALUES (
    '2.4.0-nj-state-candidate-substrate-v1',
    'Pillar 2 (civic integrity) Phase F8.5 stub: ref.nj_state_candidate -- '
    'manually-curated reference table of publicly-announced NJ candidates '
    'for major statewide offices (governor + lt_governor + attorney_general '
    '+ state_senate + state_assembly + state_supreme_court). Every row '
    'carries an HTTPS citation URL + source_doc_date for substrate-honesty. '
    'No certified-results claims (primary_winner / general_winner stay '
    'NULL until NJ Division of Elections ingest lands). Closes the '
    'user-facing gap: "we should surface more powerful politicians like '
    'governors in NJ." Companion derived view v_nj_state_candidates '
    'exposes the campaign_finance_ingest_pending flag the UI renders as '
    'a "campaign-finance ingest pending" badge.',
    '2026-05-10'::DATE,
    'Stacks on 2.3.0-fraud-strict-address-v1. Forward-compat with the '
    'planned NJ ELEC ingester (Phase F8.5-data); elec_filing_id stays '
    'NULL on every row until that ingester ships, at which point the '
    'badge flips off and the candidate becomes eligible for donor-graph '
    'fraud signals.'
)
ON CONFLICT (formula_version) DO UPDATE SET
    description    = EXCLUDED.description,
    effective_date = EXCLUDED.effective_date,
    notes          = EXCLUDED.notes;


-- ----------------------------------------------------------------------------
-- ref.nj_state_candidate
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref.nj_state_candidate (
    candidate_id              TEXT          NOT NULL PRIMARY KEY,
    full_name                 TEXT          NOT NULL,
    party                     TEXT          NOT NULL,
    office                    TEXT          NOT NULL,
    election_year             SMALLINT      NOT NULL,
    primary_date              DATE,
    general_date              DATE,

    -- Public-announcement metadata.
    announced_candidate       BOOLEAN       NOT NULL DEFAULT FALSE,
    announcement_date         DATE,
    announcement_url          TEXT,
    prior_office              TEXT,
    campaign_committee_name   TEXT,

    -- Results (substrate-honest: stays NULL until verified ingest lands).
    primary_winner            BOOLEAN,
    primary_result_url        TEXT,
    general_winner            BOOLEAN,
    general_result_url        TEXT,

    -- Forward-compat hook for ELEC ingester (NULL = not yet ingested).
    elec_filing_id            TEXT,

    -- Canonical citation for the row as a whole.
    source_url                TEXT          NOT NULL,
    source_authority          TEXT          NOT NULL,
    source_doc_date           DATE          NOT NULL,

    notes                     TEXT,

    formula_version           TEXT          NOT NULL
        REFERENCES ref.formula_version(formula_version)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    effective_date            DATE          NOT NULL DEFAULT CURRENT_DATE,
    ingested_at               TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT nj_state_candidate_party_chk
        CHECK (party IN ('DEM', 'REP', 'IND', 'LIB', 'GRN', 'CON', 'OTHER')),

    CONSTRAINT nj_state_candidate_office_chk
        CHECK (office IN (
            'governor',
            'lt_governor',
            'attorney_general',
            'state_senate',
            'state_assembly',
            'state_supreme_court'
        )),

    CONSTRAINT nj_state_candidate_election_year_chk
        CHECK (election_year BETWEEN 2000 AND 2050),

    -- Format: NJ-STATE-<UPPER-WORDS-AND-HYPHENS>-<YEAR>-<UPPER_OFFICE>
    CONSTRAINT nj_state_candidate_id_format_chk
        CHECK (candidate_id ~ '^NJ-STATE-[A-Z0-9-]+-[0-9]{4}-[A-Z_]+$'),

    CONSTRAINT nj_state_candidate_source_url_chk
        CHECK (
            source_url LIKE 'https://%'
            AND length(source_url) >= 15
        ),

    CONSTRAINT nj_state_candidate_announcement_url_chk
        CHECK (
            announcement_url IS NULL
            OR (
                announcement_url LIKE 'https://%'
                AND length(announcement_url) >= 15
            )
        ),

    CONSTRAINT nj_state_candidate_primary_result_url_chk
        CHECK (
            primary_result_url IS NULL
            OR (
                primary_result_url LIKE 'https://%'
                AND length(primary_result_url) >= 15
            )
        ),

    CONSTRAINT nj_state_candidate_general_result_url_chk
        CHECK (
            general_result_url IS NULL
            OR (
                general_result_url LIKE 'https://%'
                AND length(general_result_url) >= 15
            )
        ),

    -- If a winner flag is set, a result_url MUST accompany it
    -- (no naked claims that bypass the citation contract).
    CONSTRAINT nj_state_candidate_primary_winner_requires_url_chk
        CHECK (
            primary_winner IS NULL
            OR primary_result_url IS NOT NULL
        ),

    CONSTRAINT nj_state_candidate_general_winner_requires_url_chk
        CHECK (
            general_winner IS NULL
            OR general_result_url IS NOT NULL
        ),

    -- If announced, announcement_date must be set; if not announced,
    -- announcement_date must be NULL (no contradictions in the row).
    CONSTRAINT nj_state_candidate_announced_consistency_chk
        CHECK (
            (announced_candidate = FALSE AND announcement_date IS NULL)
            OR (announced_candidate = TRUE)
        )
);

CREATE INDEX IF NOT EXISTS idx_nj_state_candidate_office_year
    ON ref.nj_state_candidate (office, election_year);

CREATE INDEX IF NOT EXISTS idx_nj_state_candidate_party
    ON ref.nj_state_candidate (party);

CREATE INDEX IF NOT EXISTS idx_nj_state_candidate_announced
    ON ref.nj_state_candidate (announced_candidate)
    WHERE announced_candidate = TRUE;


COMMENT ON TABLE ref.nj_state_candidate IS
    'Manually-curated reference table of publicly-announced NJ '
    'candidates for major statewide / state-legislative offices. '
    'Every row carries a verifiable source_url + source_doc_date so '
    'analysts can independently confirm the claim against an external '
    'authority (Wikipedia, NJ Division of Elections, NJ ELEC). '
    'Substrate-honest scope: no certified-results claims unless a '
    'result_url is provided. Forward-compat with the planned NJ ELEC '
    'ingester via elec_filing_id; when that ingester lands, '
    'campaign_finance_ingest_pending flips FALSE in the view and the '
    'candidate becomes eligible for donor-graph fraud signals.';

COMMENT ON COLUMN ref.nj_state_candidate.candidate_id IS
    'Format: NJ-STATE-<LAST-NAME-WORDS>-<YEAR>-<UPPER_OFFICE>. Manually '
    'constructed to parallel FEC opaque IDs while staying greppable.';

COMMENT ON COLUMN ref.nj_state_candidate.source_doc_date IS
    'Date the maintainer last verified source_url. UI exposes this so '
    'users see how stale the curated record is relative to live ELEC.';

COMMENT ON COLUMN ref.nj_state_candidate.elec_filing_id IS
    'NJ ELEC candidate filing ID. NULL until the ELEC ingester ships '
    '(Phase F8.5-data). Presence of this column drives the '
    'campaign_finance_ingest_pending badge in the UI.';

COMMENT ON COLUMN ref.nj_state_candidate.primary_winner IS
    'Substrate-honest: stays NULL until either an NJ Division of '
    'Elections certified-results ingest lands OR a human attests with '
    'a primary_result_url citation (CHECK constraint enforces this).';


CREATE OR REPLACE FUNCTION ref._nj_state_candidate_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS nj_state_candidate_updated_at
    ON ref.nj_state_candidate;

CREATE TRIGGER nj_state_candidate_updated_at
BEFORE UPDATE ON ref.nj_state_candidate
FOR EACH ROW
EXECUTE FUNCTION ref._nj_state_candidate_set_updated_at();


-- ----------------------------------------------------------------------------
-- derived.v_nj_state_candidates
--
-- UI-shape view: one row per curated candidate with the
-- campaign_finance_ingest_pending flag and a human-readable office_label.
-- The /risk overview page renders these as a third section: "NJ statewide
-- candidates -- campaign-finance ingest pending." When the ELEC ingester
-- ships and elec_filing_id is populated, the badge flips off automatically.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW derived.v_nj_state_candidates AS
SELECT
    c.candidate_id                                           AS entity_id,
    c.full_name,
    c.party,
    c.office,
    CASE c.office
        WHEN 'governor'             THEN 'Governor of New Jersey'
        WHEN 'lt_governor'          THEN 'Lieutenant Governor of New Jersey'
        WHEN 'attorney_general'     THEN 'Attorney General of New Jersey'
        WHEN 'state_senate'         THEN 'NJ State Senate'
        WHEN 'state_assembly'       THEN 'NJ State Assembly'
        WHEN 'state_supreme_court'  THEN 'NJ Supreme Court'
        ELSE c.office
    END                                                      AS office_label,
    c.election_year,
    c.primary_date,
    c.general_date,
    c.announced_candidate,
    c.announcement_date,
    c.announcement_url,
    c.prior_office,
    c.campaign_committee_name,
    c.primary_winner,
    c.primary_result_url,
    c.general_winner,
    c.general_result_url,
    c.elec_filing_id,
    -- The badge driver: TRUE when the platform has no ELEC ingest for this
    -- row and therefore makes ZERO contribution / expenditure / anomaly
    -- claims about the candidate. Flips FALSE when elec_filing_id arrives.
    (c.elec_filing_id IS NULL)                               AS campaign_finance_ingest_pending,
    c.source_url,
    c.source_authority,
    c.source_doc_date,
    c.notes,
    c.formula_version,
    c.effective_date,
    c.ingested_at,
    c.updated_at
FROM ref.nj_state_candidate c
ORDER BY
    c.election_year DESC,
    -- Surface governor first within a year (UI Section 3 leads with the
    -- highest-profile race), then lt_gov, then AG, then legislature.
    CASE c.office
        WHEN 'governor'             THEN 1
        WHEN 'lt_governor'          THEN 2
        WHEN 'attorney_general'     THEN 3
        WHEN 'state_senate'         THEN 4
        WHEN 'state_assembly'       THEN 5
        WHEN 'state_supreme_court'  THEN 6
        ELSE 9
    END,
    -- Within an office, group by party (D before R alphabetically is the
    -- default; the seed comments call out the intentional ordering).
    c.party,
    c.full_name;

COMMENT ON VIEW derived.v_nj_state_candidates IS
    'UI-shape view over ref.nj_state_candidate. Exposes the '
    'campaign_finance_ingest_pending flag the /risk page renders as the '
    '"campaign-finance ingest pending" badge. Ordering: most recent '
    'election_year first, governor before legislature within a year, '
    'party then full_name within an office. Substrate-honest: no '
    'certified-results columns are populated unless paired with a '
    'verifiable result_url (CHECK constraint on the base table).';


COMMIT;
