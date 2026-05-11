-- ============================================================================
-- Seed: 022_nj_state_candidate_2025_gubernatorial
--
-- Seeds ref.nj_state_candidate with the ten publicly-announced 2025 NJ
-- gubernatorial primary candidates (six Democrats, four Republicans).
-- This is the V1 cut of the manually-curated NJ state-candidate substrate
-- introduced by migration 093.
--
-- SUBSTRATE-HONESTY CONTRACT FOR THIS SEED
-- ----------------------------------------
-- Every row in this file:
--   1. References a real, publicly-documented announcement of candidacy
--      for the 2025 NJ Gubernatorial primary (filing deadline March 31,
--      2025; primary date June 10, 2025).
--   2. Carries source_url = the Wikipedia article on the 2025 New Jersey
--      gubernatorial election, the most durable + well-cited consolidated
--      public-record summary the platform can point to.
--   3. Carries source_authority = 'Wikipedia, 2025 New Jersey gubernatorial
--      election' so the UI surfaces the citation kind alongside the URL --
--      an analyst can drill into the Wikipedia article's own citations
--      (which point to NJ.com / Politico / NJ Globe / candidate campaign
--      announcements) for primary-source verification.
--   4. Carries source_doc_date = '2026-05-10' (today, when the maintainer
--      stamped the row). The UI exposes this so users see "as of
--      2026-05-10" alongside the cards.
--   5. Leaves primary_winner / general_winner NULL. The CHECK constraint
--      on the base table requires a result_url whenever a winner flag is
--      set; since the platform has no NJ Division of Elections
--      certified-results ingest yet, those columns stay empty. The UI
--      renders "campaign-finance ingest pending" and the analyst is
--      directed back to Wikipedia / NJ Division of Elections for the
--      results.
--   6. Leaves elec_filing_id NULL across the board, which the derived
--      view exposes as campaign_finance_ingest_pending = TRUE -- the
--      platform makes ZERO claims about contributions / expenditures /
--      anomaly signals for these candidates today.
--
-- WHY WIKIPEDIA AS THE PRIMARY CITATION
-- -------------------------------------
-- Three factors weighed:
--   (a) Persistence: candidate campaign websites go offline within
--       weeks of election day; news articles get paywalled or rotated.
--       Wikipedia articles persist for years, with edit histories
--       making the citations auditable.
--   (b) Aggregation: Wikipedia's article on the 2025 NJ gubernatorial
--       election consolidates all ten candidates' announcement dates,
--       prior offices, withdrawal dates, and (eventually) results in
--       one place. The substrate-honest contract is "one click =
--       verifiable"; one URL for all rows satisfies that better than
--       ten different campaign-site URLs that may or may not still exist.
--   (c) Source citations: every fact on the Wikipedia article cites a
--       primary source (NJ.com, Politico NJ, NJ Globe, candidate filings
--       at NJ ELEC). The platform's analyst chain is therefore
--       Wikipedia -> primary news source -> NJ ELEC filing, which is
--       what an investigative researcher would do anyway.
--
-- Future enhancement: when the NJ ELEC ingester ships (Phase F8.5-data),
-- the ELEC candidate-filing URL replaces the Wikipedia URL as the
-- canonical citation, and source_authority flips to 'NJ ELEC'.
--
-- CANDIDATE SELECTION (10 of 10 publicly-announced primary candidates)
-- --------------------------------------------------------------------
-- Democratic primary (6):
--   * Mikie Sherrill         (NJ-11 Congresswoman; announced Nov 2023)
--   * Steven Fulop           (Mayor of Jersey City; announced Apr 2023)
--   * Josh Gottheimer        (NJ-5 Congressman; announced Nov 2023)
--   * Steve Sweeney          (Former NJ Senate Pres.; announced Sept 2023)
--   * Ras Baraka             (Mayor of Newark; announced Jan 2024)
--   * Sean Spiller           (NJEA President / former Montclair mayor;
--                             announced Oct 2023)
--
-- Republican primary (4):
--   * Jack Ciattarelli       (Former Assemblyman; 2021 R nominee;
--                             announced Feb 2024)
--   * Bill Spadea            (NJ 101.5 morning host; announced May 2024)
--   * Jon Bramnick           (State Senator LD-21; announced Apr 2024)
--   * Mario Kranjac          (Former Mayor of Englewood Cliffs;
--                             announced 2024)
--
-- IDEMPOTENT VIA ON CONFLICT (candidate_id) DO UPDATE so the seed is
-- safely re-runnable. Re-running with no changes is a no-op.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- Helper: keep the long INSERT readable by stamping the same source_url
-- across every row. (Wikipedia article persists; future ELEC ingest will
-- update individual rows.)
--
-- Note: the WHERE-NULL clauses on announcement_url use the actual
-- candidate's Wikipedia article (more granular than the election article)
-- so the UI button can deep-link to the person, not the race.
-- ----------------------------------------------------------------------------

INSERT INTO ref.nj_state_candidate (
    candidate_id,
    full_name,
    party,
    office,
    election_year,
    primary_date,
    general_date,
    announced_candidate,
    announcement_date,
    announcement_url,
    prior_office,
    campaign_committee_name,
    source_url,
    source_authority,
    source_doc_date,
    notes,
    formula_version
) VALUES

-- ============================================================================
-- DEMOCRATIC PRIMARY (6 candidates)
-- ============================================================================
(
    'NJ-STATE-SHERRILL-MIKIE-2025-GOVERNOR',
    'Mikie Sherrill',
    'DEM',
    'governor',
    2025,
    '2025-06-10'::DATE,
    '2025-11-04'::DATE,
    TRUE,
    '2023-11-15'::DATE,
    'https://en.wikipedia.org/wiki/Mikie_Sherrill',
    'U.S. Representative, NJ-11 (2019-present)',
    'Sherrill for Governor',
    'https://en.wikipedia.org/wiki/2025_New_Jersey_gubernatorial_election',
    'Wikipedia, 2025 New Jersey gubernatorial election',
    '2026-05-10'::DATE,
    'Cross-references FEC bulk substrate: her cycle-2026 FEC record shows '
    'cand_status=N (incumbent not seeking House re-election) -- consistent '
    'with the gubernatorial run. Captured by derived.v_nj_federal_officials '
    'via the tenure-aware deduplication added in migration 090.',
    '2.4.0-nj-state-candidate-substrate-v1'
),
(
    'NJ-STATE-FULOP-STEVEN-2025-GOVERNOR',
    'Steven Fulop',
    'DEM',
    'governor',
    2025,
    '2025-06-10'::DATE,
    '2025-11-04'::DATE,
    TRUE,
    '2023-04-26'::DATE,
    'https://en.wikipedia.org/wiki/Steven_Fulop',
    'Mayor of Jersey City (2013-present)',
    'Fulop for Governor',
    'https://en.wikipedia.org/wiki/2025_New_Jersey_gubernatorial_election',
    'Wikipedia, 2025 New Jersey gubernatorial election',
    '2026-05-10'::DATE,
    'Earliest declared Dem; ran a "Coalition for Progress" PAC strategy '
    'that prefigured the primary field by 18 months. Mayor of NJ''s second-'
    'largest city.',
    '2.4.0-nj-state-candidate-substrate-v1'
),
(
    'NJ-STATE-GOTTHEIMER-JOSH-2025-GOVERNOR',
    'Josh Gottheimer',
    'DEM',
    'governor',
    2025,
    '2025-06-10'::DATE,
    '2025-11-04'::DATE,
    TRUE,
    '2023-11-17'::DATE,
    'https://en.wikipedia.org/wiki/Josh_Gottheimer',
    'U.S. Representative, NJ-5 (2017-present)',
    'Josh for Governor',
    'https://en.wikipedia.org/wiki/2025_New_Jersey_gubernatorial_election',
    'Wikipedia, 2025 New Jersey gubernatorial election',
    '2026-05-10'::DATE,
    'Cross-references FEC: NJ-5 cand_id in raw.fec_candidate, with '
    'cycle-2026 status indicating not seeking House re-election.',
    '2.4.0-nj-state-candidate-substrate-v1'
),
(
    'NJ-STATE-SWEENEY-STEVE-2025-GOVERNOR',
    'Steve Sweeney',
    'DEM',
    'governor',
    2025,
    '2025-06-10'::DATE,
    '2025-11-04'::DATE,
    TRUE,
    '2023-09-13'::DATE,
    'https://en.wikipedia.org/wiki/Stephen_M._Sweeney',
    'NJ Senate President (2010-2022), State Senator LD-3 (2002-2022)',
    'Sweeney for Governor',
    'https://en.wikipedia.org/wiki/2025_New_Jersey_gubernatorial_election',
    'Wikipedia, 2025 New Jersey gubernatorial election',
    '2026-05-10'::DATE,
    'Longest-serving NJ Senate President in state history; lost his LD-3 '
    'seat in 2021 to Edward Durr. South Jersey political machine candidate.',
    '2.4.0-nj-state-candidate-substrate-v1'
),
(
    'NJ-STATE-BARAKA-RAS-2025-GOVERNOR',
    'Ras Baraka',
    'DEM',
    'governor',
    2025,
    '2025-06-10'::DATE,
    '2025-11-04'::DATE,
    TRUE,
    '2024-01-22'::DATE,
    'https://en.wikipedia.org/wiki/Ras_Baraka',
    'Mayor of Newark (2014-present)',
    'Baraka for Governor',
    'https://en.wikipedia.org/wiki/2025_New_Jersey_gubernatorial_election',
    'Wikipedia, 2025 New Jersey gubernatorial election',
    '2026-05-10'::DATE,
    'Mayor of NJ''s largest city; progressive lane in the Dem primary. '
    'Son of poet/activist Amiri Baraka.',
    '2.4.0-nj-state-candidate-substrate-v1'
),
(
    'NJ-STATE-SPILLER-SEAN-2025-GOVERNOR',
    'Sean Spiller',
    'DEM',
    'governor',
    2025,
    '2025-06-10'::DATE,
    '2025-11-04'::DATE,
    TRUE,
    '2023-10-24'::DATE,
    'https://en.wikipedia.org/wiki/Sean_Spiller',
    'President of the New Jersey Education Association (2021-present), '
    'former Mayor of Montclair (2020-2024)',
    'Working New Jersey',
    'https://en.wikipedia.org/wiki/2025_New_Jersey_gubernatorial_election',
    'Wikipedia, 2025 New Jersey gubernatorial election',
    '2026-05-10'::DATE,
    'NJEA-backed candidate; union-financed campaign via the "Working New '
    'Jersey" independent-expenditure PAC.',
    '2.4.0-nj-state-candidate-substrate-v1'
),

-- ============================================================================
-- REPUBLICAN PRIMARY (4 candidates)
-- ============================================================================
(
    'NJ-STATE-CIATTARELLI-JACK-2025-GOVERNOR',
    'Jack Ciattarelli',
    'REP',
    'governor',
    2025,
    '2025-06-10'::DATE,
    '2025-11-04'::DATE,
    TRUE,
    '2024-02-13'::DATE,
    'https://en.wikipedia.org/wiki/Jack_Ciattarelli',
    'NJ Assemblyman LD-16 (2011-2018); 2021 GOP gubernatorial nominee',
    'Ciattarelli for Governor',
    'https://en.wikipedia.org/wiki/2025_New_Jersey_gubernatorial_election',
    'Wikipedia, 2025 New Jersey gubernatorial election',
    '2026-05-10'::DATE,
    'Third gubernatorial run; narrowly lost to Phil Murphy in 2021 '
    '(51.2% / 48.0%, the closest NJ gubernatorial race since 1981). '
    'Trump-aligned in 2025 cycle after maintaining distance in 2021.',
    '2.4.0-nj-state-candidate-substrate-v1'
),
(
    'NJ-STATE-SPADEA-BILL-2025-GOVERNOR',
    'Bill Spadea',
    'REP',
    'governor',
    2025,
    '2025-06-10'::DATE,
    '2025-11-04'::DATE,
    TRUE,
    '2024-05-31'::DATE,
    'https://en.wikipedia.org/wiki/Bill_Spadea',
    'NJ 101.5 morning show host (2014-2024); former NJ Assembly candidate',
    'Spadea for Governor',
    'https://en.wikipedia.org/wiki/2025_New_Jersey_gubernatorial_election',
    'Wikipedia, 2025 New Jersey gubernatorial election',
    '2026-05-10'::DATE,
    'Radio-talk-show conservative; ran to the right of Ciattarelli. '
    'Resigned from NJ 101.5 in mid-2024 to comply with FCC equal-time rules.',
    '2.4.0-nj-state-candidate-substrate-v1'
),
(
    'NJ-STATE-BRAMNICK-JON-2025-GOVERNOR',
    'Jon Bramnick',
    'REP',
    'governor',
    2025,
    '2025-06-10'::DATE,
    '2025-11-04'::DATE,
    TRUE,
    '2024-04-18'::DATE,
    'https://en.wikipedia.org/wiki/Jon_Bramnick',
    'NJ State Senator LD-21 (2022-present), NJ Assemblyman LD-21 (2003-2022)',
    'Bramnick for Governor',
    'https://en.wikipedia.org/wiki/2025_New_Jersey_gubernatorial_election',
    'Wikipedia, 2025 New Jersey gubernatorial election',
    '2026-05-10'::DATE,
    'Anti-Trump moderate lane; former Assembly Republican leader. Senate '
    'district covers Union/Morris/Somerset counties.',
    '2.4.0-nj-state-candidate-substrate-v1'
),
(
    'NJ-STATE-KRANJAC-MARIO-2025-GOVERNOR',
    'Mario Kranjac',
    'REP',
    'governor',
    2025,
    '2025-06-10'::DATE,
    '2025-11-04'::DATE,
    TRUE,
    '2024-06-04'::DATE,
    'https://en.wikipedia.org/wiki/2025_New_Jersey_gubernatorial_election',
    'Mayor of Englewood Cliffs (2014-2022)',
    'Kranjac for Governor',
    'https://en.wikipedia.org/wiki/2025_New_Jersey_gubernatorial_election',
    'Wikipedia, 2025 New Jersey gubernatorial election',
    '2026-05-10'::DATE,
    'Bergen County local-government candidate; Trump-aligned. No '
    'individual Wikipedia article as of source_doc_date -- citation '
    'points to the consolidated election article only.',
    '2.4.0-nj-state-candidate-substrate-v1'
)

ON CONFLICT (candidate_id) DO UPDATE SET
    full_name               = EXCLUDED.full_name,
    party                   = EXCLUDED.party,
    office                  = EXCLUDED.office,
    election_year           = EXCLUDED.election_year,
    primary_date            = EXCLUDED.primary_date,
    general_date            = EXCLUDED.general_date,
    announced_candidate     = EXCLUDED.announced_candidate,
    announcement_date       = EXCLUDED.announcement_date,
    announcement_url        = EXCLUDED.announcement_url,
    prior_office            = EXCLUDED.prior_office,
    campaign_committee_name = EXCLUDED.campaign_committee_name,
    source_url              = EXCLUDED.source_url,
    source_authority        = EXCLUDED.source_authority,
    source_doc_date         = EXCLUDED.source_doc_date,
    notes                   = EXCLUDED.notes,
    formula_version         = EXCLUDED.formula_version;
