import Link from "next/link";

import { isDbReachable } from "@/lib/db";
import { getH1bLaneSummary, listH1bEmployerLeads } from "@/lib/queries";
import { fmtScore } from "@/lib/format";
import type { H1bEmployerLead } from "@/lib/types";

export const dynamic = "force-dynamic";
export const revalidate = 0;

const PROGRAM_VERSION = "3.9.0-fraud-h1b-wage-floor-v1";

const SIGNAL_LABEL: Record<string, string> = {
  employer_below_prevailing_wage: "Below prevailing wage",
  employer_h1b_denial_rate_outlier: "Denial-rate tail",
  employer_lca_uscis_volume_gap: "LCA vs USCIS volume gap",
  employer_certified_withdrawn_rate_outlier: "Certified-withdrawn tail",
  employer_on_whd_willful_or_debarred: "WHD willful / debarred",
  employer_level1_wage_share_outlier: "Level I wage-share tail",
  employer_secondary_entity_share_outlier: "Secondary-entity tail",
  employer_h1b_dependent_plus_anomaly: "H-1B-dependent + anomaly",
  employer_wage_at_pw_floor_share_outlier: "At-prevailing-wage floor tail",
  employer_lca_willful_attestation: "LCA willful attestation",
};

function fmtUsd(n: number | null): string {
  if (n == null) return "—";
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

function fmtPct(n: number | null): string {
  if (n == null) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function fmtRatio(n: number | null): string {
  if (n == null) return "—";
  return `${n.toFixed(2)}×`;
}

export default async function H1bPage() {
  const reachable = await isDbReachable();
  if (!reachable.reachable) {
    return (
      <div className="rounded-md bg-amber-50 dark:bg-amber-950 p-4 text-sm text-amber-800 dark:text-amber-200">
        Database not reachable. Configure{" "}
        <code className="font-mono">NEON_DATABASE_URL</code>.
      </div>
    );
  }

  const [leads, summary] = await Promise.all([
    listH1bEmployerLeads({ limit: 80 }),
    getH1bLaneSummary(),
  ]);

  return (
    <article className="space-y-8">
      <header className="space-y-3">
        <p className="font-mono text-xs text-zinc-500">
          FRAUD-V1 · formula {PROGRAM_VERSION}
        </p>
        <h1 className="text-3xl font-semibold tracking-tight">
          H-1B employer leads — New Jersey
        </h1>
        <p className="max-w-3xl text-zinc-600 dark:text-zinc-400">
          Public-record screening of New Jersey H-1B employers from DOL OFLC
          Labor Condition Applications, the USCIS H-1B Employer Data Hub, and
          the DOL Wage and Hour willful-violator / debarment lists. These are{" "}
          <strong>leads</strong>, not findings. A risk score is a
          peer-percentile of anomalousness, not a probability of visa fraud.
        </p>
        <p className="text-sm text-zinc-500">
          {summary.n_employers} flagged{" "}
          {summary.n_employers === 1 ? "employer" : "employers"}
          {summary.latest_cycle ? ` · latest FY ${summary.latest_cycle}` : ""}
          {summary.n_below_pw
            ? ` · ${summary.n_below_pw} below-prevailing-wage`
            : ""}
        </p>
      </header>

      {leads.length === 0 ? (
        <EmptyState />
      ) : (
        <ul className="divide-y divide-zinc-200 dark:divide-zinc-800 rounded-lg border border-zinc-200 dark:border-zinc-800">
          {leads.map((lead) => (
            <LeadRow key={`${lead.cycle}-${lead.entity_id}`} lead={lead} />
          ))}
        </ul>
      )}

      <Methodology />
    </article>
  );
}

function EmptyState() {
  return (
    <div className="rounded-lg border border-dashed border-zinc-300 dark:border-zinc-700 p-8 text-sm text-zinc-600 dark:text-zinc-400">
      No H-1B employer leads materialized yet. Load NJ-filtered DOL OFLC LCA
      files (<code className="font-mono">nj-ingest-lca load --nj-only --replace</code>
      ), USCIS Employer Data Hub CSVs (
      <code className="font-mono">nj-ingest-uscis-h1b load</code>), and the
      WHD lists (<code className="font-mono">nj-ingest-whd-h1b</code>), then run{" "}
      <code className="font-mono">
        SELECT derived.refresh_all_fraud_signal_observations(&apos;2025&apos;)
      </code>
      .
    </div>
  );
}

function LeadRow({ lead }: { lead: H1bEmployerLead }) {
  const href = `/risk/employer/${encodeURIComponent(lead.entity_id)}?cycle=${lead.cycle}`;
  const name = lead.display_name ?? lead.entity_id;
  return (
    <li>
      <Link
        href={href}
        className="flex flex-col gap-1 px-4 py-3 hover:bg-zinc-50 dark:hover:bg-zinc-900 sm:flex-row sm:items-center sm:justify-between"
      >
        <div>
          <div className="font-medium">{name}</div>
          <div className="text-xs text-zinc-500">
            FY{lead.cycle}
            {lead.preview_signal_id
              ? ` · ${SIGNAL_LABEL[lead.preview_signal_id] ?? lead.preview_signal_id}`
              : ""}
            {` · ${lead.n_signals} signal${lead.n_signals === 1 ? "" : "s"}`}
          </div>
        </div>
        <div className="flex flex-wrap gap-3 text-xs font-mono text-zinc-600 dark:text-zinc-400">
          {lead.below_pw_gap_usd != null && (
            <span>gap {fmtUsd(lead.below_pw_gap_usd)}</span>
          )}
          {lead.denial_rate != null && (
            <span>deny {fmtPct(lead.denial_rate)}</span>
          )}
          {lead.lca_uscis_gap_ratio != null && (
            <span>LCA/USCIS {fmtRatio(lead.lca_uscis_gap_ratio)}</span>
          )}
          {lead.certified_withdrawn_rate != null && (
            <span>CW {fmtPct(lead.certified_withdrawn_rate)}</span>
          )}
          {lead.on_whd_list != null && <span>WHD list</span>}
          {lead.level1_wage_share != null && (
            <span>L1 {fmtPct(lead.level1_wage_share)}</span>
          )}
          {lead.secondary_entity_share != null && (
            <span>3rd-party {fmtPct(lead.secondary_entity_share)}</span>
          )}
          {lead.dependent_anomaly_count != null && (
            <span>dep+{lead.dependent_anomaly_count}</span>
          )}
          {lead.at_pw_floor_share != null && (
            <span>floor {fmtPct(lead.at_pw_floor_share)}</span>
          )}
          {lead.lca_willful_count != null && <span>LCA willful</span>}
          <span className="text-zinc-900 dark:text-zinc-100">
            {lead.risk_score == null ? "—" : fmtScore(lead.risk_score)}
          </span>
        </div>
      </Link>
    </li>
  );
}

function Methodology() {
  return (
    <section className="prose prose-zinc dark:prose-invert max-w-none text-sm">
      <h2>How these leads are built</h2>
      <ul>
        <li>
          <strong>Below prevailing wage</strong> — CERTIFIED H-1B LCAs whose
          annualized offered wage is below the annualized prevailing wage by
          at least $500 (platform constant{" "}
          <code>h1b_below_pw_min_gap_usd</code>). Authority: INA §212(n), 20
          CFR 655.731.
        </li>
        <li>
          <strong>Denial-rate tail</strong> — NJ USCIS petitioners in the top
          1% of first-decision denial rate, among employers with at least 10
          decisions.
        </li>
        <li>
          <strong>LCA vs USCIS volume gap</strong> — employers in both files
          whose certified-LCA-worker / approval ratio is in the top 1%. LCA
          filings precede adjudication, so a gap is expected; only the
          extreme tail is a lead.
        </li>
        <li>
          <strong>Certified-withdrawn tail</strong> — top 1% CERTIFIED-WITHDRAWN
          share (benching / file-then-abandon lead).
        </li>
        <li>
          <strong>WHD willful / debarred</strong> — canonical-name match to an
          active DOL Wage and Hour debarment or willful-violator row (20 CFR
          655.736 / 655.750(d)). Official list, not a new finding.
        </li>
        <li>
          <strong>Level I wage-share tail</strong> — top 1% of CERTIFIED LCA
          share filed at prevailing-wage Level I. Level I is a legal OFLC
          tier; only the peer tail is a lead.
        </li>
        <li>
          <strong>Secondary-entity tail</strong> — top 1% of CERTIFIED LCA
          share attesting third-party placement (ETA-9035 §F.a.2).
        </li>
        <li>
          <strong>H-1B-dependent + anomaly</strong> — attested H-1B-dependent
          (20 CFR 655.736) plus a corroborating wage / volume / placement
          anomaly. Dependency is a statutory bucket, not itself a violation.
        </li>
        <li>
          <strong>At-prevailing-wage floor tail</strong> — top 1% of
          CERTIFIED LCA share filed at exactly the prevailing wage in the
          same unit. Filing at PW is lawful (20 CFR 655.731 is ≥ PW); only
          the peer tail is a lead.
        </li>
        <li>
          <strong>LCA willful attestation</strong> — at least one CERTIFIED
          LCA with ETA-9035 <code>WILLFUL_VIOLATOR=Y</code>. Distinct from
          the WHD official willful-violator list.
        </li>
      </ul>
      <p>
        Sources:{" "}
        <a href="https://www.dol.gov/agencies/eta/foreign-labor/performance">
          DOL OFLC performance data
        </a>
        ;{" "}
        <a href="https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub">
          USCIS H-1B Employer Data Hub
        </a>
        ;{" "}
        <a href="https://www.dol.gov/agencies/whd/immigration/h1b/debarment">
          DOL WHD H-1B debarment
        </a>
        {" / "}
        <a href="https://www.dol.gov/agencies/whd/immigration/h1b/willful-violator-list">
          willful-violator list
        </a>
        . See{" "}
        <Link href="/about">methodology</Link> for the risk-score formula.
      </p>
    </section>
  );
}
