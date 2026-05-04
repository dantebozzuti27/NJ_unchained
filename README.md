# NJ Affordability Erosion Platform

> A "Bloomberg Terminal for civic + housing data": a continuously updated,
> vintaged, queryable, alertable single source of truth for New Jersey
> county-level affordability and public-spending integrity.
> See [`idea`](./idea) for the original design document and
> [`work_left.txt`](./work_left.txt) for the BBG-mental-model architecture.

## Mental model

This is not a research notebook, dashboard, or one-off report. The product
goal is that consumers never "run the data refresh"; the data is just
there, with a known release calendar, freshness signals, alertable
thresholds, and a counterfactual engine. Every cell is provenance-tagged
back to the source byte-stream. See `work_left.txt` Tier 0.5 (DAG /
orchestration) and the "PRODUCT MENTAL MODEL" section.

## Status

Rebuilt from the `idea` spec on 2026-04-28 after the prior session's
source files were lost. Current substrate:

* **Tier 4 (civic integrity / fraud) v2.A -- structural fraud
  metrics live:** the `/fraud` terminal now leads with a "Fraud
  metrics" tab driven by **eight derived signals** computed against
  real 2024 FEC data (1,072 multi-committee treasurers, 1,760
  candidates with no PCC, 515 address clusters, 191 committee-name
  collisions, 254 candidate namesakes within cycle, 1,568
  self-treasurer flags, plus broken-PCC and multi-PCC anomalies).
  The full `indiv24.zip` is loaded: **58,210,004 individual
  contributions** in `raw.fec_contribution`. Each metric is a
  `derived.fec_*` view + a single `MetricSpec` registry entry; the
  generic `/fec/metrics`, `/fec/metrics/_summary`, and
  `/fec/metrics/{id}[/csv]` routes serve all of them with filtering,
  pagination, and constant-memory CSV streaming (sort whitelisted
  per metric for SQL-injection defense). The UI Metrics tab is the
  default landing tab, deep-linkable via `/fraud#m=<id>&cycle=<cy>`,
  and shows live flagged-row counts in the sidebar. The housing UI
  at `/` gains a prominent CIVIC INTEGRITY -> cross-link. The
  `/fec/summary` endpoint now caches in-process for 5 minutes so
  the 30-second one-shot count over 58M rows does not block page
  loads. Quality gates: ruff + mypy clean; pytest 395/395 green
  (27 new metric tests).

* **Tier 4 (civic integrity / fraud) v1 + dedicated UI:** **FEC bulk
  ingester + separate fraud terminal at `/fraud`** (both validated
  against real 2024 cycle data 2026-04-29). Three raw tables
  (`raw.fec_candidate`, `raw.fec_committee`, `raw.fec_contribution`)
  with vintage-tagged `(cycle, *_id)` primary keys; five canonical
  views including the headline `public.v_fec_money_to_nj_candidates`
  three-way join. Real-data validation: 9,804 federal candidates
  loaded for 2024 (257 NJ), 20,941 committees (454 NJ-domiciled). PCC
  linkage works (BOOKER, CORY A. -> CORY BOOKER FOR SENATE; KIM, ANDY
  -> ANDY KIM FOR NEW JERSEY). The 4.2 GB `indiv24.zip` (individual
  contributions, ~25M rows) is operator-triggered and streams directly
  from the zip into Postgres COPY without buffering in Python memory.
  Three Dagster SDAs registered with 21d/16d freshness budget, five
  asset checks (row counts, NJ candidate coverage, contribution ->
  committee referential integrity at 95% floor, NJ money visibility).
  Substrate-honesty bug surfaced and pinned: real FEC bulk has
  literal double-quote characters in plain text (`"VAL" VALMA PAUL`)
  so Polars must parse with `quote_char=None`. **Read API**: 11
  filterable JSON endpoints under `/fec/*` (candidates, committees,
  contributions, money-to-NJ, plus distinct-value lookups for
  dropdowns and a cross-table summary) returning a paginated
  `FecPagedResponse` envelope, plus 4 CSV streaming endpoints under
  `/fec/export/*.csv` that use Postgres named cursors so a 100k-row
  export consumes constant Python memory. **UI** at `/fraud`: a
  separate front end (Tabulator + zero-build vanilla JS) with a 5-card
  summary header, 4 tabs (Candidates / Committees / Contributions /
  Money -> NJ), a per-tab filter bar (cycle/state/office/party/ICI/
  status/name + amount + date range + memo flag), virtualized tables,
  click-to-drill side detail panel (renders linked PCC committee +
  recent contributions), and an Export CSV button per tab that
  carries the live filter set into the streaming download. All
  filters server-side; sort whitelisted defense in depth. 42 new
  tests across the serving layer, all green. Roadmap v2-v7 (NJ
  YourMoney, SAM.gov, Senate LDA, NJ ELEC, GLEIF, USAspending)
  stubbed in `work_left.txt`.

* **Tier 0 / Tier 1 / Tier 2:** ~95% complete. Eight ingesters,
  286+ unit tests, real-data validated against DOL, BLS, Census,
  FHFA, FRED, NJ DCA. Headline burden ratio computes end-to-end
  for all 21 NJ counties.
* **Tier 3 (population segmentation):** ~95% complete.
  - **5 years of real Census PUMS data, end-to-end.** The platform
    holds 9 ``(year, product)`` raw materializations spanning 2018-2022:
    1-Year for 2018, 2019, 2021, 2022 (Census skipped 2020 due to
    COVID), and 5-Year for 2018-2022. ~2.5M raw person rows / 1.1M
    raw housing rows. The county-grain derived layer materializes
    all 9 pairs (10,349 cells total) and surfaces a public time-
    series view (``public.v_pums_burden_county_yoy_overall``) with
    YoY deltas and naive-independence SE. Headline finding: NJ
    Bergen renter burden rose +4.7pp 2019->2021 (z=2.09, significant
    at 95% on independent 1-Year samples), then has been flat through
    2022. Real signal in real data, with statistically defensible
    confidence intervals.
  - **Year-portability bugs surfaced and pinned by real bytes.**
    Multi-year ingestion exposed THREE more substrate-honesty bugs
    that synthetic data could not have caught: (i) the YBL->YRBLT
    rename happened in **ACS 2020** 1-Year, not 2019 as commonly
    documented, so 2018-2019 1-Year files still ship YBL; (ii) ACS
    5-Year files for 2018-2020 carry alphanumeric SERIALNO (e.g.,
    ``2018HU0133940``) that Polars's first-1000-row schema inference
    locks in as Int64 and rejects on the first non-numeric row;
    (iii) **2021 1-Year still uses 2010-vintage PUMA boundaries**
    -- Census didn't switch to 2020-vintage until the 2022 file.
    Each became a regression-pinned unit test. The ingester is now
    year-aware: it dispatches PUMA-vintage default on year, detects
    YRBLT-vs-YBL dynamically, and forces Utf8 on identifier columns
    via ``schema_overrides`` before scan.

  - **REAL ACS PUMS data flowing end-to-end.** The platform now
    holds the actual Census-published 2022 NJ PUMS for both
    products: 1-year (93,166 person rows / 41,674 housing rows)
    and 5-year (433,288 / 195,673). All previous analytics ran on
    synthetic data; switching to real bytes surfaced and fixed
    three classes of substrate-honesty bugs that synthetic
    fixtures could not catch (column rename `YBL`->`YRBLT` after
    ACS 2019; multi-vintage PUMA columns `PUMA10`/`PUMA20` in
    5-Year files spanning the 2020 decennial revision; and
    Census's `-9` not-applicable sentinel in the column that
    does not apply to a row). Each became a regression-pinned
    unit test.
  - **Multi-vintage PUMA support, end to end.** Both raw PUMS
    tables carry a `puma_vintage` column (`'2010'` or `'2020'`).
    The ingester detects which decennial-PUMA columns are in the
    upstream CSV (`PUMA`, `PUMA10`, `PUMA20`) and synthesizes a
    canonical `(puma, puma_vintage)` pair. The platform now
    holds **two** PUMA->county crosswalks
    (`ref.puma2020_county_xwalk` and `ref.puma2010_county_xwalk`,
    151 rows total), each with its own invariant view, and the
    county-grain compute layer dispatches each PUMS row to the
    right one by joining on `(state_fips, puma, puma_vintage)`.
    The 2010 crosswalk is derived from Census's authoritative
    `2010_Census_Tract_to_2010_PUMA.txt` relationship file (74K
    nationwide rows; tract-count proxy for population weighting,
    same precision class as the 2020 hand-coded seed).
    **Result:** the previously-dropped 340,122 PUMA10-tagged
    person rows in the 2022 5-Year file (~80% of the sample) now
    flow into county aggregation; sample sizes per county-cell
    grow ~5x and median burden-ratio standard errors halve
    (acs1 median SE = 0.0243 vs acs5 median SE = 0.0121).
    The PUMA-grain table remains 2020-only this session
    (defer dual-vintage PUMA grain; counties already capture
    the analytical payoff, since county FIPS is decennial-stable
    and PUMA codes are not).
  - **Multi-product derived materialization.** The two
    PUMS-derived assets (`pums_burden_segmented`,
    `pums_burden_county_segmented`) iterate over every
    `(year, product)` pair in raw, not just the latest. Both
    `acs1` and `acs5` rows now coexist in derived; the API
    surfaces them via `?product=acs1|acs5` (default `acs5`).
    This closed a substrate-honesty hazard: the previous
    `MAX(year), product LIMIT 1` materialized only one product
    and silently shadowed the other.
  - **`?product=` filter on PUMS endpoints.** `acs5` is the
    default for headline analytics (larger sample, fewer
    suppressed cells); `acs1` is available for fresher data.
    Both share the same response schema -- no client code
    change required to switch.
  - **ACS PUMS person + housing ingester** (`nj-ingest-pums`).
    Pulls 1-year (or 5-year) Public Use Microdata Sample from
    Census's FTP, parses with Polars (column projection +
    replicate-weight folding), bulk-COPYs into
    `raw.acs_pums_person` (~100K rows/year for NJ) and
    `raw.acs_pums_housing` (~40K rows/year). Stores all 80
    replicate weights as `INTEGER[]` for SDR variance estimation.
  - **Two new Dagster raw assets** with annual freshness policy and
    four asset checks (row-count floor, NJ PUMA coverage,
    replicate-weight cardinality = 80, person/housing serialno
    consistency).
  - **`derived.pums_burden_segmented`** -- the Tier 3 analytical
    surface. The platform's first MATERIALIZED derived table.
    Person-level housing burden ratios at PUMA grain, segmented by
    tenure x demographic dimension (race, hispanic origin,
    citizenship, age band, overall). Long-format schema (one row
    per `(puma, year, tenure, segment_dim, segment_value)`) so
    adding a new segment dimension is compute-side only -- no
    schema migration. Suppresses cells with `weighted_n < 1000`
    per ACS disclosure-avoidance practice. Methodology:
    `burden_ratio = median_cost*12 / median_income` (ratio of
    medians, NOT median of ratios -- matches HUD/Census
    methodology and is robust to top-coded incomes).
  - **`/pums-burden`** + **`/pums-burden/{puma}`** API endpoints.
    Filter via `?dim=race|hispanic|citizenship|age_band|overall`
    and `?tenure=renter|owner_w_mtg|owner_no_mtg`. Default
    excludes suppressed cells; opt in via
    `?include_suppressed=true`.
  - **`ref.puma2020_county_xwalk` + `ref.puma2010_county_xwalk`**
    -- two PUMA-to-county population-weighted allocation
    crosswalks, one per decennial vintage. NJ 2020 seed (74
    PUMAs, 76 rows) is sourced from the official TIGER/Line
    2022 PUMA shapefile; NJ 2010 seed (73 PUMAs, 75 rows) is
    derived from Census's authoritative tract-to-PUMA
    relationship file using a tract-count proxy for
    population. Both seeds share the same shape, the same
    multi-county-split invariant (allocation factors per PUMA
    sum to 1.0 within tolerance), and a paired diagnostic view
    (`ref.v_puma_xwalk_invariant_violations`,
    `ref.v_puma2010_xwalk_invariant_violations`) that flags
    any drift. Both views must be empty in steady state -- an
    asset check enforces that on every county materialization.
  - **`derived.pums_burden_county_segmented`** -- COUNTY-grain
    mirror of the segmented PUMA table. Re-aggregated from raw
    PUMS via the dual-vintage allocation path -- NOT rolled up
    from the PUMA-grain table (median-of-medians is statistically
    invalid). Each PUMS person's PWGTP is dispatched to the
    matching crosswalk by `puma_vintage`, then fractionally
    allocated across counties for multi-county PUMAs; the
    weighted percentile is computed across the allocated
    observations and concatenated across vintages before the
    aggregator sees it. County FIPS codes are decennial-stable,
    so 2010-vintage and 2020-vintage rows merge cleanly into
    the same county bucket. Records `n_pumas_contributing` per
    cell counting distinct `(puma, puma_vintage)` pairs (not
    bare codes -- 63 of 73 NJ PUMA10 codes collide with PUMA20
    codes despite covering different geography). Suppression at
    the same `weighted_n < 1000` threshold; for the 5-Year
    product, average `n_pumas_contributing` is ~2x the 1-Year
    average because both vintages contribute to the same county.
  - **`/pums-burden-county`** + **`/pums-burden-county/{county_fips}`**
    API endpoints. Same filter shape as `/pums-burden`. Returns
    `county_name` joined from `ref.county` for human-readable
    output.
  - **Successive Differences Replication standard errors.** Every
    PUMS-derived percentile (income, cost, burden ratio) now
    carries an `*_se` companion column computed via Census's SDR
    methodology over the 80 stored replicate weights:
    `SE = sqrt((4/80) * sum_r (theta_r - theta)^2)`. The ratio SE
    is computed jointly per replicate (NOT delta-method) so the
    covariance between numerator and denominator medians is
    preserved. For multi-county PUMAs each replicate weight is
    multiplied by the PUMA-county allocation factor, so the SE
    correctly captures the additional uncertainty from allocation.
    With SEs in place, every cell is a hypothesis-testable
    statistic: `90% CI = p50 +/- 1.645 * se`. Demographic
    subgroups predictably show wider CIs than overall baselines,
    which is the analytical guard against confusing signal with
    sampling noise.
  - **DOL OFLC LCA** ingester complete (visa-flow component).
  - Remaining: IRS migration (county-to-county flows), ACS B07
    (geographic mobility), school enrollment, ACS 5-year PUMS for
    smaller-county unsuppression.
* **Tier 0.5 (orchestration / DAG):** ~85% complete.
  - **15 software-defined assets**: 9 raw (FRED, BLS CPI, FHFA HPI,
    ACS income, ACS housing, DOL OFLC LCA, NJ DCA property tax,
    ACS PUMS person, ACS PUMS housing) + 6 derived
    (`derived.fred_annual`, `derived.f_acs_mhi_real`,
    `derived.fhfa_hpi_indexed_2000`, `derived.housing_burden_ratio`,
    `derived.pums_burden_segmented`,
    `derived.pums_burden_county_segmented`), each with explicit
    upstream-asset deps so the asset graph mirrors data lineage
    end-to-end. The two PUMS-derived tables are MATERIALIZED (the
    first non-view derived assets); both compute in parallel from
    raw PUMS -- the county table is NOT a roll-up of the PUMA
    table because median-of-medians is statistically invalid.
  - **Event-driven derived refreshes via `AutomationCondition.eager()`**:
    every derived asset auto-fires within ~30s of any upstream
    parent's materialization, instead of being polled by a 6-hour
    cron. Two architectural-contract tests prevent regression
    (no derived asset without an automation condition; no cron
    targeting a derived asset).
  - **8 schedules** for raw assets only (weekly FRED, monthly CPI
    polling window, quarterly FHFA, annual ACS Dec window, monthly
    LCA poll, annual NJ DCA Jan window, annual ACS PUMS Oct window).
    The derived layer is entirely event-driven.
  - **20 AssetChecks**: row-count gates, NJ-21-county coverage
    gates (now including `derived.pums_burden_county_segmented`),
    NJ-PUMA coverage gate, PUMS replicate-weight cardinality
    invariant, PUMS person/housing serialno consistency gate,
    burden-ratio plausibility gates (ACS county + PUMS PUMA + PUMS
    county), PUMS suppression-rate plausibility, **standard-error
    non-negativity invariant (PUMA + county)**. Every raw asset
    has at least one quality gate.
  - **`ref.release_calendar`** table with 8 seeded source schedules.
  - **Freshness sensor** that writes 'warn' rows to
    `governance.dataset_health` for any asset that breaches its
    `FreshnessPolicy.time_window` budget.
  - **Per-asset MaterializeResult metadata** including row counts,
    fetch windows, source vintages, content fingerprints (SHA256
    over view contents). Verified live: a property-tax mutation
    propagates a new fingerprint to `derived.housing_burden_ratio`,
    so no dep edge is decorative.
  - **`docker-compose.yml`** for Postgres + Dagster (web + daemon).
  - End-to-end raw->derived materialization validated through
    Dagster on FRED. AssetCheck validated end-to-end.
* **Tier 3 (population segmentation by visa/nativity):** ~10%
  (LCA loader complete; PUMS pending).
* **Tier 3.5 (tax simulation + counterfactual):** 0%.
* **Tier 4 (civic integrity / fraud):** 0%.
* **Tier 5 (serving / LLM / viz):** ~40% — read API + UI live.
  - **Bundled web UI at `/`** ([http://localhost:8000](http://localhost:8000)
    when running `nj-serve`). Zero-build static HTML/CSS/JS shipped
    inside the `serving` package; Plotly.js loaded from CDN. Three
    interactive views, all driven by the existing API:
    - **Trend** — county + tenure -> burden ratio across all available
      years with 90% CI bands (1.645 * SDR-replicate-weight SE);
      HUD cost-burdened (0.30) and severely-burdened (0.50)
      reference lines drawn.
    - **Compare** — year + tenure -> all 21 NJ counties ranked by
      burden ratio, color-graded by HUD bin, horizontal-bar error
      marks for 90% CIs.
    - **Segments** — county + tenure -> burden ratio by race / hispanic
      origin / age band / citizenship for the latest published year,
      with sample_n and weighted_n on hover.
    The UI is non-blocking: empty data renders a clear status message
    rather than a chart frame, so it stays useful before the derived
    table is materialized.
  - **FastAPI read API** at `nj-serve` with **12 v0 endpoints**:
    `/health`, `/releases`, `/assets`, `/assets/{schema}/{table}`,
    `/burden`, `/burden/{county_fips}`,
    `/pums-burden`, `/pums-burden/{puma}`,
    `/pums-burden-county`, `/pums-burden-county/{county_fips}`,
    `/pums-burden-county-series` (multi-year, drives the Trend view),
    `/counties` (NJ county directory for UI dropdowns).
  - `/burden` rows now include **NJ DCA property-tax context columns**
    (`property_tax_amount_avg`, `property_tax_effective_rate_pct`,
    `property_tax_share_of_income`, `property_tax_share_of_owner_cost_w_mtg`).
    For Bergen 2022, `property_tax_share_of_owner_cost_w_mtg = 0.3151`
    — i.e. 31.5% of the median monthly cost of owning a home in
    Bergen is property tax. (Note: ACS B25088/B25089 already include
    property tax in owner cost; the burden RATIOS already account
    for it. The new columns are informational/transparency, not a
    re-computation. See migration 032 for the analytic rationale.)
  - **Pooled Postgres connections** via `psycopg-pool` so the API
    can serve many concurrent reads without per-request connection
    setup overhead.
  - **Pydantic v2 response models** (every endpoint returns a typed
    BaseModel; OpenAPI schema is the contract).
  - **`governance.v_latest_materialization`** + **`v_dataset_health_summary`**
    views: the BBG-style "what is fresh, what is stale, what has
    been wrong recently" surface, computed in SQL.
  - **Observability headers** (X-Request-Id, X-Query-Time-Ms) on
    every response.
  - End-to-end Dagster -> Postgres -> FastAPI flow validated:
    materializing `raw.fred_observation` through Dagster, then
    `GET /assets/raw/fred_observation` correctly reports
    `freshness_state="fresh", age_hours=0.0, last_rows_upserted=79`
    with the per-series materialization payload.
  - Remaining: LLM/SQL-DSL surface, alert engine, what-if scenarios
    (see work_left.txt BBG-LIKE-1..7).

## Quick start

### Run the API + bundled UI

```bash
export PG_DSN=postgresql://postgres:ci@localhost:5432/nj
nj-serve            # starts uvicorn on 0.0.0.0:8000
# Open http://localhost:8000        --> bundled web UI
# Open http://localhost:8000/docs   --> auto-generated Swagger UI
```

The UI is pure HTML/CSS/JS shipped inside the `serving` package; no
Node, npm, or build step is required. Plotly.js is loaded from
[cdn.plot.ly](https://cdn.plot.ly). All charts read from the existing
JSON endpoints, so the UI and the API stay in lockstep automatically.

### Bootstrap the database

```bash
make install                                    # one-time
export PG_DSN=postgresql://localhost/nj_dev     # your dev Postgres
make migrate                                    # apply all .sql migrations
make seed                                       # load reference seeds
make check                                      # lint + typecheck + test
```

### End-to-end: real FEC bulk load (validated 2026-04-29)

The FEC bulk ingester (`nj-ingest-fec`) loads federal campaign-finance
data for one election cycle. Three file kinds:

* `cn{yy}.zip` — Candidate Master (~350 KB)
* `cm{yy}.zip` — Committee Master (~880 KB)
* `indiv{yy}.zip` — Individual Contributions (multi-GB; ~4.2 GB for 2024)

```bash
export PG_DSN=postgresql://postgres:ci@localhost:5432/nj
nj-ingest-fec fetch --cycle 2024 --files cn,cm     # download (cached on disk)
nj-ingest-fec load  --cycle 2024 --files cn,cm     # COPY into raw.fec_*

# Optional: full national contributions (~25M rows, 10-15 min, 30 GB free disk)
nj-ingest-fec fetch --cycle 2024 --files indiv
nj-ingest-fec load  --cycle 2024 --files cn,cm,indiv
```

`fetch` is HTTP conditional (`If-Modified-Since` via Content-Length probe);
`load` is `DELETE WHERE cycle=... + COPY FROM STDIN`, idempotent for
re-runs of the same cycle. The `indiv` path streams directly from the
ZIP into `psycopg.Cursor.copy()` with no Python-side buffering; memory
stays flat regardless of file size.

After loading, query the canonical views:

```sql
-- All NJ federal candidates for the 2024 cycle (257 rows)
SELECT cand_office, count(*)
FROM   public.v_fec_nj_candidates
WHERE  cycle = '2024'
GROUP  BY 1;

-- Headline civic-integrity view (joins contrib + committee + NJ candidate)
SELECT cand_name, sum(transaction_amount) AS total_raised
FROM   public.v_fec_money_to_nj_candidates
WHERE  cycle = '2024' AND NOT is_memo
GROUP  BY cand_name
ORDER  BY total_raised DESC NULLS LAST
LIMIT  20;
```

### Fraud / civic-integrity UI at `/fraud` (validated 2026-04-29)

Once `nj-ingest-fec load` has populated `raw.fec_*`, the read API and the
dedicated terminal at `/fraud` are usable. Both share the housing app's
FastAPI process and Postgres pool — `nj-serve` exposes them simultaneously.

```bash
export PG_DSN=postgresql://postgres:ci@localhost:5432/nj
nj-serve --host 127.0.0.1 --port 8765
```

Open the **fraud terminal** at <http://127.0.0.1:8765/fraud>. The default
landing tab is **Fraud metrics**: an analyst's view of eight derived
fraud-detection signals, each with description, threshold guidance, a
sortable Tabulator of flagged entities, and a CSV export button.
Available signals (Tier A — structural, computable from cn/cm only):

| Signal id                    | What it flags |
|------------------------------|---------------|
| `treasurer_concentration`    | Treasurer name on multiple committees in one cycle (>=15 = lead) |
| `candidate_no_pcc`           | Candidate without declared Principal Campaign Committee |
| `candidate_broken_pcc`       | Candidate's `cand_pcc` not present in `raw.fec_committee` |
| `candidate_multiple_pccs`    | More than one P-designated committee for one candidate |
| `committee_address_clusters` | >=3 committees registered at the same canonical street address |
| `committee_name_collisions`  | Identical canonical name across distinct `cmte_id`s |
| `candidate_namesakes`        | Same canonical name across distinct `cand_id`s, same office/state |
| `treasurer_is_candidate`     | Self-treasurer (collapses audit chain) |

Deep-link any signal at any cycle:

```
/fraud#m=treasurer_concentration&cycle=2024
/fraud#m=committee_address_clusters&cycle=2024
```

Other tabs: Candidates / Committees / Contributions / Money → NJ.
Each has a filter bar (cycle, state, office, party, ICI, status, amount
range, date range, name/employer/occupation contains, memo flag), a
sortable virtualized Tabulator, a click-to-drill side detail panel
that fetches the candidate/committee detail endpoint and renders
linked PCC + 25 most recent contributions, and an Export CSV button
per tab that streams the current filter set.

The housing UI remains at <http://127.0.0.1:8765/> with a CIVIC
INTEGRITY → cross-link in its top-right nav.

The underlying read API speaks JSON at `/fec/*`:

```bash
# Cross-table snapshot (cached 5 min in-process)
curl -s http://127.0.0.1:8765/fec/summary | python -m json.tool

# Fraud-metrics catalog (8 signal entries)
curl -s http://127.0.0.1:8765/fec/metrics | python -m json.tool

# Total flagged-row counts per signal (one cycle)
curl -s 'http://127.0.0.1:8765/fec/metrics/_summary?cycle=2024' | python -m json.tool

# Paginated flagged rows for one signal
curl -s 'http://127.0.0.1:8765/fec/metrics/treasurer_concentration?cycle=2024&limit=20' \
    | python -m json.tool

# Streaming CSV export of a metric (constant memory)
curl -s 'http://127.0.0.1:8765/fec/metrics/committee_address_clusters/csv?cycle=2024' \
    -o address_clusters_2024.csv

# Filterable + paginated list (NJ Senate field, 2024)
curl -s 'http://127.0.0.1:8765/fec/candidates?state=NJ&office=S' | python -m json.tool

# Single-candidate drill-down with linked committees
curl -s http://127.0.0.1:8765/fec/candidates/S4NJ00185 | python -m json.tool
```

Full OpenAPI schema at <http://127.0.0.1:8765/docs>; 15+ JSON endpoints +
5 CSV streaming endpoints under `/fec/*` (the metric `/csv` is added on
top of the original 4 list-table exports). All filters are server-side
parameterized (no string interpolation); sort columns are whitelisted
per endpoint and per metric.

### End-to-end: real LCA load (validated 2026-04-28)

The DOL OFLC LCA loader has been exercised against the 133-MiB FY2024 Q3
disclosure file (216,470 rows). To reproduce:

```bash
docker run --name nj_pg --rm -e POSTGRES_PASSWORD=ci -e POSTGRES_DB=nj \
    -p 5432:5432 -d postgres:16
export PG_DSN=postgresql://postgres:ci@localhost:5432/nj
nj-migrate apply  --dsn $PG_DSN
nj-migrate seed   --dsn $PG_DSN

nj-ingest-lca fetch --fiscal-year 2024 --fiscal-quarter 3
nj-ingest-lca load  --dsn $PG_DSN \
    data/manual/dol_oflc_lca/LCA_Disclosure_Data_FY2024_Q3.xlsx

# Yields (NJ CERTIFIED H-1B, FY2024 Q3, n=12,108):
#   p25 = $82,500   p50 = $103,730   p75 = $121,285
```

The `derived.lca_wage_by_county_yr_visa` aggregator additionally requires a
HUD ZIP-County crosswalk file. HUD's huduser.gov requires a registered
account; see `work_left.txt` for the operator-staging instructions.

### Tier 2 substrate: real-dollar income (validated 2026-04-28)

```bash
nj-ingest-cpi        load --start-year 2010 --end-year 2024
nj-ingest-acs-income load --start-year 2010 --end-year 2022 --product both

# Then in psql:
#   SELECT * FROM derived.f_acs_mhi_real(2022::SMALLINT)
#     WHERE county_fips = '34003' ORDER BY year;
# yields Bergen County's nominal-vs-real income trajectory.
#
# Or:
#   SELECT * FROM public.v_acs_mhi_nj_5yr ORDER BY year, county_name;
# for the human-readable NJ panel.
```

Real result: Hudson +17%, Hunterdon -2%, Cumberland -8% real income
2010 -> 2022 (CPI-deflated). The platform's deflator function joins ACS
to CPI cleanly via SQL.

### Tier 2 substrate: house prices and mortgage rates (validated 2026-04-28)

```bash
# FHFA all-transactions county HPI (50 years x ~3,000 counties, ~5 MiB Excel)
nj-ingest-fhfa fetch
nj-ingest-fhfa load data/manual/fhfa_hpi/hpi_at_county.xlsx

# FRED rate panel (mortgage 30-yr, 10-yr Treasury, fed funds)
nj-ingest-fred load --start-date 2010-01-01 --end-date 2025-12-31
```

Real NJ house-price growth 2010 -> 2024 (FHFA HPI, indexed to 2010=100):
- Cape May +99.6%, Hudson +96.7%, Ocean +89.8%  (shore + urban doubled)
- Hunterdon +51.3%, Morris +57.2%               (commuter belt slowest)

Real FRED rate trajectory 2021 -> 2024:
- 30-yr mortgage:   2.96% -> 6.72%   (P+I on $500K = $2,103 -> $3,237/mo,
                                       a +54% jump from rate alone)
- Fed funds:        0.08% -> 5.14%

Together these power the burden-ratio counterfactuals
("what would my burden be if I had bought in 2021 vs today?").

### ACS housing-cost numerator

```bash
nj-ingest-acs-housing load --start-year 2010 --end-year 2022 --product both
```

Pulls the canonical 7-variable housing batch (B25064, B25077, B25088,
B25003) into ``raw.acs_housing``. The view ``derived.housing_burden_ratio``
joins it to ACS B19013 to produce per-tenure burden ratios, plus a
tenure-weighted blended ratio, per (county, year, product).

NB: Census API was experiencing widespread 503 outages on the validation
day (2026-04-28); the ingester implements 4-attempt exponential-backoff
retry. Live load can be re-run when the API recovers; logic is fully
unit-tested via mocked responses.

### Live-Postgres tests

```bash
docker run --name nj_pg_test --rm -e POSTGRES_PASSWORD=ci \
    -e POSTGRES_DB=nj_test -p 5432:5432 -d postgres:16
export PG_TEST_DSN=postgresql://postgres:ci@localhost:5432/nj_test
pytest -q                                       # 180 tests, ~2s
```

### Local orchestration + serving stack

```bash
# Bring up Postgres + Dagster (web + daemon) + the read API.
docker compose up -d

# Dagster UI:           http://localhost:3000
# Read API + OpenAPI:   http://localhost:8000/docs
#
# - Asset graph at /3000 shows 11 assets (7 raw + 4 derived) with
#   explicit lineage edges and FreshnessPolicy.time_window budgets.
# - Schedules: fred_weekly (Thu 12:30 ET), bls_cpi_monthly_window
#   (10:00-15:00 ET), fhfa_hpi_quarterly (28th Feb/May/Aug/Nov),
#   acs_*_annual_window (Dec 5-19), lca_monthly_poll (15th of month),
#   nj_dca_january_window (Jan 8-22), derived_refresh_6h.

# Materialize one asset on demand (no daemon required):
PG_DSN=postgresql://postgres:ci@localhost:5432/nj python -c "
from dagster import materialize
from orchestration.assets import raw_fred_observation
from orchestration.resources import GovernanceWriter, PgResource
import os
pg = PgResource(dsn=os.environ['PG_DSN'])
materialize([raw_fred_observation], resources={'pg': pg, 'governance': GovernanceWriter(pg=pg)})
"
```

Adding a new source to the DAG:

1. Add a SQL migration / seed for the table.
2. Write the ingester in `ingestion/<source>.py` (CLI optional).
3. Define `@asset` in `orchestration/assets.py`, append to `ALL_ASSETS`.
4. Add a row to `db/seeds/003_release_calendar.sql`.
5. Add a `ScheduleDefinition` in `orchestration/schedules.py`.
6. Add at least one `AssetCheck` in `orchestration/asset_checks.py`.

### Read API examples

```bash
# Service liveness + recent error count
curl http://localhost:8000/health

# Per-source publication calendar (the BBG ECO<GO> pane)
curl http://localhost:8000/releases | jq

# All datasets with freshness state and 30-day signal counts
curl http://localhost:8000/assets | jq

# One asset's detail (last materialization payload, per-series counts)
curl http://localhost:8000/assets/raw/fred_observation | jq

# Headline metric: latest year, all 21 NJ counties
curl http://localhost:8000/burden | jq

# Time series for one county
curl http://localhost:8000/burden/34003 | jq   # Bergen County
```

## Layout

```
db/migrations/      Numbered, idempotent SQL migrations.
db/seeds/           One-shot reference data loads (NJ county FIPS, etc.).
ingestion/          One module per data source. Stateless, idempotent loaders.
                      _base.py               shared canonicalization + fingerprinting
                      dol_oflc_lca.py        POP-2 LCA worksite-level ingester
                      hud_zip_county.py      HUD USPS ZIP-county crosswalk
                      bls_cpi.py             CPI-U deflator (BLS API v2)
                      census_acs_income.py   B19013 median household income
                      census_acs_housing.py  B250xx housing-cost numerator (multi-var)
                      fhfa_hpi.py            FHFA county HPI (repeat-sales)
                      fred_mortgage_rates.py FRED rate panel (mortgage/Treasury/fed funds)
derived/            Computed metrics with formula_version + provenance.
scripts/            CLI entrypoints (migrate runner, smoke tests).
tests/              Pytest suite. Tests requiring a live Postgres are marked
                      with @pytest.mark.live_pg and skipped if PG_TEST_DSN
                      is not set.
data/               Local-only download / staging area (gitignored).
sources_manifest.toml   Single source of truth for data source URLs, vintages,
                          and license terms.
work_left.txt       Prioritized backlog with rationale for sequencing.
idea                Project design spec. READ ONLY.
```

## Methodological invariants (non-negotiable)

These are enforced in code, not just in documentation:

1. **`raw.*` is loaded from sources unmodified.** Any normalization, currency
   conversion, or aggregation lives in `derived.*` with a `formula_version`
   stamp and a `source_vintage_hash` fingerprint. Reproducibility means a
   given `(formula_version, source_vintage_hash)` always produces the same
   `derived.*` row.
2. **Every measured row carries a `data_quality` of `'measured'`,
   `'computed'`, or `'modeled'`.** Modeled rows are never displayed to the
   user without that label.
3. **Suppression invariants are CHECK constraints, not application logic.**
   The database refuses to store a sample-thinned percentile.
4. **Citizenship/nativity decomposition (TIER 3.5 / POP-N)** uses ACS and DOL
   public data only. Unauthorized estimates are state-level only and tagged
   `data_quality = 'modeled'`.
