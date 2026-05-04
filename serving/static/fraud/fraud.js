/*
 * Fraud / civic-integrity terminal client.
 *
 * Architecture
 * ------------
 * One module-scoped controller per tab. Each controller owns:
 *   - a Tabulator instance bound to a fixed columns spec
 *   - a reference to the tab's filter <form>
 *   - paginator state (limit, offset, total_count)
 *   - the export-CSV button's URL
 *
 * On filter submit, the controller serializes the form to a
 * URLSearchParams, fetches /fec/<endpoint>?<params>&limit=&offset=,
 * replaces the table data with the fetched rows, and rewires the
 * export button to <export endpoint>?<same params>.
 *
 * On row click, the row is forwarded to the side detail panel which
 * fetches the corresponding /fec/<endpoint>/{id} drill-down and
 * renders a key-value table.
 *
 * No build step. No bundler. Tabulator + a single global script.
 */

(function () {
  "use strict";

  // ==================================================================
  // Config
  // ==================================================================

  const PAGE_SIZE = 100;          // per-page row count (matches API default)
  const ENDPOINTS = {
    candidates:    "/fec/candidates",
    committees:    "/fec/committees",
    contributions: "/fec/contributions",
    "money-to-nj": "/fec/money-to-nj",
  };
  const EXPORTS = {
    candidates:    "/fec/export/candidates.csv",
    committees:    "/fec/export/committees.csv",
    contributions: "/fec/export/contributions.csv",
    "money-to-nj": "/fec/export/money-to-nj.csv",
  };

  // ==================================================================
  // Small utilities
  // ==================================================================

  /** Format an integer with thousands separators; null/undefined -> "-". */
  function fmtInt(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "-";
    return Number(n).toLocaleString("en-US");
  }

  /** Format a number as USD currency; null/undefined -> "-". */
  function fmtUsd(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "-";
    return Number(n).toLocaleString("en-US", {
      style: "currency", currency: "USD", maximumFractionDigits: 0,
    });
  }

  /** Convert a HTML form to a URLSearchParams, dropping empty values. */
  function formToParams(form) {
    const fd = new FormData(form);
    const out = new URLSearchParams();
    for (const [key, value] of fd.entries()) {
      // Checkboxes only appear in FormData when checked. For our
      // exclude_memo + has_candidate booleans we do want to send
      // both true and false; FastAPI defaults will fill in the
      // unchecked case.
      if (value !== null && value !== undefined && String(value).length) {
        out.set(key, String(value));
      }
    }
    // Treat unchecked exclude_memo as explicit "false" (the API default
    // is true, so we need to override). Tabulator+server defaults
    // handle the form on first load; on resubmission, an unchecked box
    // is absent from FormData, so we have to opt-out explicitly.
    const memo = form.querySelector('input[name="exclude_memo"]');
    if (memo && !memo.checked) out.set("exclude_memo", "false");
    return out;
  }

  /** Append &limit=&offset= to a URLSearchParams. */
  function withPagination(params, limit, offset) {
    const cloned = new URLSearchParams(params);
    cloned.set("limit",  String(limit));
    cloned.set("offset", String(offset));
    return cloned;
  }

  /** Render a key-value table inside the detail panel body. */
  function renderKv(obj) {
    const rows = Object.entries(obj || {})
      .filter(([, v]) => v !== null && v !== undefined && v !== "")
      .map(([k, v]) => {
        const cell = typeof v === "object" ? JSON.stringify(v) : String(v);
        return `<tr><td class="k">${escapeHtml(k)}</td><td class="v">${escapeHtml(cell)}</td></tr>`;
      })
      .join("");
    return `<table class="kv">${rows}</table>`;
  }

  /** Minimal HTML escape (we never trust raw FEC strings). */
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ==================================================================
  // Detail panel
  // ==================================================================

  const detailPanel = document.getElementById("detail-panel");
  const detailTitle = document.getElementById("detail-title");
  const detailBody  = document.getElementById("detail-body");
  document.getElementById("detail-close").addEventListener("click", () => {
    detailPanel.hidden = true;
  });

  /** Open the panel with a loading state, then fetch + render. */
  async function openDetail(title, fetchUrl, render) {
    detailTitle.textContent = title;
    detailBody.innerHTML = '<p class="empty-note">Loading&hellip;</p>';
    detailPanel.hidden = false;
    try {
      const resp = await fetch(fetchUrl);
      if (!resp.ok) {
        detailBody.innerHTML =
          `<p class="empty-note">Error ${resp.status}: ${escapeHtml(await resp.text())}</p>`;
        return;
      }
      const data = await resp.json();
      detailBody.innerHTML = render(data);
    } catch (err) {
      detailBody.innerHTML =
        `<p class="empty-note">Network error: ${escapeHtml(String(err))}</p>`;
    }
  }

  function renderCandidateDetail(d) {
    const head = renderKv({
      cand_id:               d.cand_id,
      cycle:                 d.cycle,
      cand_name:             d.cand_name,
      party:                 d.cand_pty_affiliation,
      office:                d.cand_office,
      state:                 d.cand_office_st,
      district:              d.cand_office_district,
      ici:                   d.cand_ici,
      status:                d.cand_status,
      principal_committee:   d.cand_pcc,
      election_year:         d.cand_election_yr,
      address:               [d.cand_st1, d.cand_st2, d.cand_city, d.cand_st, d.cand_zip]
                              .filter(Boolean).join(", "),
    });
    const c = (d.linked_committees || []);
    const cmtes = c.length === 0
      ? '<p class="empty-note">No linked committees in raw.fec_committee for this cycle.</p>'
      : c.map(x => renderKv({
          cmte_id:        x.cmte_id,
          name:           x.committee_name,
          designation:    x.cmte_dsgn,
          type:           x.cmte_tp,
          state:          x.cmte_st,
          treasurer:      x.treasurer_name,
        })).join("<hr/>");
    return `${head}<h3>Linked committees (${c.length})</h3>${cmtes}`;
  }

  function renderCommitteeDetail(d) {
    const head = renderKv({
      cmte_id:           d.cmte_id,
      cycle:             d.cycle,
      name:              d.committee_name,
      designation:       d.cmte_dsgn,
      type:              d.cmte_tp,
      party:             d.cmte_pty_affiliation,
      state:             d.cmte_st,
      address:           [d.cmte_st1, d.cmte_st2, d.cmte_city, d.cmte_st, d.cmte_zip]
                          .filter(Boolean).join(", "),
      treasurer:         d.treasurer_name,
      filing_freq:       d.cmte_filing_freq,
      org_type:          d.org_tp,
      connected_org:     d.connected_org_nm,
      candidate:         d.cand_id,
    });
    const cand = d.linked_candidate
      ? `<h3>Linked candidate</h3>${renderKv({
          cand_id:      d.linked_candidate.cand_id,
          cand_name:    d.linked_candidate.cand_name,
          party:        d.linked_candidate.cand_pty_affiliation,
          office:       d.linked_candidate.cand_office,
          state:        d.linked_candidate.cand_office_st,
          district:     d.linked_candidate.cand_office_district,
        })}`
      : "";
    const r = d.recent_contributions || [];
    const recent = r.length === 0
      ? '<h3>Recent contributions</h3><p class="empty-note">No itemized contributions loaded for this committee.</p>'
      : `<h3>Recent contributions (${r.length})</h3>` +
        r.map(x => renderKv({
            date:       x.transaction_date,
            amount:     fmtUsd(x.transaction_amount),
            donor:      x.contributor_name,
            donor_loc:  [x.contributor_city, x.contributor_state].filter(Boolean).join(", "),
            employer:   x.contributor_employer,
            occupation: x.contributor_occupation,
            type:       x.transaction_type,
            memo:       x.is_memo ? "yes" : "",
          })).join("<hr/>");
    return `${head}${cand}${recent}`;
  }

  // ==================================================================
  // Risk-score formatters (Tier 4 v3, L3a)
  // ==================================================================
  //
  // Score band thresholds are intentional: see fraud.css for the same
  // four-band scheme. KEEP IN SYNC if you tune one set.
  //   high   >= 80   -- immediate action band
  //   medium >= 60   -- worth a closer look
  //   low    >= 40   -- notable but bounded
  //   faint  >= 20   -- borderline
  //   zero   <  20   -- background noise

  function riskScoreBucket(s) {
    const n = Number(s);
    if (!Number.isFinite(n)) return "zero";
    if (n >= 80) return "high";
    if (n >= 60) return "medium";
    if (n >= 40) return "low";
    if (n >= 20) return "faint";
    return "zero";
  }

  /** Render a risk_score as a styled badge (used in the queue table). */
  function renderRiskScoreBadge(score) {
    const bucket = riskScoreBucket(score);
    const v = Number(score);
    const text = Number.isFinite(v) ? v.toFixed(2) : "—";
    return `<span class="risk-score-badge ${bucket}">${escapeHtml(text)}</span>`;
  }

  /** Render a severity (1..5) as a color-graded tag. */
  function renderSeverityTag(sev) {
    const n = Number(sev);
    const cls = `s${[1, 2, 3, 4, 5].includes(n) ? n : 1}`;
    return `<span class="severity-tag ${cls}">${escapeHtml(String(n))}</span>`;
  }

  /** Render a list of signal_ids as inline pills (queue's signals column).
   *  Truncates to 4 visible + "+N more" so the column stays scannable. */
  function renderSignalPills(signals) {
    const arr = Array.isArray(signals) ? signals : [];
    if (!arr.length) return '<span class="signal-pill more">none</span>';
    const head = arr.slice(0, 4)
      .map(s => `<span class="signal-pill">${escapeHtml(s)}</span>`).join("");
    const tail = arr.length > 4
      ? `<span class="signal-pill more">+${arr.length - 4}</span>` : "";
    return head + tail;
  }

  /** Render the score-share bar inside the panel's per-observation row.
   *  ``share`` is a [0, 100] percentage. */
  function renderShareCell(share) {
    const n = Number(share);
    if (!Number.isFinite(n) || n <= 0) {
      return `<span class="share-cell">
        <span class="bar"></span>
        <span class="label">0.0</span>
      </span>`;
    }
    const pct = Math.min(100, Math.max(0, n));
    let band = "low";
    if (pct >= 60) band = "high";
    else if (pct >= 30) band = "medium";
    return `<span class="share-cell">
      <span class="bar"><span class="${band}" style="width:${pct.toFixed(2)}%"></span></span>
      <span class="label">${pct.toFixed(1)}</span>
    </span>`;
  }

  /** Format a [0, 1] peer percentile as 3 decimals; null/undefined -> "-". */
  function fmtPct01(p) {
    const n = Number(p);
    if (!Number.isFinite(n)) return "-";
    return n.toFixed(3);
  }

  /** Format phi_contribution / raw_value to 4 decimals. */
  function fmtPhi(x) {
    if (x === null || x === undefined) return "-";
    const n = Number(x);
    return Number.isFinite(n) ? n.toFixed(4) : "-";
  }

  /** Render the side detail panel for a /fec/risk/entities/{kind}/{id}
   *  payload (RiskEntityPanel). */
  function renderRiskPanel(d) {
    const bucket = riskScoreBucket(d.risk_score);
    const score  = Number(d.risk_score);
    const scoreText = Number.isFinite(score) ? score.toFixed(2) : "—";

    const csvHref = "/fec/risk/entities/" +
      encodeURIComponent(d.entity_kind) + "/" +
      encodeURIComponent(d.entity_id) + "/csv" +
      (d.cycle ? `?cycle=${encodeURIComponent(d.cycle)}` : "");

    const head = `
      <div class="risk-panel-score">
        <span class="num ${bucket}">${escapeHtml(scoreText)}</span>
        <span class="label">risk score · 0–100</span>
      </div>
      <div class="risk-panel-actions">
        <a class="export" href="${csvHref}" target="_blank" rel="noopener">
          Export evidence CSV
        </a>
      </div>`;

    const summary = renderKv({
      entity_kind:        d.entity_kind,
      entity_id:          d.entity_id,
      cycle:              d.cycle,
      n_signals_fired:    d.n_signals_fired,
      max_severity:       d.max_severity,
      max_peer_percentile: fmtPct01(d.max_peer_percentile),
      avg_peer_percentile: fmtPct01(d.avg_peer_percentile),
      primary_peer_bucket: d.primary_peer_bucket,
      last_observation_at: d.last_observation_at,
    });

    const obs = Array.isArray(d.observations) ? d.observations : [];
    const rows = obs.map(o => `
      <tr>
        <td class="signal">
          <a href="${escapeHtml(o.evidence_url || "#")}"
             target="_blank" rel="noopener">${escapeHtml(o.signal_id)}</a>
        </td>
        <td class="num">${renderSeverityTag(o.severity)}</td>
        <td class="num">${escapeHtml(fmtPct01(o.peer_percentile))}</td>
        <td class="bucket">${escapeHtml(o.peer_bucket || "")}</td>
        <td class="num">${escapeHtml(fmtPhi(o.raw_value))}</td>
        <td class="num">${escapeHtml(fmtPhi(o.phi_contribution))}</td>
        <td class="num">${renderShareCell(o.score_share_pct)}</td>
      </tr>
    `).join("");

    const table = obs.length === 0
      ? '<p class="empty-note">No fired signals.</p>'
      : `<table class="risk-evidence">
          <thead><tr>
            <th>signal</th>
            <th class="num">sev</th>
            <th class="num">peer pct</th>
            <th>peer bucket</th>
            <th class="num">raw</th>
            <th class="num">phi</th>
            <th class="num">share %</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`;

    return `${head}<h3>Entity</h3>${summary}<h3>Observations (${obs.length})</h3>${table}`;
  }

  function renderContributionDetail(d) {
    return renderKv({
      sub_id:            d.sub_id,
      cycle:             d.cycle,
      cmte_id:           d.cmte_id,
      transaction_date:  d.transaction_date,
      amount:            fmtUsd(d.transaction_amount),
      transaction_type:  d.transaction_type,
      primary_general:   d.transaction_primary_general,
      memo:              d.is_memo ? "yes" : "no",
      donor:             d.contributor_name,
      donor_address:     [d.contributor_city, d.contributor_state, d.contributor_zip]
                          .filter(Boolean).join(", "),
      employer:          d.contributor_employer,
      occupation:        d.contributor_occupation,
      entity_type:       d.contributor_entity_type,
    });
  }

  // ==================================================================
  // Tabulator column specs
  // ==================================================================

  const COLS = {
    candidates: [
      { title: "Cycle",     field: "cycle",                width: 70 },
      { title: "Cand ID",   field: "cand_id",              width: 110, cssClass: "row-id-link" },
      { title: "Name",      field: "cand_name",            minWidth: 200 },
      { title: "Party",     field: "cand_pty_affiliation", width: 70 },
      { title: "Office",    field: "cand_office",          width: 70 },
      { title: "State",     field: "cand_office_st",       width: 70 },
      { title: "District",  field: "cand_office_district", width: 80 },
      { title: "ICI",       field: "cand_ici",             width: 60 },
      { title: "Status",    field: "cand_status",          width: 70 },
      { title: "PCC",       field: "cand_pcc",             width: 110 },
    ],
    committees: [
      { title: "Cycle",       field: "cycle",                width: 70 },
      { title: "Cmte ID",     field: "cmte_id",              width: 110, cssClass: "row-id-link" },
      { title: "Committee",   field: "committee_name",       minWidth: 240 },
      { title: "Treasurer",   field: "treasurer_name",       minWidth: 180 },
      { title: "State",       field: "cmte_st",              width: 70 },
      { title: "Designation", field: "cmte_dsgn",            width: 90 },
      { title: "Type",        field: "cmte_tp",              width: 70 },
      { title: "Party",       field: "cmte_pty_affiliation", width: 70 },
      { title: "Cand ID",     field: "cand_id",              width: 110 },
    ],
    contributions: [
      { title: "Date",       field: "transaction_date",         width: 100 },
      { title: "Amount",     field: "transaction_amount",       width: 110, hozAlign: "right",
        formatter: (cell) => fmtUsd(cell.getValue()) },
      { title: "Donor",      field: "contributor_name",         minWidth: 200 },
      { title: "City",       field: "contributor_city",         width: 130 },
      { title: "St",         field: "contributor_state",        width: 50 },
      { title: "Zip",        field: "contributor_zip",          width: 80 },
      { title: "Employer",   field: "contributor_employer",     minWidth: 180 },
      { title: "Occupation", field: "contributor_occupation",   minWidth: 160 },
      { title: "Cmte",       field: "cmte_id",                  width: 110, cssClass: "row-id-link" },
      { title: "Type",       field: "transaction_type",         width: 70 },
      { title: "P/G",        field: "transaction_primary_general", width: 60 },
      { title: "Memo",       field: "is_memo",                  width: 60,
        formatter: (cell) => cell.getValue() ? "X" : "" },
      { title: "Sub ID",     field: "sub_id",                   width: 160, cssClass: "row-id-link" },
    ],
    "money-to-nj": [
      { title: "Date",       field: "transaction_date",         width: 100 },
      { title: "Amount",     field: "transaction_amount",       width: 110, hozAlign: "right",
        formatter: (cell) => fmtUsd(cell.getValue()) },
      { title: "Cand",       field: "cand_name",                minWidth: 180 },
      { title: "Party",      field: "cand_pty_affiliation",     width: 70 },
      { title: "Off",        field: "cand_office",              width: 50 },
      { title: "Dist",       field: "cand_office_district",     width: 60 },
      { title: "Cmte",       field: "committee_name",           minWidth: 200 },
      { title: "Designation", field: "cmte_dsgn",               width: 90 },
      { title: "Donor",      field: "contributor_name",         minWidth: 180 },
      { title: "St",         field: "contributor_state",        width: 50 },
      { title: "Employer",   field: "contributor_employer",     minWidth: 160 },
      { title: "Occupation", field: "contributor_occupation",   minWidth: 160 },
      { title: "Memo",       field: "is_memo",                  width: 60,
        formatter: (cell) => cell.getValue() ? "X" : "" },
      { title: "Sub ID",     field: "sub_id",                   width: 160, cssClass: "row-id-link" },
    ],
  };

  // ==================================================================
  // Per-tab controller
  // ==================================================================

  function makeController(tabKey) {
    const form     = document.getElementById(`filter-${tabKey}`);
    const tableEl  = document.getElementById(`table-${tabKey}`);
    const pagerEl  = document.getElementById(`pager-${tabKey}`);
    const exportEl = document.getElementById(`export-${tabKey}`);
    const endpoint = ENDPOINTS[tabKey];
    const exportEp = EXPORTS[tabKey];

    let state = { params: new URLSearchParams(), limit: PAGE_SIZE, offset: 0, total: 0 };

    // Build the Tabulator first so column widths render even before
    // data lands.
    const table = new Tabulator(tableEl, {
      layout: "fitDataStretch",
      columns: COLS[tabKey],
      placeholder: "No matching rows. Adjust filters and re-apply.",
      reactiveData: false,
      virtualDomBuffer: 300,
      rowFormatter: () => {/* no-op; styling lives in CSS */},
    });

    // Row click -> open detail panel for ID-bearing tables.
    table.on("rowClick", (e, row) => {
      const data = row.getData();
      if (tabKey === "candidates" && data.cand_id) {
        const url = `/fec/candidates/${encodeURIComponent(data.cand_id)}` +
                    (data.cycle ? `?cycle=${encodeURIComponent(data.cycle)}` : "");
        openDetail(`Candidate ${data.cand_id}`, url, renderCandidateDetail);
      } else if (tabKey === "committees" && data.cmte_id) {
        const url = `/fec/committees/${encodeURIComponent(data.cmte_id)}` +
                    (data.cycle ? `?cycle=${encodeURIComponent(data.cycle)}` : "");
        openDetail(`Committee ${data.cmte_id}`, url, renderCommitteeDetail);
      } else if ((tabKey === "contributions" || tabKey === "money-to-nj") && data.sub_id) {
        // No /fec/contribution/{sub_id} endpoint by design (it would
        // be a row-by-row PK fetch with no extra info than the row
        // itself). Render the row's data inline.
        detailTitle.textContent = `Contribution ${data.sub_id}`;
        detailBody.innerHTML = renderContributionDetail(data);
        detailPanel.hidden = false;
      }
    });

    function renderPager() {
      const start = state.total === 0 ? 0 : state.offset + 1;
      const end   = Math.min(state.offset + state.limit, state.total);
      const prevDisabled = state.offset === 0;
      const nextDisabled = state.offset + state.limit >= state.total;
      pagerEl.innerHTML = `
        <button data-pg="first" ${prevDisabled ? "disabled" : ""}>&laquo; first</button>
        <button data-pg="prev"  ${prevDisabled ? "disabled" : ""}>&lsaquo; prev</button>
        <button data-pg="next"  ${nextDisabled ? "disabled" : ""}>next &rsaquo;</button>
        <span class="pager-meta">
          ${fmtInt(start)}&ndash;${fmtInt(end)} of ${fmtInt(state.total)}
        </span>`;
      pagerEl.querySelectorAll("button[data-pg]").forEach(b => {
        b.addEventListener("click", () => {
          if (b.dataset.pg === "first") state.offset = 0;
          if (b.dataset.pg === "prev")  state.offset = Math.max(0, state.offset - state.limit);
          if (b.dataset.pg === "next")  state.offset += state.limit;
          load();
        });
      });
    }

    async function load() {
      const url = `${endpoint}?${withPagination(state.params, state.limit, state.offset)}`;
      try {
        const resp = await fetch(url);
        if (!resp.ok) {
          table.setData([]);
          pagerEl.innerHTML = `<span class="pager-meta">Error ${resp.status}</span>`;
          return;
        }
        const env = await resp.json();
        state.total = env.total_count || 0;
        await table.setData(env.rows || []);
        renderPager();
      } catch (err) {
        console.error("fraud:load error", err);
        pagerEl.innerHTML = `<span class="pager-meta">Network error</span>`;
      }
      // Wire export button to current params
      exportEl.href = `${exportEp}?${state.params.toString()}`;
    }

    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      state.params = formToParams(form);
      state.offset = 0;
      load();
    });
    form.addEventListener("reset", () => {
      // Defer until the form values actually clear (reset event fires before).
      setTimeout(() => {
        state.params = new URLSearchParams();
        state.offset = 0;
        load();
      }, 0);
    });

    return { load, table };
  }

  // ==================================================================
  // Bootstrap
  // ==================================================================

  function activateTab(tabKey) {
    document.querySelectorAll(".tab-button").forEach(b => {
      b.classList.toggle("active", b.dataset.tab === tabKey);
    });
    document.querySelectorAll(".tab-pane").forEach(p => {
      p.classList.toggle("active", p.id === `pane-${tabKey}`);
    });
  }

  document.querySelectorAll(".tab-button").forEach(b => {
    b.addEventListener("click", () => activateTab(b.dataset.tab));
  });

  /** Fill every <select data-fill="X"> with the API's distinct values. */
  async function populateDropdowns() {
    const sources = {
      cycles:  "/fec/cycles",
      states:  "/fec/states",
      parties: "/fec/parties",
      offices: "/fec/offices",
    };
    const cache = {};
    for (const [key, url] of Object.entries(sources)) {
      try {
        const resp = await fetch(url);
        cache[key] = resp.ok ? await resp.json() : [];
      } catch (err) {
        console.warn("dropdown fetch failed", key, err);
        cache[key] = [];
      }
    }
    document.querySelectorAll("select[data-fill]").forEach(sel => {
      const key = sel.getAttribute("data-fill");
      const items = cache[key] || [];
      // Preserve the existing first option ("all"); append distincts.
      for (const it of items) {
        const o = document.createElement("option");
        o.value = it.value;
        o.textContent = `${it.value} (${fmtInt(it.count)})`;
        sel.appendChild(o);
      }
    });
  }

  /** Fetch /fec/summary and paint the header strip. */
  async function paintSummary() {
    try {
      const resp = await fetch("/fec/summary");
      if (!resp.ok) return;
      const s = await resp.json();
      document.getElementById("sum-cycle").textContent = s.cycle || "(no data)";
      document.getElementById("sum-cands").textContent =
        `${fmtInt(s.candidates_nj)} / ${fmtInt(s.candidates_total)}`;
      document.getElementById("sum-cmtes").textContent =
        `${fmtInt(s.committees_nj_domiciled)} / ${fmtInt(s.committees_total)}`;
      document.getElementById("sum-contribs").textContent =
        `${fmtInt(s.contributions_nj_donor)} / ${fmtInt(s.contributions_total)}`;
      document.getElementById("sum-to-nj").textContent =
        fmtInt(s.contributions_to_nj_candidates);

      // Show the contributions empty-state banner when raw is empty.
      const banner = document.getElementById("contribs-empty-banner");
      if (s.contributions_total === 0) banner.hidden = false;
    } catch (err) {
      console.warn("summary fetch failed", err);
    }
  }

  // Tabulator is loaded via a deferred CDN script. We wait for it to
  // be defined before constructing controllers; if we don't, the
  // ReferenceError surfaces silently in the console.
  function whenTabulatorReady(cb) {
    if (typeof Tabulator !== "undefined") return cb();
    let tries = 0;
    const iv = setInterval(() => {
      tries++;
      if (typeof Tabulator !== "undefined") {
        clearInterval(iv);
        cb();
      } else if (tries > 50) {
        clearInterval(iv);
        console.error("Tabulator did not load from CDN after 5s");
      }
    }, 100);
  }

  // ==================================================================
  // Fraud-metrics tab controller
  // ==================================================================
  //
  // Different shape from the per-table controllers above: every metric
  // returns its own row schema (treasurer name + n_committees vs.
  // candidate id + name vs. address + count). We compute the
  // Tabulator columns dynamically from the first row's keys instead
  // of hard-coding them. This keeps adding a new metric to a single
  // SQL view + one catalog entry; no JS change required.
  //
  // The metric-id and cycle live in the URL hash so the page is
  // deep-linkable: /fraud#m=treasurer_concentration&cycle=2024 jumps
  // straight to that signal at that cycle.

  function makeMetricsController() {
    const sidebarEl = document.getElementById("metrics-list");
    const headerEl  = document.getElementById("metrics-header");
    const tableEl   = document.getElementById("table-metric");
    const pagerEl   = document.getElementById("pager-metric");
    const titleEl   = document.getElementById("metric-title");
    const tierEl    = document.getElementById("metric-tier");
    const descEl    = document.getElementById("metric-description");
    const threshEl  = document.getElementById("metric-threshold");
    const cycleEl   = document.getElementById("metric-cycle");
    const sortEl    = document.getElementById("metric-sort");
    const sortDirEl = document.getElementById("metric-sort-dir");
    const applyEl   = document.getElementById("metric-apply");
    const exportEl  = document.getElementById("metric-export");
    const bannerEl  = document.getElementById("metric-empty-banner");

    let catalog = [];
    let counts  = {};
    let activeId = null;
    let state   = { cycle: "", limit: PAGE_SIZE, offset: 0, total: 0,
                    sortBy: "", sortDir: "DESC" };

    const table = new Tabulator(tableEl, {
      layout: "fitDataStretch",
      placeholder: "Select a signal from the sidebar.",
      reactiveData: false,
      virtualDomBuffer: 300,
      columns: [],
    });

    /** Render one Tabulator column per key in the row. Arrays + numbers
     *  get specific formatters; everything else is a string. */
    function columnsFor(rows) {
      if (!rows.length) return [];
      const sample = rows[0];
      return Object.keys(sample).map(field => {
        const v = sample[field];
        if (Array.isArray(v)) {
          return {
            title: field, field, width: 220,
            formatter: cell => {
              const arr = cell.getValue() || [];
              if (!arr.length) return "";
              const head = arr.slice(0, 3).join(", ");
              return arr.length > 3
                ? `${head}, +${arr.length - 3} more`
                : head;
            },
            tooltip: cell => (cell.getValue() || []).join(", "),
          };
        }
        if (typeof v === "number") {
          return {
            title: field, field, hozAlign: "right", width: 120,
            formatter: c => fmtInt(c.getValue()),
          };
        }
        // Cycle gets a fixed narrow width; identifiers get a slightly
        // wider column. Everything else auto-sizes via fitDataStretch.
        if (field === "cycle") return { title: field, field, width: 70 };
        if (field.endsWith("_id") || field === "missing_cmte_id") {
          return { title: field, field, width: 120, cssClass: "row-id-link" };
        }
        return { title: field, field };
      });
    }

    function renderSidebar() {
      sidebarEl.innerHTML = "";
      let lastTier = null;
      for (const m of catalog) {
        if (m.tier !== lastTier) {
          const div = document.createElement("li");
          div.className = "tier-divider";
          div.textContent = m.tier === "structural"
            ? "Structural signals (cn / cm)"
            : "Contribution signals (indiv)";
          sidebarEl.appendChild(div);
          lastTier = m.tier;
        }
        const li = document.createElement("li");
        li.dataset.metricId = m.id;
        if (m.id === activeId) li.classList.add("active");
        const count = counts[m.id];
        li.innerHTML = `
          <div class="metric-name">${escapeHtml(m.name)}</div>
          <div class="metric-meta">
            <span>${escapeHtml(m.id)}</span>
            <span class="metric-count">${count == null ? "&hellip;" : fmtInt(count)}</span>
          </div>`;
        li.addEventListener("click", () => selectMetric(m.id));
        sidebarEl.appendChild(li);
      }
    }

    function renderHeader(metric) {
      headerEl.hidden = false;
      titleEl.textContent = metric.name;
      tierEl.textContent  = metric.tier;
      descEl.textContent  = metric.description;
      if (metric.threshold_note) {
        threshEl.hidden = false;
        threshEl.textContent = "Threshold: " + metric.threshold_note;
      } else {
        threshEl.hidden = true;
        threshEl.textContent = "";
      }
    }

    function renderPager() {
      const start = state.total === 0 ? 0 : state.offset + 1;
      const end   = Math.min(state.offset + state.limit, state.total);
      const prevDisabled = state.offset === 0;
      const nextDisabled = state.offset + state.limit >= state.total;
      pagerEl.innerHTML = `
        <button data-pg="first" ${prevDisabled ? "disabled" : ""}>&laquo; first</button>
        <button data-pg="prev"  ${prevDisabled ? "disabled" : ""}>&lsaquo; prev</button>
        <button data-pg="next"  ${nextDisabled ? "disabled" : ""}>next &rsaquo;</button>
        <span class="pager-meta">
          ${fmtInt(start)}&ndash;${fmtInt(end)} of ${fmtInt(state.total)}
        </span>`;
      pagerEl.querySelectorAll("button[data-pg]").forEach(b => {
        b.addEventListener("click", () => {
          if (b.dataset.pg === "first") state.offset = 0;
          if (b.dataset.pg === "prev")  state.offset = Math.max(0, state.offset - state.limit);
          if (b.dataset.pg === "next")  state.offset += state.limit;
          loadRows();
        });
      });
    }

    function buildParams() {
      const p = new URLSearchParams();
      if (state.cycle)  p.set("cycle",   state.cycle);
      if (state.sortBy) p.set("sort_by", state.sortBy);
      if (state.sortDir) p.set("sort_dir", state.sortDir);
      return p;
    }

    async function loadRows() {
      if (!activeId) return;
      const p = buildParams();
      p.set("limit",  String(state.limit));
      p.set("offset", String(state.offset));
      const url = `/fec/metrics/${encodeURIComponent(activeId)}?${p.toString()}`;
      try {
        const resp = await fetch(url);
        if (!resp.ok) {
          table.setData([]);
          pagerEl.innerHTML = `<span class="pager-meta">Error ${resp.status}</span>`;
          return;
        }
        const env = await resp.json();
        state.total = env.total_count || 0;
        // Reapply column spec each load: a different metric with a
        // different row shape needs a fresh columns array.
        table.setColumns(columnsFor(env.rows || []));
        await table.setData(env.rows || []);
        renderPager();
        if (state.total === 0) {
          bannerEl.hidden = false;
          bannerEl.innerHTML = "<strong>No flagged rows for this metric at this cycle.</strong> "
                             + "Try widening the cycle filter, or pick a different signal.";
        } else {
          bannerEl.hidden = true;
        }
      } catch (err) {
        console.error("metrics:loadRows error", err);
        pagerEl.innerHTML = `<span class="pager-meta">Network error</span>`;
      }
      // Wire export to current state
      const ep = buildParams();
      exportEl.href = `/fec/metrics/${encodeURIComponent(activeId)}/csv?${ep.toString()}`;
    }

    async function selectMetric(metricId) {
      const metric = catalog.find(m => m.id === metricId);
      if (!metric) return;
      activeId = metricId;
      state.offset = 0;
      state.sortBy  = metric.sort_default;
      state.sortDir = "DESC";

      // Reflect into URL hash for deep-linking
      const hash = new URLSearchParams();
      hash.set("m", activeId);
      if (state.cycle) hash.set("cycle", state.cycle);
      window.location.hash = hash.toString();

      // Refresh sidebar active styling
      sidebarEl.querySelectorAll("li[data-metric-id]").forEach(li => {
        li.classList.toggle("active", li.dataset.metricId === activeId);
      });

      // Repopulate sort selector with the metric's allowed sort cols.
      // The catalog only carries sort_default; the full whitelist lives
      // server-side. We derive the allowed columns from the row keys
      // after the first load, but we need *something* in the dropdown
      // for the first render -- start with sort_default only.
      sortEl.innerHTML = "";
      const opt = document.createElement("option");
      opt.value = metric.sort_default;
      opt.textContent = metric.sort_default;
      opt.selected = true;
      sortEl.appendChild(opt);

      renderHeader(metric);
      await loadRows();

      // After loading rows, the column keys ARE the candidate sort
      // columns. Repopulate the selector with all columns, preserving
      // the current selection. Server-side whitelist will reject any
      // non-allowed column with a 400, which is fine: the user picks
      // again.
      const data = table.getData();
      if (data.length) {
        const cols = Object.keys(data[0]);
        const cur = state.sortBy;
        sortEl.innerHTML = "";
        for (const c of cols) {
          const o = document.createElement("option");
          o.value = c;
          o.textContent = c;
          if (c === cur) o.selected = true;
          sortEl.appendChild(o);
        }
      }
    }

    applyEl.addEventListener("click", () => {
      state.cycle   = cycleEl.value;
      state.sortBy  = sortEl.value;
      state.sortDir = sortDirEl.value || "DESC";
      state.offset  = 0;
      loadRows();
    });

    /** Refetch summary counts and update sidebar pills. */
    async function refreshCounts() {
      try {
        const url = state.cycle
          ? `/fec/metrics/_summary?cycle=${encodeURIComponent(state.cycle)}`
          : "/fec/metrics/_summary";
        const resp = await fetch(url);
        if (!resp.ok) return;
        const env = await resp.json();
        counts = env.counts || {};
        renderSidebar();
      } catch (err) {
        console.warn("metrics:refreshCounts", err);
      }
    }

    cycleEl.addEventListener("change", () => {
      state.cycle = cycleEl.value;
      refreshCounts();
    });

    async function init() {
      // Catalog
      try {
        const resp = await fetch("/fec/metrics");
        catalog = resp.ok ? await resp.json() : [];
      } catch (err) {
        console.warn("metrics:catalog fetch", err);
        catalog = [];
      }
      // Default cycle from URL hash (if any), else the first cycle
      // returned by the cycles dropdown (the most recent loaded cycle).
      const hashParams = new URLSearchParams(
        (window.location.hash || "").replace(/^#/, ""),
      );
      state.cycle = hashParams.get("cycle") || cycleEl.value || "";
      cycleEl.value = state.cycle;

      await refreshCounts();
      renderSidebar();
      const initial = hashParams.get("m")
                    || (catalog[0] && catalog[0].id);
      if (initial) await selectMetric(initial);
    }

    return { init };
  }

  // ==================================================================
  // Risk-queue controller (Tier 4 v3, L3a) -- the default landing tab
  // ==================================================================
  //
  // GET /fec/risk/entities returns a uniform shape across all five
  // entity_kinds, so unlike the metric explorer we can hard-code the
  // Tabulator columns. Row click -> open the side detail panel with
  // GET /fec/risk/entities/{kind}/{id} (RiskEntityPanel).
  //
  // The filter knobs (cycle, entity_kind, signal_id, min_score,
  // max_score) live in the URL hash so the page is deep-linkable:
  // /fraud#risk=cycle=2024&min=60&kind=treasurer jumps right to the
  // filtered view. We use a separate hash key ("risk=") from the
  // metric-explorer hash ("m=") so deep links to either tab survive.

  function makeRiskController() {
    const form        = document.getElementById("filter-risk");
    const tableEl     = document.getElementById("table-risk");
    const pagerEl     = document.getElementById("pager-risk");
    const bannerEl    = document.getElementById("risk-empty-banner");
    const cycleEl     = document.getElementById("risk-cycle");
    const kindEl      = document.getElementById("risk-entity-kind");
    const signalEl    = document.getElementById("risk-signal-id");
    const minEl       = document.getElementById("risk-min-score");
    const maxEl       = document.getElementById("risk-max-score");
    const sortByEl    = document.getElementById("risk-sort-by");
    const sortDirEl   = document.getElementById("risk-sort-dir");

    const state = {
      cycle: "", entityKind: "", signalId: "",
      minScore: "", maxScore: "",
      sortBy: "risk_score", sortDir: "DESC",
      limit: PAGE_SIZE, offset: 0, total: 0,
    };

    const COLS_RISK = [
      { title: "Cycle",     field: "cycle",            width: 70 },
      { title: "Kind",      field: "entity_kind",      width: 110 },
      {
        title: "Entity ID", field: "entity_id",
        minWidth: 200, cssClass: "row-id-link",
      },
      {
        title: "Score", field: "risk_score",
        width: 100, hozAlign: "right",
        formatter: cell => renderRiskScoreBadge(cell.getValue()),
      },
      {
        title: "Sigs", field: "n_signals_fired",
        width: 60, hozAlign: "right",
        formatter: c => fmtInt(c.getValue()),
      },
      {
        title: "Max sev", field: "max_severity",
        width: 75, hozAlign: "right",
        formatter: c => renderSeverityTag(c.getValue()),
      },
      {
        title: "Max peer pct", field: "max_peer_percentile",
        width: 110, hozAlign: "right",
        formatter: c => fmtPct01(c.getValue()),
      },
      {
        title: "Peer bucket", field: "primary_peer_bucket",
        minWidth: 140,
      },
      {
        title: "Signals", field: "signals_fired",
        minWidth: 240,
        formatter: c => renderSignalPills(c.getValue()),
        tooltip: c => (c.getValue() || []).join(", "),
      },
    ];

    const table = new Tabulator(tableEl, {
      layout: "fitDataStretch",
      columns: COLS_RISK,
      placeholder: "No flagged entities. Adjust filters and re-apply.",
      reactiveData: false,
      virtualDomBuffer: 300,
    });

    table.on("rowClick", (e, row) => {
      const d = row.getData();
      if (!d.entity_kind || !d.entity_id) return;
      const url = `/fec/risk/entities/${encodeURIComponent(d.entity_kind)}/`
                + `${encodeURIComponent(d.entity_id)}`
                + (d.cycle ? `?cycle=${encodeURIComponent(d.cycle)}` : "");
      const title = `${d.entity_id} · ${d.entity_kind}`;
      openDetail(title, url, renderRiskPanel);
    });

    function reflectHash() {
      // Hash format: #risk=cycle=2024&kind=treasurer&signal=foo&min=60&max=100&sort=risk_score&dir=DESC
      // We deliberately keep the hash short (single-letter keys for
      // optional knobs) so the URL stays scannable.
      const hp = new URLSearchParams();
      if (state.cycle)      hp.set("cycle", state.cycle);
      if (state.entityKind) hp.set("kind",  state.entityKind);
      if (state.signalId)   hp.set("signal", state.signalId);
      if (state.minScore !== "") hp.set("min", state.minScore);
      if (state.maxScore !== "") hp.set("max", state.maxScore);
      if (state.sortBy && state.sortBy !== "risk_score")
        hp.set("sort", state.sortBy);
      if (state.sortDir && state.sortDir !== "DESC")
        hp.set("dir", state.sortDir);
      const qs = hp.toString();
      // Use a single risk= prefix so the metric-explorer tab can keep
      // its own m= hash without collision.
      window.location.hash = qs ? `risk&${qs}` : "risk";
    }

    function readHash() {
      const raw = (window.location.hash || "").replace(/^#/, "");
      // Accept "risk", "risk&cycle=...", or legacy "risk=cycle=..."
      // (we never emitted that, but be defensive about hand-edits).
      const stripped = raw.replace(/^risk[&=]?/, "");
      const hp = new URLSearchParams(stripped);
      if (hp.has("cycle"))  state.cycle      = hp.get("cycle") || "";
      if (hp.has("kind"))   state.entityKind = hp.get("kind") || "";
      if (hp.has("signal")) state.signalId   = hp.get("signal") || "";
      if (hp.has("min"))    state.minScore   = hp.get("min") || "";
      if (hp.has("max"))    state.maxScore   = hp.get("max") || "";
      if (hp.has("sort"))   state.sortBy     = hp.get("sort") || "risk_score";
      if (hp.has("dir"))    state.sortDir    = hp.get("dir") || "DESC";
    }

    function syncFormFromState() {
      cycleEl.value   = state.cycle;
      kindEl.value    = state.entityKind;
      signalEl.value  = state.signalId;
      minEl.value     = state.minScore;
      maxEl.value     = state.maxScore;
      sortByEl.value  = state.sortBy || "risk_score";
      sortDirEl.value = state.sortDir || "DESC";
    }

    function syncStateFromForm() {
      state.cycle      = cycleEl.value || "";
      state.entityKind = kindEl.value || "";
      state.signalId   = signalEl.value || "";
      state.minScore   = minEl.value || "";
      state.maxScore   = maxEl.value || "";
      state.sortBy     = sortByEl.value || "risk_score";
      state.sortDir    = sortDirEl.value || "DESC";
    }

    function buildParams() {
      const p = new URLSearchParams();
      if (state.cycle)      p.set("cycle",       state.cycle);
      if (state.entityKind) p.set("entity_kind", state.entityKind);
      if (state.signalId)   p.set("signal_id",   state.signalId);
      if (state.minScore !== "") p.set("min_score", state.minScore);
      if (state.maxScore !== "") p.set("max_score", state.maxScore);
      if (state.sortBy)  p.set("sort_by",  state.sortBy);
      if (state.sortDir) p.set("sort_dir", state.sortDir);
      return p;
    }

    function renderPager() {
      const start = state.total === 0 ? 0 : state.offset + 1;
      const end   = Math.min(state.offset + state.limit, state.total);
      const prevDisabled = state.offset === 0;
      const nextDisabled = state.offset + state.limit >= state.total;
      pagerEl.innerHTML = `
        <button data-pg="first" ${prevDisabled ? "disabled" : ""}>&laquo; first</button>
        <button data-pg="prev"  ${prevDisabled ? "disabled" : ""}>&lsaquo; prev</button>
        <button data-pg="next"  ${nextDisabled ? "disabled" : ""}>next &rsaquo;</button>
        <span class="pager-meta">
          ${fmtInt(start)}&ndash;${fmtInt(end)} of ${fmtInt(state.total)}
        </span>`;
      pagerEl.querySelectorAll("button[data-pg]").forEach(b => {
        b.addEventListener("click", () => {
          if (b.dataset.pg === "first") state.offset = 0;
          if (b.dataset.pg === "prev")  state.offset = Math.max(0, state.offset - state.limit);
          if (b.dataset.pg === "next")  state.offset += state.limit;
          load();
        });
      });
    }

    async function load() {
      const params = buildParams();
      params.set("limit",  String(state.limit));
      params.set("offset", String(state.offset));
      const url = `/fec/risk/entities?${params.toString()}`;
      try {
        const resp = await fetch(url);
        if (!resp.ok) {
          await table.setData([]);
          pagerEl.innerHTML = `<span class="pager-meta">Error ${resp.status}</span>`;
          bannerEl.hidden = false;
          bannerEl.innerHTML =
            `<strong>API error ${resp.status}.</strong> ${escapeHtml(await resp.text())}`;
          return;
        }
        const env = await resp.json();
        state.total = env.total_count || 0;
        await table.setData(env.rows || []);
        renderPager();
        if (state.total === 0) {
          bannerEl.hidden = false;
          bannerEl.innerHTML = "<strong>No entities match the current filters.</strong> "
                             + "Either widen the filter (lower min_score, broaden cycle), or "
                             + "materialize L1 first via "
                             + "<code>SELECT derived.refresh_all_fraud_signal_observations('2024')</code>.";
        } else {
          bannerEl.hidden = true;
        }
      } catch (err) {
        console.error("risk:load error", err);
        pagerEl.innerHTML = `<span class="pager-meta">Network error</span>`;
      }
    }

    /** Populate the signal-id dropdown from the metric catalog so the
     *  user can filter the queue to "fired this specific signal". */
    async function populateSignals() {
      try {
        const resp = await fetch("/fec/metrics");
        if (!resp.ok) return;
        const items = await resp.json();
        for (const m of items) {
          const o = document.createElement("option");
          o.value = m.id;
          o.textContent = `${m.id} (${m.tier})`;
          signalEl.appendChild(o);
        }
      } catch (err) {
        console.warn("risk:populateSignals", err);
      }
    }

    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      syncStateFromForm();
      state.offset = 0;
      reflectHash();
      load();
    });
    form.addEventListener("reset", () => {
      // Wait for the form's reset to actually clear values.
      setTimeout(() => {
        state.cycle = "";       state.entityKind = "";
        state.signalId = "";    state.minScore = "";
        state.maxScore = "";    state.sortBy = "risk_score";
        state.sortDir = "DESC"; state.offset = 0;
        syncFormFromState();
        reflectHash();
        load();
      }, 0);
    });

    async function init() {
      await populateSignals();
      readHash();
      syncFormFromState();
      await load();
    }

    return { init, load };
  }

  whenTabulatorReady(async () => {
    // populateDropdowns is fast (4 small distinct-value endpoints with
    // tiny indexes) so it stays in the critical path; the metrics tab
    // depends on the cycles dropdown for its default cycle filter.
    //
    // paintSummary intentionally runs WITHOUT await: with 58M+
    // contribution rows, /fec/summary can take 30+ seconds (it does
    // five cross-table COUNTs on raw.fec_contribution). Awaiting it
    // would block the risk tab init and the user would see an
    // empty page for the entire wait. Fire-and-forget keeps the
    // header strip showing "..." until the count lands while the
    // risk queue renders immediately.
    await populateDropdowns();
    paintSummary();

    // Risk queue is the new default landing tab. Init it eagerly so
    // the user lands on a populated table; metrics + per-table tabs
    // initialize lazily on first activation.
    const riskController = makeRiskController();
    await riskController.init();

    // Lazily build the metric explorer the first time the user opens
    // its tab; the catalog fetch + first-metric load is small but
    // not zero.
    let metricsController = null;
    async function ensureMetricsController() {
      if (metricsController) return metricsController;
      metricsController = makeMetricsController();
      await metricsController.init();
      return metricsController;
    }

    const controllers = {};
    for (const key of Object.keys(ENDPOINTS)) {
      controllers[key] = makeController(key);
    }

    // Lazy-load each non-default tab on first activation so we don't
    // pay the round-trips for tabs the user never opens.
    document.querySelectorAll(".tab-button").forEach(b => {
      b.addEventListener("click", async () => {
        const k = b.dataset.tab;
        if (k === "metrics") {
          await ensureMetricsController();
        } else if (controllers[k] && !controllers[k]._initialized) {
          controllers[k]._initialized = true;
          controllers[k].load();
        }
      });
    });
  });
})();
