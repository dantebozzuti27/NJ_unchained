/* =========================================================================
 * NJ Housing Burden Terminal -- front-end controller.
 *
 * Architecture
 * ------------
 * A single ES module, no build step. Three views:
 *
 *   Trend     -> /pums-burden-county-series       (multi-year series)
 *   Compare   -> /pums-burden-county              (latest year, all counties)
 *   Segments  -> /pums-burden-county/{county_fips} (latest year, one county)
 *
 * The module is structured as:
 *   - tiny fetch wrapper (apiGet)
 *   - per-view render functions (renderTrend / renderCompare / renderSegments)
 *   - a top-level state machine that re-renders the active view on
 *     control changes
 *
 * No frameworks. We treat the DOM as a pure-render target keyed by
 * controls. Plotly is loaded async via <script src="..." defer> and
 * we await its presence on first render.
 * ========================================================================= */

"use strict";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const TENURE_LABEL = {
  renter:       "Renter",
  owner_w_mtg:  "Owner (mortgage)",
  owner_no_mtg: "Owner (no mortgage)",
};

const SEGMENT_VALUE_LABEL = {
  // race
  white: "White",
  black: "Black",
  asian: "Asian",
  aian:  "AIAN",
  nhpi:  "NHPI",
  some_other: "Some other race",
  two_or_more: "Two or more races",
  // hispanic
  hispanic: "Hispanic",
  not_hispanic: "Not Hispanic",
  // citizenship
  us_citizen_born:        "US-born citizen",
  us_citizen_naturalized: "Naturalized citizen",
  not_us_citizen:         "Not a citizen",
  // age bands (whatever derived layer emits)
  under_25: "Under 25",
  "25_34":  "25-34",
  "35_44":  "35-44",
  "45_54":  "45-54",
  "55_64":  "55-64",
  "65_plus": "65+",
};

const PALETTE = [
  "#f0a860", "#6dd3c7", "#9aa7e0", "#e07a7a", "#71c787",
  "#e2c365", "#b287d9", "#7fb1d9", "#d99c7a", "#85c1ad",
  "#cc8fb6", "#a3c285", "#d4d169", "#7eb8b8", "#e09d9d",
  "#9c9c7a", "#b3a3e0", "#7acc92", "#e0bf85", "#a87aa8",
  "#85b0e0",
];

const PLOTLY_LAYOUT_BASE = {
  paper_bgcolor: "#161c22",
  plot_bgcolor:  "#0c1014",
  font: { color: "#d8dde3", family: "ui-monospace, Menlo, Consolas, monospace", size: 12 },
  margin: { t: 40, r: 24, b: 56, l: 80 },
  xaxis: { gridcolor: "#2a323b", zerolinecolor: "#2a323b" },
  yaxis: { gridcolor: "#2a323b", zerolinecolor: "#2a323b" },
  hoverlabel: { bgcolor: "#1f262e", bordercolor: "#2a323b", font: { color: "#d8dde3" } },
  legend:  { bgcolor: "rgba(0,0,0,0)" },
};

const PLOTLY_CONFIG = {
  displaylogo: false,
  responsive: true,
  modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"],
};

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function apiGet(path, params = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") q.append(k, v);
  }
  const url = q.size > 0 ? `${path}?${q.toString()}` : path;
  const r = await fetch(url, { headers: { Accept: "application/json" } });
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`GET ${url} -> ${r.status}: ${detail.slice(0, 200)}`);
  }
  return r.json();
}

async function waitForPlotly(timeoutMs = 5000) {
  const start = Date.now();
  while (typeof window.Plotly === "undefined") {
    if (Date.now() - start > timeoutMs) throw new Error("Plotly failed to load");
    await new Promise((res) => setTimeout(res, 50));
  }
}

// ---------------------------------------------------------------------------
// Health badge
// ---------------------------------------------------------------------------

async function refreshHealth() {
  const badge = document.getElementById("health-badge");
  try {
    const h = await apiGet("/health");
    if (h.db_reachable && h.status === "ok") {
      badge.textContent = "db: ok";
      badge.className = "badge badge-good";
    } else if (h.db_reachable) {
      badge.textContent = `db: degraded (${h.n_errors_last_1h} err/1h)`;
      badge.className = "badge badge-warn";
    } else {
      badge.textContent = "db: down";
      badge.className = "badge badge-bad";
    }
  } catch (e) {
    badge.textContent = "db: unreachable";
    badge.className = "badge badge-bad";
  }
}

// ---------------------------------------------------------------------------
// Footer: dataset freshness summary (GET /assets)
// ---------------------------------------------------------------------------

async function refreshFreshnessFooter() {
  const el = document.getElementById("freshness-badge");
  if (!el) return;

  try {
    const rows = await apiGet("/assets");
    let fresh = 0;
    let stale = 0;
    let unknown = 0;
    let err30 = 0;
    let warn30 = 0;
    for (const r of rows) {
      if (r.freshness_state === "fresh") fresh += 1;
      else if (r.freshness_state === "stale") stale += 1;
      else unknown += 1;
      err30 += r.n_error_30d ?? 0;
      warn30 += r.n_warn_30d ?? 0;
    }
    el.textContent = `datasets: ${fresh} fresh · ${stale} stale · ${unknown} unknown`;
    let cls = "freshness-footer";
    if (stale > 0) cls += " freshness-warn";
    if (err30 > 0) cls += " freshness-bad";
    el.className = cls;
    const parts = [`${rows.length} datasets`, `errors 30d: ${err30}`, `warns 30d: ${warn30}`];
    el.title = parts.join(" · ");
  } catch (e) {
    el.textContent = "datasets: —";
    el.className = "freshness-footer";
    el.title = e.message;
  }
}

// ---------------------------------------------------------------------------
// Release calendar strip (GET /release-calendar)
// ---------------------------------------------------------------------------

const RELEASE_ET_FMT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

function fmtReleaseEt(iso) {
  if (!iso) return "—";
  return RELEASE_ET_FMT.format(new Date(iso));
}

function releaseCalendarSortKey(s) {
  const upcoming = s.upcoming_releases ?? [];
  if (upcoming.length > 0) return new Date(upcoming[0]).getTime();
  if (s.next_expected_at) return new Date(s.next_expected_at).getTime();
  return Number.MAX_SAFE_INTEGER;
}

async function refreshReleasePanel() {
  const tbody = document.getElementById("release-panel-tbody");
  const meta = document.getElementById("release-panel-meta");
  const statusEl = document.getElementById("release-panel-status");
  if (!tbody || !meta || !statusEl) return;

  statusEl.textContent = "";
  statusEl.classList.remove("error");

  try {
    const data = await apiGet("/release-calendar", { days: 14 });
    const nLate = data.sources.reduce((n, s) => n + (s.overdue ? 1 : 0), 0);
    meta.textContent =
      `Window: ${data.horizon_days}d · as of ${fmtReleaseEt(data.as_of)} ET` +
      (nLate > 0 ? ` · ${nLate} late vs calendar` : "");

    const sorted = [...data.sources].sort((a, b) => {
      const ka = releaseCalendarSortKey(a);
      const kb = releaseCalendarSortKey(b);
      if (ka !== kb) return ka - kb;
      return a.source_id.localeCompare(b.source_id);
    });

    tbody.replaceChildren();
    for (const s of sorted) {
      const tr = document.createElement("tr");

      const tdSrc = document.createElement("td");
      tdSrc.className = "src";
      tdSrc.textContent = s.source_id;

      const tdCad = document.createElement("td");
      tdCad.className = "cadence";
      tdCad.textContent = s.cadence;

      const tdNext = document.createElement("td");
      tdNext.className = "next";
      if (!s.schedule_computed) {
        tdNext.textContent = "—";
        tdNext.title = s.schedule_label ?? "";
      } else {
        tdNext.textContent = fmtReleaseEt(s.next_expected_at);
        tdNext.title = s.schedule_label ?? "";
      }

      const tdWin = document.createElement("td");
      tdWin.className = "win";
      const ur = s.upcoming_releases ?? [];
      if (ur.length === 0) {
        tdWin.textContent = s.schedule_computed ? "—" : "n/a";
      } else if (ur.length <= 3) {
        tdWin.textContent = ur.map((x) => fmtReleaseEt(x)).join("; ");
      } else {
        tdWin.textContent = `${ur.length} slots (${fmtReleaseEt(ur[0])} …)`;
      }

      const tdFresh = document.createElement("td");
      let cls = "tag-unknown";
      let label = s.freshness_state;
      if (s.overdue) {
        cls = "tag-overdue";
        label = "late";
      } else if (s.freshness_state === "fresh") {
        cls = "tag-fresh";
      } else if (s.freshness_state === "stale") {
        cls = "tag-stale";
      }
      tdFresh.className = cls;
      tdFresh.textContent = label;

      tr.append(tdSrc, tdCad, tdNext, tdWin, tdFresh);
      tbody.appendChild(tr);
    }
  } catch (e) {
    tbody.replaceChildren();
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "release-panel-loading";
    td.textContent = `Calendar unavailable: ${e.message}`;
    tr.appendChild(td);
    tbody.appendChild(tr);
    statusEl.textContent = e.message;
    statusEl.classList.add("error");
  }
}

// ---------------------------------------------------------------------------
// Trend view -- one county, multiple tenures (or one tenure if filtered),
//               line chart with 90% CI bands.
// ---------------------------------------------------------------------------

async function renderTrend(state, statusEl) {
  const div = document.getElementById("chart-trend");
  if (!state.county) {
    statusEl.textContent = "Select a county to render the trend.";
    Plotly.purge(div);
    return;
  }
  statusEl.textContent = "Loading...";
  const rows = await apiGet("/pums-burden-county-series", {
    county_fips: state.county,
    tenure: state.tenure || undefined,
    product: state.product,
  });
  if (rows.length === 0) {
    statusEl.textContent = "No rows for this county/tenure/product.";
    Plotly.purge(div);
    return;
  }

  // Group by tenure (if filter is unset, multiple traces appear).
  const byTenure = new Map();
  for (const r of rows) {
    if (!byTenure.has(r.tenure_class)) byTenure.set(r.tenure_class, []);
    byTenure.get(r.tenure_class).push(r);
  }

  const traces = [];
  let i = 0;
  for (const [tenure, group] of byTenure) {
    group.sort((a, b) => a.year - b.year);
    const x = group.map((r) => r.year);
    const y = group.map((r) => r.burden_ratio_p50);
    const se = group.map((r) => r.burden_ratio_p50_se ?? 0);
    const half = se.map((s) => 1.645 * s);

    const color = PALETTE[i % PALETTE.length];
    traces.push({
      type: "scatter",
      mode: "lines+markers",
      name: TENURE_LABEL[tenure] ?? tenure,
      x, y,
      error_y: { type: "data", array: half, visible: true, color, thickness: 1.4, width: 4 },
      line: { color, width: 2 },
      marker: { color, size: 7 },
      hovertemplate:
        "%{x}<br>" +
        "burden ratio: %{y:.3f}<br>" +
        "90%% CI half: ±%{error_y.array:.3f}<extra>%{fullData.name}</extra>",
    });
    i += 1;
  }

  const countyName = rows[0].county_name;
  const layout = {
    ...PLOTLY_LAYOUT_BASE,
    title: { text: `${countyName} County -- burden ratio over time (${state.product})`, font: { size: 14 } },
    xaxis: { ...PLOTLY_LAYOUT_BASE.xaxis, title: "year", dtick: 1 },
    yaxis: {
      ...PLOTLY_LAYOUT_BASE.yaxis,
      title: "burden ratio (median monthly cost / income)",
      tickformat: ".2f",
      range: [0, Math.max(0.6, ...rows.map((r) => (r.burden_ratio_p50 ?? 0) + 1.645 * (r.burden_ratio_p50_se ?? 0))) * 1.1],
    },
    shapes: [
      {  // HUD cost-burdened threshold = 0.30
        type: "line", xref: "paper", x0: 0, x1: 1,
        y0: 0.30, y1: 0.30,
        line: { color: "#e2c365", width: 1, dash: "dot" },
      },
      {  // HUD severely cost-burdened threshold = 0.50
        type: "line", xref: "paper", x0: 0, x1: 1,
        y0: 0.50, y1: 0.50,
        line: { color: "#e07a7a", width: 1, dash: "dot" },
      },
    ],
    annotations: [
      { xref: "paper", x: 1.0, xanchor: "right", y: 0.30, yanchor: "bottom",
        text: "HUD: cost-burdened (0.30)", showarrow: false,
        font: { color: "#e2c365", size: 11 } },
      { xref: "paper", x: 1.0, xanchor: "right", y: 0.50, yanchor: "bottom",
        text: "HUD: severely (0.50)", showarrow: false,
        font: { color: "#e07a7a", size: 11 } },
    ],
  };

  await Plotly.react(div, traces, layout, PLOTLY_CONFIG);
  const totalSampleN = rows.reduce((s, r) => s + r.sample_n, 0);
  statusEl.textContent =
    `${rows.length} cells across ${byTenure.size} tenure(s); total sample_n = ${totalSampleN.toLocaleString()}.`;
}

// ---------------------------------------------------------------------------
// Compare view -- one year, all counties for a tenure, ranked horizontal bars.
// ---------------------------------------------------------------------------

async function renderCompare(state, statusEl) {
  const div = document.getElementById("chart-compare");
  if (!state.tenure) {
    statusEl.textContent = "Select a tenure.";
    Plotly.purge(div);
    return;
  }
  statusEl.textContent = "Loading...";
  // The /pums-burden-county endpoint returns the LATEST year for the
  // selected product. Year selector is informational only -- to honor a
  // year choice we hit the series endpoint and filter client-side. This
  // keeps both endpoints simple.
  const seriesRows = await apiGet("/pums-burden-county-series", {
    tenure: state.tenure,
    product: state.product,
  });
  const rows = seriesRows.filter((r) => r.year === state.year);
  if (rows.length === 0) {
    statusEl.textContent = "No rows for this year/tenure/product.";
    Plotly.purge(div);
    return;
  }

  rows.sort((a, b) => (a.burden_ratio_p50 ?? 0) - (b.burden_ratio_p50 ?? 0));
  const y = rows.map((r) => r.county_name);
  const x = rows.map((r) => r.burden_ratio_p50);
  const se = rows.map((r) => r.burden_ratio_p50_se ?? 0);
  const half = se.map((s) => 1.645 * s);
  // Color by burden severity.
  const colors = x.map((v) => {
    if (v == null) return "#666";
    if (v >= 0.50) return "#e07a7a";
    if (v >= 0.30) return "#e2c365";
    return "#71c787";
  });

  const trace = {
    type: "bar",
    orientation: "h",
    x, y,
    marker: { color: colors },
    error_x: { type: "data", array: half, visible: true, color: "#8a939e", thickness: 1.2, width: 4 },
    hovertemplate:
      "%{y}<br>" +
      "burden ratio: %{x:.3f}<br>" +
      "90%% CI half: ±%{error_x.array:.3f}<extra></extra>",
  };

  const layout = {
    ...PLOTLY_LAYOUT_BASE,
    title: {
      text: `${state.year} ${state.product} -- ${TENURE_LABEL[state.tenure]} burden ratio by NJ county`,
      font: { size: 14 },
    },
    xaxis: { ...PLOTLY_LAYOUT_BASE.xaxis, title: "burden ratio", tickformat: ".2f" },
    yaxis: { ...PLOTLY_LAYOUT_BASE.yaxis, automargin: true },
    shapes: [
      { type: "line", yref: "paper", x0: 0.30, x1: 0.30, y0: 0, y1: 1,
        line: { color: "#e2c365", width: 1, dash: "dot" } },
      { type: "line", yref: "paper", x0: 0.50, x1: 0.50, y0: 0, y1: 1,
        line: { color: "#e07a7a", width: 1, dash: "dot" } },
    ],
  };

  await Plotly.react(div, [trace], layout, PLOTLY_CONFIG);
  statusEl.textContent =
    `${rows.length} counties, year=${state.year}, product=${state.product}, tenure=${state.tenure}.`;
}

// ---------------------------------------------------------------------------
// Segments view -- one county, breakdown by chosen segment dim.
// ---------------------------------------------------------------------------

async function renderSegments(state, statusEl) {
  const div = document.getElementById("chart-segments");
  if (!state.county || !state.tenure) {
    statusEl.textContent = "Select a county and tenure.";
    Plotly.purge(div);
    return;
  }
  statusEl.textContent = "Loading...";
  const rows = await apiGet(`/pums-burden-county/${encodeURIComponent(state.county)}`, {
    dim: state.segment,
    tenure: state.tenure,
    product: state.product,
  });
  if (rows.length === 0) {
    statusEl.textContent = "No rows for this county/tenure/segment.";
    Plotly.purge(div);
    return;
  }
  rows.sort((a, b) => (a.burden_ratio_p50 ?? 0) - (b.burden_ratio_p50 ?? 0));

  const y = rows.map((r) => SEGMENT_VALUE_LABEL[r.segment_value] ?? r.segment_value);
  const x = rows.map((r) => r.burden_ratio_p50);
  const se = rows.map((r) => r.burden_ratio_p50_se ?? 0);
  const half = se.map((s) => 1.645 * s);
  const colors = x.map((v) => {
    if (v == null) return "#666";
    if (v >= 0.50) return "#e07a7a";
    if (v >= 0.30) return "#e2c365";
    return "#71c787";
  });
  const sample = rows.map((r) => r.sample_n);
  const weighted = rows.map((r) => r.weighted_n);

  const trace = {
    type: "bar",
    orientation: "h",
    x, y,
    marker: { color: colors },
    customdata: rows.map((r) => [r.sample_n, r.weighted_n, r.year]),
    error_x: { type: "data", array: half, visible: true, color: "#8a939e", thickness: 1.2, width: 4 },
    hovertemplate:
      "%{y}<br>" +
      "burden ratio: %{x:.3f} (±%{error_x.array:.3f} 90%% CI)<br>" +
      "sample_n: %{customdata[0]:,}<br>" +
      "weighted_n: %{customdata[1]:,}<br>" +
      "year: %{customdata[2]}<extra></extra>",
  };

  const countyName = rows[0].county_name;
  const yearLatest = Math.max(...rows.map((r) => r.year));
  const layout = {
    ...PLOTLY_LAYOUT_BASE,
    title: {
      text: `${countyName} -- ${TENURE_LABEL[state.tenure]} burden by ${state.segment} (${yearLatest} ${state.product})`,
      font: { size: 14 },
    },
    xaxis: { ...PLOTLY_LAYOUT_BASE.xaxis, title: "burden ratio", tickformat: ".2f" },
    yaxis: { ...PLOTLY_LAYOUT_BASE.yaxis, automargin: true },
    shapes: [
      { type: "line", yref: "paper", x0: 0.30, x1: 0.30, y0: 0, y1: 1,
        line: { color: "#e2c365", width: 1, dash: "dot" } },
      { type: "line", yref: "paper", x0: 0.50, x1: 0.50, y0: 0, y1: 1,
        line: { color: "#e07a7a", width: 1, dash: "dot" } },
    ],
  };
  await Plotly.react(div, [trace], layout, PLOTLY_CONFIG);
  const totalN = sample.reduce((s, n) => s + n, 0);
  statusEl.textContent =
    `${rows.length} segments; total sample_n = ${totalN.toLocaleString()}; weighted_n = ${weighted.reduce((s,n)=>s+n,0).toLocaleString()}.`;
}

// ---------------------------------------------------------------------------
// State + control wiring
// ---------------------------------------------------------------------------

const state = {
  view:    "trend",
  product: "acs5",
  tenure:  "renter",
  county:  "",
  segment: "race",
  year:    null,
  yearsAvailable: [],
};

function applyVisibility() {
  for (const ctl of document.querySelectorAll(".control[data-views]")) {
    const views = ctl.dataset.views.split(/\s+/);
    ctl.hidden = !views.includes(state.view);
  }
}

function setActiveTab(view) {
  for (const t of document.querySelectorAll(".tab")) {
    t.setAttribute("aria-selected", t.dataset.view === view ? "true" : "false");
  }
  for (const v of document.querySelectorAll(".view")) {
    v.classList.toggle("active", v.id === `view-${view}`);
  }
  state.view = view;
  applyVisibility();
}

async function populateCountyDropdown() {
  const sel = document.getElementById("ctl-county");
  try {
    const counties = await apiGet("/counties");
    sel.innerHTML = "";
    for (const c of counties) {
      const opt = document.createElement("option");
      opt.value = c.county_fips;
      opt.textContent = c.name;
      sel.appendChild(opt);
    }
    // Default: Bergen if present, else first.
    const bergen = counties.find((c) => c.name === "Bergen");
    state.county = bergen ? bergen.county_fips : (counties[0]?.county_fips ?? "");
    sel.value = state.county;
  } catch (e) {
    sel.innerHTML = `<option value="">(error: ${e.message})</option>`;
  }
}

async function populateYearDropdown() {
  const sel = document.getElementById("ctl-year");
  // Discover years from the series endpoint for the current product;
  // tenure=renter as a stable key (every product/tenure has the same
  // year coverage by construction of the materialization).
  const rows = await apiGet("/pums-burden-county-series", {
    tenure: "renter",
    product: state.product,
  });
  const years = [...new Set(rows.map((r) => r.year))].sort((a, b) => b - a);
  state.yearsAvailable = years;
  sel.innerHTML = "";
  for (const y of years) {
    const opt = document.createElement("option");
    opt.value = String(y);
    opt.textContent = String(y);
    sel.appendChild(opt);
  }
  state.year = years[0] ?? null;
  if (state.year !== null) sel.value = String(state.year);
}

async function rerender() {
  const statusMap = {
    trend:    document.getElementById("trend-status"),
    compare:  document.getElementById("compare-status"),
    segments: document.getElementById("segments-status"),
  };
  const statusEl = statusMap[state.view];
  statusEl.classList.remove("error");
  try {
    await waitForPlotly();
    if (state.view === "trend")    await renderTrend(state, statusEl);
    if (state.view === "compare")  await renderCompare(state, statusEl);
    if (state.view === "segments") await renderSegments(state, statusEl);
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
    statusEl.classList.add("error");
    console.error(e);
  }
}

function bindControls() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      setActiveTab(tab.dataset.view);
      rerender();
    });
  }

  document.getElementById("ctl-product").addEventListener("change", async (e) => {
    state.product = e.target.value;
    await populateYearDropdown();
    rerender();
  });
  document.getElementById("ctl-tenure").addEventListener("change", (e) => {
    state.tenure = e.target.value;
    rerender();
  });
  document.getElementById("ctl-county").addEventListener("change", (e) => {
    state.county = e.target.value;
    rerender();
  });
  document.getElementById("ctl-year").addEventListener("change", (e) => {
    state.year = e.target.value === "" ? null : Number(e.target.value);
    rerender();
  });
  document.getElementById("ctl-segment").addEventListener("change", (e) => {
    state.segment = e.target.value;
    rerender();
  });
  document.getElementById("btn-refresh").addEventListener("click", async () => {
    await Promise.all([
      refreshHealth(),
      refreshReleasePanel(),
      refreshFreshnessFooter(),
    ]);
    await rerender();
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function boot() {
  bindControls();
  applyVisibility();
  await Promise.all([
    refreshHealth(),
    populateCountyDropdown(),
    populateYearDropdown(),
    refreshReleasePanel(),
    refreshFreshnessFooter(),
  ]);
  await rerender();
}

boot().catch((e) => {
  const el = document.getElementById("trend-status");
  if (el) {
    el.textContent = `Boot error: ${e.message}`;
    el.classList.add("error");
  }
  console.error(e);
});
