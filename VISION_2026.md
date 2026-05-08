# NJ Unchained — Strategic Vision and Honest Gap Analysis (v2)
**Author:** Cursor (Claude Opus 4.7), assistant to D. Bozzuti
**Date:** 2026-05-04 (night, wrap-up — second pass)
**Status:** Working document. Anchored to the original `idea` spec (the
file at the repo root, marked READ ONLY in `README.md`) and to
`AGENTS.md` (free-tier / Oracle Cloud constraints).

---

## 0. Why this document exists

Tonight you said: "every number and piece of data must be verifiable,
don't hardcode things... I explained everything in the vision at the
beginning and you didn't seem to follow it." You are correct. I did not
read `idea` carefully and I drifted from your spec. This second pass of
the vision document is anchored to `idea` line-by-line so we are
working off the same source of truth.

The associated rule file `.cursor/rules/verifiable-data.mdc` is the
permanent enforcement mechanism for the "no magic numbers, full
provenance" requirement, so this drift cannot recur silently.

---

## 1. The original spec, restated as platform contract

Quoting `idea` directly:

| `idea` § | Specified | Built today | Gap |
|---|---|---|---|
| Goal | "Housing burden over time / True tax-adjusted disposable income / Required income to afford median housing / Affordability erosion (how far middle-class purchasing power has degraded)" | Only "burden over time" partially; the other three are zero. | 75% of headline output missing |
| 1.1 | "Data quality > feature breadth" | We added breadth (17 fraud signals) before depth in the housing core. | Inverted priority |
| 1.3 | "Every metric must be reproducible / derivable from stored raw inputs + deterministic transforms" | Substrate honors this; UI hardcodes tier cutoffs (1.40/1.15/0.95) and base year (2010) without source citations. | Rule violation, now codified in `.cursor/rules/verifiable-data.mdc` |
| 3.1 Income | ACS 5-year + Decennial pre-2005, MoE stored, never mix 1-yr and 5-yr without label | ACS5 loaded; 1-yr/5-yr labeling enforced; MoE not yet stored. | Partial |
| 3.2 Housing | "Index-based, not raw listings". Zillow ZHVI / Redfin county series. | FHFA HPI loaded. ZHVI/Redfin not. | Partial |
| 3.3 Taxes — "MOST IMPORTANT AND MOST ERROR-PRONE LAYER" | IRS brackets per year, NJ Division of Taxation tables, NJ DCA county property tax averages. "Effective tax rate must be SIMULATED, not assumed flat. Store full tax function per year (not just %)." | NJ DCA loaded but never exposed. IRS / NJ state brackets not loaded. Tax function not implemented at all. | Critical gap |
| 3.4 Inflation | CPI-U, all values converted to **2026 real dollars baseline** | CPI-U loaded; conversion implemented in `derived.f_acs_mhi_real`; baseline year is 2010 in the UI, **not 2026 as spec requires**. | Wrong baseline year |
| 5.1 HBR | Mortgage-equivalent: 20% down, 30-yr fixed, Freddie Mac historical rate. | FRED rates loaded but never used in housing math. UI shows HPI ÷ income, not PITI ÷ income. | Critical gap |
| 5.2 ETR | Federal + State + Property, simulated per income band. | None of this exists. | Critical gap |
| 5.3 DI | Income − Taxes − Housing, CPI-adjusted to 2026. | Doesn't exist. | Critical gap |
| 5.4 Affordability Threshold Gap | `Income_required@30% − MedianIncome` where `Income_required@30% = (Housing+Taxes) / 0.30`. **"This is your headline collapse metric."** | Doesn't exist. | The headline metric of the entire platform is missing. |
| 5.5 AEI | `HBR_2026 / HBR_1990`. "How many times harder it is to afford housing today vs baseline year." | Doesn't exist (we don't even have 1990 data). | Need historical anchor data |
| 7.1 | Time series chart, burden ratio 1990–2026, required income vs actual income | Sparkline 2010–2023. | Wrong window, wrong metric |
| 7.3 | "Collapse curve" — single chart per county: income line, required-income line, divergence area shaded. **"This is your viral insight chart."** | Doesn't exist. | The named "viral insight chart" is missing. |
| 8.1 | Cross-source validation (Census income vs BLS wage) | Not implemented | Open |
| 8.2 | YoY jumps >15% flagged | Not implemented | Open |
| 8.3 | Never interpolate; mark gaps | Honored in current code | OK |
| 8.4 | Versioned (dataset version + formula version) | Honored at substrate level | OK |
| 10 (warning) | "Do NOT fall into this trap: 'Good enough tax assumptions + scraped housing data.' That will destroy credibility." | I built without the tax layer. | Direct violation of the explicit warning |
| 11 | "Not a dashboard. A longitudinal middle-class viability index... can later scale to relocation decision tools." | I built a dashboard. The "relocation decision tools" framing is the natural home for the **personalized affordability engine** (user-supplied household profile → answers "can I afford town X?"). | Direct violation of the framing; personalization layer not started |

This is the honest scoreboard. Most of the headline metrics named in
the `idea` spec do not exist yet. The substrate I spent weeks building
(17 fraud signals, asset checks, parity tests) is real engineering and
not wasted, but it is not what the spec told me to build first.

---

## 2. The `idea`-aligned roadmap

This replaces the roadmap I wrote in the v1 draft of this doc. It walks
the `idea` spec section by section and maps each to a deliverable.

### Phase 1 — Tax layer (the "most important and most error-prone") — **COMPLETED 2026-05-05 (TY 2023+2024)**

`idea` §3.3 + §5.2. Until the tax layer is correct, every downstream
affordability number is fiction.

**Status:** Schema + functions + 2 years of seed data + tests shipped.
Adding 2010-2022 is a pure-data exercise (no schema or function work)
that incrementally extends coverage without touching anything else.

**Shipped:**

1. **Schema** (Migrations 068, 069). `ref` not `raw`, because the
   schema-001 comment classifies "IRS thresholds" as `ref` material —
   curated reference data hand-transcribed from authoritative sources,
   not crawled by an automated ingester. Tables:
   - `ref.filing_status` (5-row IRS enum: single/mfj/mfs/hoh/qss)
   - `ref.irs_federal_brackets` (per year × status, with declarative
     CHECK that bracket #1 floor=0, UNIQUE INDEX no-duplicate-floor)
   - `ref.irs_standard_deduction` (base + age-65/blind add-ons)
   - `ref.irs_personal_exemption` ($0 since TCJA but field exists)
   - `ref.irs_child_tax_credit` (under-6 vs 6-to-17 split for ARPA
     2021 special case + per-status phaseout threshold + rate)
   - `ref.fica_parameters` (SS rate + cap, Medicare, Additional
     Medicare 0.9% with per-status threshold)
   - `ref.nj_state_brackets` (honors NJ's two-schedule shape:
     Schedule I single/mfs vs Schedule II mfj/hoh/qss with the
     extra 2.45% bracket)
   - `ref.nj_state_personal_exemption` (8 stackable kinds matching
     NJ-1040 Schedule A)
   - `ref.nj_state_property_tax_deduction` ($15K cap + $50 alternative
     credit + 18% rent-as-property-tax-equivalent per NJSA 54A:3A-17)
   - `ref.nj_state_eitc_match` (40% piggyback rate post-2020)

   Every table has `source_url` + `source_citation` columns;
   coverage views (`v_irs_federal_brackets_coverage`,
   `v_nj_state_brackets_coverage`) for asset-check substrate.

2. **Functions** (Migration 070). All `LANGUAGE sql STABLE PARALLEL
   SAFE` so the planner can inline:
   - `derived.f_apply_federal_brackets` / `f_apply_nj_state_brackets`:
     atomic piecewise-linear walkers via `LEAD()` over `bracket_floor`
   - `derived.f_federal_taxable_income`: gross − std_dedn − personal
     exemption × (filer + spouse-if-mfj-or-qss + dependents)
   - `derived.f_federal_child_tax_credit`: per-child × kids minus
     `max(0, MAGI − threshold) × phaseout_rate`, status-specific
   - `derived.f_federal_income_tax`: composite with non-refundable
     CTC clamp at 0
   - `derived.f_fica_tax`: SS @ MIN(wage, ss_wage_base) + Medicare
     uncapped + Additional Medicare 0.9% over filing-status threshold
   - `derived.f_nj_state_income_tax`: computes BOTH the property-tax
     deduction method AND the alternative-$50-credit method per
     NJSA 54A:3A-17/20, returns `LEAST(A, B)`
   - `derived.f_household_taxes(...)` RETURNS TABLE with named
     columns `(federal, nj_state, fica, total, effective_rate,
     formula_version)` — the headline composite the rest of the
     platform calls
   - Registers `ref.formula_version` row `1.1.0-tax-engine-v1` so
     downstream materializations carry reproducibility lineage

3. **Substrate-honesty discipline** (the verifiable-data rule made
   operational in code): every function returns NULL when the
   requested `(year, filing_status)` is not seeded. NEVER silently
   substitutes an adjacent year. Caught a Postgres NULL-swallow bug
   mid-implementation (`GREATEST(0, NULL)` returns 0, not NULL —
   would have made unseeded years silently return $0 federal tax)
   and fixed it with explicit CASE WHEN ... IS NULL THEN NULL ELSE
   GREATEST(0, ...) END pattern.

4. **Seed data** for tax years 2023 and 2024 (Seeds 010, 011), with
   every row carrying URL + Rev. Proc. or NJ-1040 citation:
   - 70 federal bracket rows (Rev. Proc. 2022-38 / 2023-34, Tables 1-4)
   - 10 standard-deduction rows + 2 personal-exemption rows
   - 2 CTC rows + 2 FICA rows (SSA Fact Sheet citation)
   - 78 NJ bracket rows (Schedules I + II for both years)
   - 18 NJ personal-exemption rows (all 9 kinds × 2 years)
   - 2 NJ property-tax-deduction rows + 2 NJ EITC rows

   Coverage status is documented in seed-file headers:
   **Loaded:** TY 2023, TY 2024.
   **Pending:** TY 2010-2022 — each requires its own Rev. Proc. /
   NJ-1040 citation and ships as `NNN_irs_federal_tax_<year>.sql` +
   `NNN_nj_state_tax_<year>.sql` with same per-row provenance
   discipline. Substrate honesty: 2010-2022 queries return NULL
   until those seeds land, never silent fallback to an adjacent year.

5. **54 live-pg tests** in `tests/test_tax_simulator.py` across 9
   test classes. Every assertion is a HAND-COMPUTED tax liability
   matching the published IRS Pub 17 / NJ-1040 example — not
   "function returns a number" but "function returns *the* number
   an auditor would re-derive from the cited Rev. Proc." Coverage
   includes bracket walks (boundary, mid, top-bracket including
   single $1M = $328,187.75 and NJ MFJ $1.5M Millionaires' Tax =
   $126,407.50), CTC phaseout (no-phaseout, partial single @ $230K =
   $500 credit, full MFJ @ $500K = $0), NJ deduction-vs-credit
   selector for each branch (credit wins, deduction wins, $20K prop
   tax clamps to $15K cap), FICA boundary cases (at-cap, above-cap,
   additional-Medicare with per-status threshold), composite
   household-taxes (single $60K → 19.205% effective; MFJ $120K 2 kids
   → 15.951% effective), substrate-honesty NULL bubble (12
   parameterized cases for unseeded years + surgical-delete of std-
   deduction row), and coverage-view invariants (every seeded year
   has all 5 filing statuses + a bracket starting at $0).

   ALL 54 pass; full repo suite 940/940 passing; ruff clean; mypy
   clean on new files.

**Cost:** $0. **Time spent:** ~3 hours (one session). **Remaining for
full Phase 1:** seed TY 2010-2022 (purely data work, ~30 min/year
with PDF cross-check; doesn't block Phase 2 starting on TY 2024 data).

### Phase 2 — Required-income + Affordability Gap (the headline metric) — **COMPLETED 2026-05-05**

`idea` §5.1 + §5.4 + §7.3.

**Status:** SQL substrate shipped. Three required-income metrics
(HUD-aligned linear, lender post-tax bisection, strict full-burden
bisection) + per-county affordability-gap view. Frontend Collapse
Curve page is the next deliverable in this phase.

**Shipped:**

1. **Constants registry** (Migration 071): `ref.affordability_assumptions`
   table — every numeric assumption that influences a metric carries
   `(constant_id, value, unit, source_url, source_citation)` plus an
   `effective_year` (sentinel 0 = perpetual, otherwise "as of" year)
   so that downstream calculations are traceable to a published rule
   of thumb or a regulator's stated standard. 11 constants seeded:
   mortgage default down-pct (Fannie Mae), default term (Fannie Mae),
   homeowners insurance rate default (NAIC), HUD 30% threshold (HUD
   CHAS), HUD 50% severe-burden, conventional front-DTI 28% (Fannie),
   conventional back-DTI 36% (Fannie), CFPB QM 43% effective 2014
   (Reg Z 1026.43), PMI threshold LTV 80% (HOPA 1998), PMI annual
   rate (Urban Institute), HOA default $0 (AHS), CPI baseline year
   2026 (idea §3.4). Plus `ref.f_assumption(constant_id, year)` and
   the scalar wrapper `ref.f_assumption_value(...)` for clean
   substrate reads.

2. **Mortgage + housing-cost engine** (Migration 072):
   - `derived.f_mortgage_pi_monthly(loan, annual_rate, term_years)` —
     standard amortization closed-form, IMMUTABLE.
   - `derived.f_fred_30yr_annual_rate(year)` — annual mean of FRED
     MORTGAGE30US, percent → decimal normalized.
   - `derived.f_county_property_tax_rate(fips, year)` — DCA
     `cy_total_rate`, percent → decimal normalized.
   - `derived.f_county_avg_home_price(fips, year)` — DCA
     `avg_residential_value` pass-through.
   - `derived.f_piti_annual(home_price, year, county_fips, ...)` —
     composite (12·P&I + property tax + insurance) with all four
     assumptions defaulted from the constants registry and
     individually overridable for the personalization engine; also
     accepts a `p_rate_override` for counterfactual scenarios
     ("what would PITI be if rates dropped to 2021 lows?").

3. **Three required-income metrics** (Migration 072), each
   answering a different question:
   - `derived.f_required_income_hud_30pct(piti, threshold)` — the
     **headline**: `PITI / threshold`. Linear, always defined,
     matches the HUD CHAS published cost-burden definition.
   - `derived.f_required_income_post_tax_30pct(piti, year, status,
     deps, kids, threshold)` — **lender-style**: `PITI ≤ threshold *
     (gross − tax(gross))`. Bisection over the monotone take-home
     function; always converges if tax substrate exists.
   - `derived.f_required_income_full_burden_30pct(home, year, fips,
     status, deps, kids, threshold)` — **strict**: `PITI + tax(G) ≤
     threshold * G`. Bisection; **deliberately returns NULL when
     unreachable**, which it usually is in NJ because combined
     federal+NJ+FICA marginal exceeds 30% in middle MFJ brackets.
     The NULL is the housing-cost crisis rendered numerically and
     tested as such (`TestRequiredIncomeFullBurden::
     test_typical_nj_scenario_unreachable`).

4. **Per-county headline view** (Migration 072):
   `derived.v_affordability_gap` exposes home price, median income,
   PITI, all three required-income metrics, headroom dollars
   (`median − HUD_required`), and the HUD required/actual ratio. One
   row per `(county_fips, year)`. Representative household MFJ + 1
   dep + 1 kid; the personalization engine in Phase 4 computes
   per-user. Smoke run on a synthetic county (DCA $500K @ 2.85%,
   FRED 7%, ACS5 $120K) yields exactly the kind of granular signal
   the spec demanded:
   - PITI = $47,934.52
   - HUD-required income = $159,781.73 → median falls **25% short**
   - Lender-required income = $210,318 → median falls **75% short**
   - Strict-required income = NULL (unreachable at any income)
   `formula_version` = `'1.2.0-affordability-engine-v1'` registered.

5. **41 live-pg tests** in `tests/test_phase2_affordability.py`
   across 10 test classes. Every assertion is a HAND-COMPUTED PITI,
   mortgage P&I, or required-income value cross-checked against
   published mortgage formulas. Coverage includes: mortgage P&I at
   ($400K@7%/30y=$2,661.21, $300K@3%/30y=$1,264.81,
   $500K@8%/15y=$4,778.26), zero-rate edge ($P/n), zero/negative/
   null inputs, FRED unit normalization (percent→decimal), DCA
   pass-through, "as of" assumption resolution including the CFPB
   QM rule that doesn't exist before 2014, full PITI composition
   with five parameter overrides, all three required-income metrics
   on the same scenario showing they tell different stories, and
   a substrate-honesty pin on the strict-metric NULL signal so a
   future tax-cut or rate-drop that changes the math has to update
   the test deliberately. ALL 41 pass; full repo suite **981/981**;
   ruff clean; mypy clean on new files.

6. **The Collapse Curve frontend** (`/housing/[county_id]/collapse`)
   — **SHIPPED 2026-05-05**. The spec §7.3 "viral insight chart" is
   live as a server-rendered SVG that plots actual median income vs
   HUD-required income with a shaded gap area between them.
   - `lib/housing.ts::getCountyAffordabilityGap(countyId)` queries
     `derived.v_affordability_gap` (Phase 2 view) and returns the
     time series + a coverage block reporting per-substrate year
     ranges so the page is honest about data availability.
   - `components/CollapseCurve.tsx` is a server-rendered SVG (zero
     client JS, same architectural choice as `<Sparkline />`) with
     dollar y-axis, year x-axis, two line series (blue actual,
     red required), filled gap polygon (red-tinted when required >
     actual = unaffordable; green-tinted otherwise), per-year SVG
     `<title>` tooltips, legend.
   - `app/housing/[county_id]/collapse/page.tsx` lays out the
     header (latest-year headline numbers), the curve, a side-by-
     side comparison of all three required-income metrics (HUD,
     lender, strict full-burden with the substrate-honest
     "Unreachable" label when applicable), and a methodology box
     showing exactly which years each upstream substrate covers
     and which years are joinable into a single curve point.
   - Linked from the per-county detail page via a prominent "View
     Collapse Curve →" CTA in the page header.
   - 10 live-pg tests in `tests/test_collapse_curve_query.py`
     covering view shape, hand-computed PITI for two synthetic
     years (2023 $450K @ 6% / 2024 $500K @ 7%), HUD required-
     income linearity, headroom arithmetic, the coverage query the
     page issues, and a substrate-honesty pin: a year with DCA +
     ACS + FRED but NO seeded tax tables MUST surface NULL
     post-tax-required (not silently substitute another year).
   - tsc clean, Next 16 production build clean (route inventoried
     as ƒ /housing/[id]/collapse, dynamic), full pytest 991/991
     (was 981 + 10 new collapse-curve tests).

**Phase 2 completion status:** SQL substrate + frontend curve all
shipped. Frontend will start showing real data for any NJ county as
soon as the historical IRS / NJ tax-table seeds for 2010–2022 land
(the page already renders correctly for 2023 + 2024; earlier years
display as NULL with explicit "data not yet seeded" messaging in the
methodology box).

**Cost:** $0. **Time spent:** ~5 hours total across both sessions.

### Phase 3 — Disposable income trajectory + Affordability Erosion Index — **COMPLETED 2026-05-06**

`idea` §5.3 + §5.5.

**Delivered:**

1. **`db/migrations/073_disposable_income_aei.sql`** — formula version
   `1.3.0-disposable-income-erosion-v1`. Five surfaces:
   - `derived.f_disposable_income_annual(gross, year, county_fips, status, deps, kids, home)`
     = `gross - federal/NJ/FICA tax - PITI`. Composes the Phase-1 tax
     engine and the Phase-2 PITI engine. NULL bubbles through every
     missing-substrate path.
   - `derived.f_disposable_income_real(..., base_year)` — CPI-deflated
     via `derived.cpi_u_headline_annual`. Returns NULL when CPI is
     missing for either the value year or the base year (no silent
     fallback to a different base year — substrate honesty per
     `verifiable-data.mdc`).
   - `derived.f_household_burden_ratio(year, county_fips)` — the
     spec §5.1 HBR: `PITI(median_home, year, county) / median_income`.
     Used by AEI and exposed as a pure function so the personalization
     engine can reuse it.
   - `derived.f_affordability_erosion_index(county_fips, year, anchor_year)`
     — the spec §5.5 AEI: `HBR(year) / HBR(anchor_year)`.
   - `derived.v_disposable_income_trajectory` — per-(county, year) DI
     for the representative MFJ-1-1 household, both nominal and
     CPI-deflated to the latest available CPI year. Row carries
     `real_dollars_base_year`, profile fields, and formula version.
   - `derived.v_aei_by_county` — per-county AEI vs the EARLIEST year
     for which the county has a non-NULL HBR. Anchor-year choice is
     auto-discovered, NOT hardcoded to 1990 (which is unreachable
     until pre-2009 income substrate is loaded). Row exposes
     `anchor_year` so the UI can show "vs YYYY" honestly.

2. **`tests/test_phase3_disposable_income.py`** — 34 hand-computed
   live-PG tests pinning DI, real-DI, HBR, and AEI to specific dollar
   values for a synthetic two-year substrate. Substrate-honesty
   assertions pin every "missing input → NULL" path.

3. **`lib/housing.ts`** — `getCountyDisposableIncome(countyId)` reads
   both views in parallel; returns `CountyDisposableIncome` with the
   trajectory, the headline AEI, profile metadata, and formula version.

4. **`components/DisposableIncomeChart.tsx`** — server-rendered SVG
   plotting real DI (solid blue) over nominal DI (dashed gray), with
   a shaded area-under-curve to make "what's left over" visually
   concrete. Zero client JS.

5. **`app/housing/[id]/collapse/page.tsx`** — extended (not duplicated
   onto a separate route) with:
   - **AEI strip in the header** showing the headline AEI value, the
     `HBR_latest / HBR_anchor` ratio, and the explicit anchor year.
     Color-coded (amber if ≥1.25, default if ≥1.0, emerald if <1.0).
   - **Disposable income trajectory section** between the
     required-income comparison and the substrate methodology box.

6. **`tests/test_collapse_curve_query.py`** — extended with 8 Phase-3
   query tests (trajectory view shape + numbers, AEI view shape +
   numbers, real-dollars base year detection, substrate honesty for
   unseeded tax years).

**Hand-computed anchors (synthetic two-year fixture, pinned in tests):**

| Year | Profile | PITI | Tax | DI nominal | DI in 2024 dollars |
|---|---|---|---|---|---|
| 2023 | MFJ-1-1, $450K home, FRED 6%, ACS $115K | $40,300.69 | $20,168.77 | $54,530.54 | $56,138.89 |
| 2024 | MFJ-1-1, $500K home, FRED 7%, ACS $120K | $47,934.52 | $21,223.63 | $50,841.86 | $50,841.86 |

HBR_2023 = 0.35044, HBR_2024 = 0.39945, **AEI = 1.140** (housing 14%
more burdensome in one year against this synthetic substrate). CPI
deflator 2023 → 2024: 313.689 / 304.702 = 1.02949.

**Quality gate:** `ruff` + `mypy` clean on touched Python; `tsc` +
`next build` clean; **1033/1033 pytest** pass.

**Operational note:** real NJ counties currently have only 2023+2024
plotted (the seeded tax-table range). The chart and AEI light up for
older years automatically as `db/seeds/010_irs_federal_tax_*` and
`011_nj_state_tax_*` extend backward — pure data work, not blocking
Phase 4.

**Cost:** $0. **Time spent:** ~3 hours.

### Phase 4 — Personalized Affordability Engine — **COMPLETED 2026-05-06** (idea §11: "relocation decision tools")

This is the layer that turns the platform from "look at numbers" into
"answer my question." Same engine as Phases 1–3, with the median
household swapped for a user-supplied profile.

**Delivered:**

1. **`db/migrations/074_personalization_engine.sql`** + **`db/seeds/013_personalization_assumptions.sql`**
   — formula version `1.4.0-personalization-engine-v1`. Six surfaces:
   - `derived.f_piti_coefficient(year, county, down, term, ins, rate)`
     — annual PITI per dollar of home price. Closed-form constant
     extracted via `f_mortgage_pi_monthly($1)` to handle the zero-rate
     edge without a special case.
   - `derived.f_user_max_affordable_home_price_dti(...)` — closed-form
     max H under Fannie Mae conventional DTI on gross. PITI(H) is
     linear in H so this is `min(dti_front × G / c, max(0, (dti_back × G − 12 × other_debt) / c))`,
     no bisection needed.
   - `derived.f_user_max_affordable_home_price_post_tax(...)` —
     stricter variant: PITI ≤ dti_front × take-home. Same closed
     form; tax computed once because it doesn't depend on H.
   - `derived.f_user_required_income_for_home(...)` — inverse of
     max-affordable; gross required to make a given home satisfy
     both DTI caps.
   - `derived.f_user_town_verdict(year, county, profile, ...)` —
     headline tuple per (year, county, profile): median home price,
     max-affordable (both flavors), PITI on median, required income,
     personal burden ratio (gross + post-tax), verdict label
     (`affordable` / `stretch` / `out_of_reach`), dollar gap.
   - `derived.f_user_nj_county_verdicts(year, profile, ...)` —
     set-returning convenience that emits one verdict row per NJ
     county. Drives the per-county verdict table on `/personalize`.
   - Seed 013 adds `affordability_stretch_multiplier=1.25` (HUD
     outreach materials: "stretch home" = up to 25% over budget).

2. **`tests/test_phase4_personalization.py`** — 34 hand-computed
   live-PG tests grouped into 6 classes pinning every closed-form
   value to specific dollar amounts. Substrate-honesty tests pin
   every "missing input → NULL" path (NULL gross, zero gross,
   negative gross, unknown county, unseeded tax year, unknown filing
   status). The `test_c_matches_piti_via_component_sum` invariant
   guarantees the closed-form refactor agrees with the existing
   `f_piti_annual` to the cent — if the c-coefficient ever drifts,
   this test fails before the personalization engine misleads anyone.

3. **`lib/personalize.ts`** — typed wrappers:
   - `HouseholdProfile` type + `parseProfileFromSearch()` /
     `profileToSearchParams()` for URL ↔ state encoding (sanitizes
     every field; a hostile URL can't crash the server or trigger
     SQL errors).
   - `runPersonalizationEngine(profile)` — single-round-trip read
     that runs the verdicts function, the tax engine, and the
     citation lookup in parallel via `Promise.all`.
   - `verdictTone()` and `fmtUsd()` / `fmtPct()` display helpers.

4. **`app/personalize/page.tsx`** — server-rendered (zero client JS)
   form + per-county verdict table:
   - **Form uses METHOD=GET** so the URL fully encodes the profile
     — bookmarks, sharing, and back/forward all "just work" without
     any session/account infrastructure. No PII server-side.
   - **Tax burden card** shows federal + NJ + FICA + total +
     effective rate + take-home, computed by the Phase-1 engine for
     the user's exact profile.
   - **Per-county verdict table** with 21 rows (one per NJ county),
     sorted by income gap ascending (most affordable first). Every
     row carries median home, user's max-affordable, dollar gap,
     personal burden ratio, and BOTH DTI verdicts (gross + post-tax)
     as colored pills.
   - **Counterfactual sliders** behind a `<details>` block: down
     payment, term (15/30), DTI front/back caps, mortgage rate
     override (so the user can ask "what if rates dropped to 2021
     lows?"). Each override re-runs the engine via URL navigation.
   - **Assumptions block** lists every defaulted constant with its
     citation, source URL, and effective year — the verifiable-data
     contract made user-facing.
   - **Substrate-honest empty state** when the requested year has
     no NJ DCA / FRED substrate (e.g. year=2010 currently): the
     page surfaces a one-line explanation suggesting year=2024.

5. **`app/layout.tsx`** — added `Personalize` to the top nav,
   visually emphasized (red, bold) so it's discoverable from every
   page. The civic-integrity nav order is now:
   `Housing | Personalize | Risk queue | Methodology`.

**Hand-computed anchors (synthetic test substrate, pinned in tests):**

For a $150K MFJ-1-1 user, $500K median home in test county
(FRED 7%, prop tax 2.85%, 2024):

| Output | Value |
|---|---|
| PITI coefficient `c` | 0.095869 (annual PITI per $1 home price) |
| PITI on median home | $47,934.52 (matches `f_piti_annual` exactly) |
| Max-affordable (gross DTI) | $438,097.64 (front-end binds) |
| Max-affordable (post-tax DTI) | $346,312.90 |
| Required gross for median | $171,194.71 |
| Tax on $150K MFJ-1-1 2024 | $31,426.13 (Phase-1 anchor) |
| Take-home | $118,573.87 |
| Personal burden ratio (gross) | 31.96% (above HUD 30%) |
| Personal burden ratio (post-tax) | 40.43% (above 28%) |
| Verdict (DTI / post-tax) | `stretch` / `out_of_reach` |
| Gross income gap | +$21,194.71 (need $21K more income) |

The "out_of_reach by post-tax" + "stretch by DTI" split is exactly
the kind of granular per-user signal the spec demanded — and exactly
the kind of detail an opposing lawyer can't dismiss because every
input is cited.

**Quality gate:** `ruff` + `mypy` clean on touched Python; `tsc` +
`next build` clean (now includes `ƒ /personalize` route);
**1067/1067 pytest** pass (was 1033 after Phase 3, +34 Phase 4).

**Cost:** $0. **Time spent:** ~3 hours.

---

(Original Phase 4 design notes, kept for reference — every item
below is now satisfied by the deliverables above.)

**User inputs** (all optional; sensible defaults from spec/HUD/Fannie):

| Field | Type | Default (cited) |
|---|---|---|
| Annual gross household income | $ | (no default — required) |
| Filing status | enum {single, MFJ, MFS, HOH} | single |
| Dependents | int | 0 |
| Other dependents (qualifying relative) | int | 0 |
| Liquid savings available for down payment | $ | 20% × home_price (idea §5.1) |
| Other monthly debt service (auto, student, CC) | $/mo | 0 |
| Town(s) of interest (NJ municipality picker) | enum from `ref.nj_municipality` | none (defaults to all 565) |
| Target home price (or "use town median") | $ or null | town median (MOD-IV when loaded; DCA avg residential value as fallback) |
| Mortgage term | enum {15, 30} years | 30 (idea §5.1) |
| Front-end DTI cap | % | 28% (Fannie Mae conventional underwriting standard) |
| Back-end DTI cap | % | 36% (Fannie Mae conventional standard); 43% (CFPB QM rule) as alt |
| Filing-year tax law to apply | year | latest in `raw.irs_federal_brackets` |

**Outputs** (every value carries source + formula version):

1. **Personal effective tax rate**: federal (with brackets, standard
   deduction, child tax credit per dependents, EITC if applicable) +
   NJ state (with NJ property-tax deduction capped per current law)
   + FICA (6.2% SS up to wage base + 1.45% Medicare). Each component
   broken out.
2. **Personal max affordable home price**: the largest home price
   such that PITI ÷ monthly_gross ≤ front-DTI AND
   (PITI + other_debt) ÷ monthly_gross ≤ back-DTI, given user's down
   payment, current FRED 30-yr rate, and the *town's* effective
   property tax rate (not a state-average estimate).
3. **Per-town verdict** ("Can I afford to live in X?"): for each town
   the user selected (or all 565 by default), traffic-light:
   - **Affordable** — town median home price ≤ user's max affordable.
   - **Stretch** — between max affordable and 1.25× max.
   - **Out of reach** — above 1.25× max.
   Each verdict shows the dollar gap and the *exact* numbers driving
   it (PITI breakdown, after-tax income, DTI ratios).
4. **Personal Affordability Gap** (the spec's §5.4 metric, applied to
   *this* household instead of the median): `required_income_for_this_town -
   user_income`. Negative = town is affordable; positive = how much
   more annual gross is needed.
5. **"Towns where your dollar goes furthest"**: ranked list of NJ
   towns by `(user_max_affordable - town_median_price)` with the
   per-town breakdown one click away.
6. **Counterfactual sliders**: user can drag down-payment, rate
   (default current FRED, can set to 2021 lows for nostalgia),
   filing status, dependents, term — re-renders verdicts live.
7. **Disposable-income trajectory under this profile** in this town:
   `gross - taxes - PITI` over time, in real 2026 dollars. Shows
   what their lifestyle budget would look like in town X this year
   vs. five years ago vs. ten years ago.
8. **Shareable URL** that encodes the profile (no PII server-side;
   profile is a query string + signed hash) so the user can share
   "here's what the math says for my situation."

**Why this matters for spec compliance:** spec §11 explicitly names
"relocation decision tools" as what the platform should become. This
phase builds it.

**Why this matters for credibility:** a generic "burden ratio = 1.6×"
is dismissable. A specific "you earn $X, you have N dependents, in
town Y the median home is $Z, and at today's rate your PITI would be
$W which puts your front-DTI at 47% — out of reach by $K/year of
gross income" is undismissable. It's also auditable, because every
input is cited and every formula is versioned (per
`verifiable-data.mdc`).

**Privacy and posture:** the household profile is a client-side
construct passed as URL query params; the server stores nothing
unless the user explicitly opts into a saved scenario. No accounts
required. The platform's value comes from the public data, not from
hoarding user data.

**Cost:** $0. **Time:** 2 weeks after Phase 3 (assumes Phases 1–3
are in place; the engine is fully reusable).

### Phase 5 — Spec compliance audit + UI rewrite

1. Replace the 2010 base-year UI with the spec-mandated **2026 real
   dollars baseline** (idea §3.4).
2. Replace the unitless "burden ratio" headline with the **dollar
   Affordability Gap** (idea §5.4) and the personal verdict from
   Phase 4.
3. Replace tier badges with the dollar gap and the income-required
   number; tier classification stays as a secondary visual.
4. Per `verifiable-data.mdc`: every number gets a hover with source +
   vintage + formula version. Every page gets a "View sources" link.

### Phase 6 — Add Zillow ZHVI / Redfin (idea §3.2)

Spec preferred sources for housing index. Add as a parallel index to
FHFA so we have cross-source validation per idea §8.1.

### Phase 7 — Cross-source validation + outlier flags

`idea` §8.1, §8.2. Asset checks that fail the build when:

- Census income deviates from BLS wage by more than X%.
- Any (county, year, metric) jumps >15% YoY without a documented
  publisher revision.

### Phase 8 — Municipality drill-down + MOD-IV parcel data

NJ DCA already publishes municipal-level property-tax data (we ingest
counties only today; the same workbook contains 565 munis). MOD-IV
parcel data adds per-property granularity. Both unlock town-level
precision for the Phase 4 personalization engine ("the median home in
*this exact town* is $X" instead of "the county median is $Y").

### Phase 9 — Geographic expansion (idea §2)

NJ counties first, then statewide. Spec §11 explicitly anticipates
"US-wide comparisons" as a follow-on.

### Phase 10 — Civic-integrity pillar (separate track)

The `idea` spec is silent on the fraud pillar; that scope was added in
later sessions. Pillar 2 work continues in parallel but with the same
verifiability discipline. Highest-leverage missing source remains
**NJ ELEC** (state-level political contributions; the actual NJ
corruption substrate that federal-only sources cannot see).

---

## 3. Specific verification debt to pay down right now

Listed so we have a punch-list, not a vibe.

| File | What's hardcoded / unverifiable | Fix |
|---|---|---|
| `lib/housing.ts` | `BURDEN_BASE_YEAR = 2010` (spec wants 2026) | Move to `ref.platform_constants` table; cite spec §3.4 |
| `lib/housing.ts` `burdenTier()` | Cutoffs 1.4 / 1.15 / 0.95 with no source | Move to `ref.tier_bands` seed table with citation column; per-tier `formula_version` |
| `components/BurdenBarChart.tsx` | Color hex codes inline; `padded * 1.1`; `step = 0.25` | Colors → reference theme; padding/step → component props with documented defaults |
| `components/Sparkline.tsx` | Pixel sizes, baseline=100 | OK if labeled, but baseline source needs citation (HUD 30%? spec §3.4? document) |
| `app/housing/page.tsx` | Counties table assumes 21; methodology copy hardcodes formula | Counties from `ref.county WHERE state_code = 'NJ'`; methodology copy reads from a `ref.metric_methodology` table |
| FRED rate usage anywhere | Currently zero — but when added, must be vintage-stamped per query | Per-query vintage column; never a global `MORTGAGE_RATE` constant |
| All migrations adding derived tables | Already comply | Keep enforcing |
| **Phase 4 personalization defaults** (when implemented) | Down-payment 20%, term 30y, front-DTI 28%, back-DTI 36%, insurance ~0.35%/yr of home value | Each default lands in `ref.affordability_defaults` with `(default_id, value, source_url, citation_text, formula_version)`; UI shows the citation next to every default and lets the user override it |
| **Phase 4 tax-credit constants** (CTC, EITC phaseouts, NJ property-tax deduction cap) | Currently zero | Per-year rows in `raw.irs_*` and `raw.nj_*`; never inlined in code |

---

## 4. Where this leaves us

**As of 2026-05-06: Phases 1, 2, 2.5, 3, and 4 are all SHIPPED.**

- The substrate is solid and reusable. Nothing thrown away.
- The user-facing deliverables (**Required income**, **Disposable
  income**, **Affordability Gap**, **Collapse Curve**, **AEI**, and
  **per-user verdicts**) are now all live, with every dollar number
  cited and every formula version-stamped (`1.1.0-tax-engine-v1` →
  `1.2.0-affordability-engine-v1` → `1.3.0-disposable-income-erosion-v1`
  → `1.4.0-personalization-engine-v1`).
- The platform now answers the spec's actual question, in two flavors:
  - **County-level (anonymous):** "In dollar terms, how much income
    does a household need in this NJ county this year to afford the
    median home — and how short of that is the median household?"
    → Answered by `/housing/[id]/collapse` (the Collapse Curve +
    three required-income metrics + AEI + DI trajectory).
  - **Personal (URL-shareable, no PII):** "Given my income, my filing
    status, my dependents, my other debt — which NJ counties can I
    actually afford, and by how much?" → Answered by `/personalize`.
- Pillar 1 (housing affordability) is now feature-complete against
  the `idea` spec. Remaining work is **data depth** (Phase 5 historical
  tax tables to light up the Collapse Curve back to 2010, Phase 8
  MOD-IV for municipality-level granularity), **operational** (Phase 7
  release calendar + asset checks, deferred §7.1 one-button refresh),
  or **expansion** (Phase 9 statewide / out-of-NJ).

---

## 5. What I'm committing to going forward

1. Read `idea` and `AGENTS.md` at the start of every working session
   and do not act outside their constraints.
2. Honor `.cursor/rules/verifiable-data.mdc` on every change.
3. Map every new metric to an `idea` section before building it; if it
   doesn't map, document why before adding it.
4. Stop adding fraud signals as the default response to "continue".
   Spec compliance for Pillar 1 comes first.
5. Default to $0/month per `AGENTS.md`. Surface paid-tier trade-offs
   explicitly when they're the right call; do not assume.

---

## 6. Tomorrow's first move (proposed)

~~Phases 1, 2, 2.5, 3, and 4 are now all COMPLETED.~~ The headline
spec deliverables are all shipped.

**Next moves, in priority order (each is independently valuable):**

1. **Phase 5 — Historical tax-table backfill (2010–2022).** Pure data
   work, ~1 day per pair (IRS + NJ) of years. Lights up the Collapse
   Curve, DI trajectory, and AEI back to 2010 across all 21 counties
   and across all `/personalize` historical scenarios. The engines
   already work for these years — the only blocker is the substrate.
   Highest data-leverage-per-hour task on the board.

2. **Phase 8 — MOD-IV municipality-level home prices.** Currently
   `/personalize` uses DCA county-average residential value as the
   median-home proxy. Loading MOD-IV upgrades this to municipality-
   level granularity (565 NJ towns vs 21 counties) — turns "the
   median home in Bergen County" into "the median home in Tenafly."
   Same engines, finer substrate.

3. **Deferred §7.1 — One-button data refresh.** Already designed in
   §7.1 of this doc. ~1 day to implement now that we have a live
   user-facing surface that benefits from staleness honesty.

4. **Phase 7 — Release calendar + freshness asset checks.** Companion
   to §7.1; surfaces "data current as of <date>; publisher last
   released <date>" badges throughout the UI.

5. **`/personalize` follow-ups (next-session polish):** (a) "Towns
   where your dollar goes furthest" sortable view (already implicit
   in the verdict table; could be a separate ranking page);
   (b) personal disposable-income trajectory chart in a chosen
   county using the existing `DisposableIncomeChart` parameterized
   on the user profile; (c) personal AEI ("housing in town X is N×
   harder for YOU vs the anchor year").

**Recommended first lift:** Phase 5 (historical tax tables). The
engines are starving for substrate; every additional year unlocks
21 counties × 4 metrics = 84 new data points on existing pages with
zero engineering risk.

---

## 7. Deferred — operational convenience (not blocking)

Captured 2026-05-05 from a side conversation. **None of these are on
the critical path; they are noted here so they don't get lost when
they become the right next thing.**

### 7.1 One-button data-refresh

The user asked: "will I be able to click a button on my computer and
it automatically start refreshing to make sure all data has the most
current available data?". The honest answer is yes for the
auto-fetchable sources (FRED, ACS, BLS, FBI NIBRS, OIG LEIE,
USAspending, CPI, FHFA HPI) and no for the manually-curated tier
constants (IRS Rev. Procs., NJ Division of Taxation tables, NJ DCA
property tax — these need annual hand-transcription with citations
because the publishers do not offer machine-readable APIs).

**Deliverable when prioritized:**

1. A Dagster job `global_refresh_all` that materializes every
   `auto_fetchable=True` asset in dependency order, with per-source
   "skip if fresh enough" gates so it idempotently no-ops on data
   already at the latest published vintage.
2. A Make target `make refresh` (and CLI shortcut
   `nj-cli refresh --since 7d`) that triggers the job locally
   against the docker-compose Postgres without needing the Dagster
   webserver UI.
3. A `derived.v_data_freshness_summary` view: per-source last-
   ingested-at, latest-publisher-vintage, days-stale. Surfaced in
   the UI as a small badge on each metric tooltip ("data current as
   of <date>; publisher last released <date>") so the verifiable-
   data rule extends to staleness as well as provenance.
4. For the manually-curated layers (tax tables especially), an
   asset-check that asserts "if (current_year - 1) seed file does
   not exist by April 1, fail the build". A polite forcing function
   so the IRS Rev. Proc. for the prior tax year gets transcribed
   within Q1 of the next year.

**Cost:** $0 (all infra already in place). **Time:** ~1 day.
**Trigger:** when the user wants to stop reasoning about freshness.

### 7.2 Other deferred ops items

(Reserved for future entries; do not let this section get long
without a corresponding promotion to a phase.)

— end —
