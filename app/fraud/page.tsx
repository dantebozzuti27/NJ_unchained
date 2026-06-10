import Link from "next/link";

import { isDbReachable } from "@/lib/db";
import {
  getHealthcareSignalCatalog,
  getHealthcareSubstrateStatus,
  listTopProviderRisk,
} from "@/lib/queries";
import { fmtScore, fmtUsd, riskTier } from "@/lib/format";
import type {
  HealthcareSignalCatalogEntry,
  HealthcareSubstrateStatus,
  ProviderRiskCard,
} from "@/lib/types";

export const dynamic = "force-dynamic";
export const revalidate = 0;

// FRAUD-F7 program formula head (the latest signal's version). Surfaced so
// the page carries its own provenance, per the verifiable-data invariant.
const PROGRAM_VERSION = "2.9.1-fraud-excluded-provider-received-open-payments-v1";

/* Human labels for the three provider-domain signal families. */
const FAMILY_LABELS: Record<string, { label: string; blurb: string }> = {
  leie_bearing: {
    label: "Exclusion × billing",
    blurb:
      "An OIG-excluded or name-resolved provider who is nonetheless present in federal billing or industry-payment data.",
  },
  state_exclusion: {
    label: "State exclusion × billing",
    blurb:
      "A provider debarred by a state Medicaid program who still appears in federal Medicare billing.",
  },
  cms_utilization: {
    label: "Utilization outlier",
    blurb:
      "A provider whose prescribing or service volume sits in the extreme tail of their own medical specialty.",
  },
};

/* Friendly titles for each of the seven signals. */
const SIGNAL_TITLES: Record<string, string> = {
  provider_excluded_billing: "Excluded provider billing Medicare (Part D)",
  provider_excluded_billing_partb: "Excluded provider billing Medicare (Part B)",
  state_excluded_provider_billing: "State-excluded provider billing Medicare",
  opioid_prescribing_outlier: "Opioid-prescribing outlier",
  services_per_beneficiary_outlier: "Services-per-beneficiary outlier",
  name_resolved_excluded_provider_billing:
    "Name-resolved excluded provider billing",
  excluded_provider_received_open_payments:
    "Excluded provider receiving industry payments",
};

const BASIS_LABELS: Record<string, string> = {
  oig_report: "OIG exclusion authority",
  inferred_identity: "Name + state inferred identity",
  empirical_pctile: "Empirical peer distribution",
  state_exclusion: "State debarment authority",
};

export default async function FraudPage() {
  const reachable = await isDbReachable();

  let status: HealthcareSubstrateStatus | null = null;
  let catalog: HealthcareSignalCatalogEntry[] = [];
  let providers: ProviderRiskCard[] = [];
  let dbError: string | null = null;

  if (reachable.reachable) {
    try {
      [status, catalog] = await Promise.all([
        getHealthcareSubstrateStatus(),
        getHealthcareSignalCatalog(),
      ]);
      if (status.n_provider_observations > 0) {
        providers = await listTopProviderRisk({ limit: 25 });
      }
    } catch (e) {
      dbError = e instanceof Error ? e.message : String(e);
    }
  }

  return (
    <div className="space-y-8">
      <Hero status={status} catalog={catalog} />

      {!reachable.reachable ? (
        <Notice
          tone="amber"
          title="Database not reachable."
          body="Configure NEON_DATABASE_URL in the Vercel project to populate this view."
        />
      ) : dbError ? (
        <Notice tone="red" title="Query failed." body={dbError} mono />
      ) : (
        <>
          {status && <SubstrateStatusBar status={status} />}
          {providers.length > 0 ? (
            <ProviderQueue providers={providers} />
          ) : (
            <DormantQueueNotice status={status} />
          )}
          <SignalCatalog catalog={catalog} />
          <Methodology />
        </>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- */
/*  Hero                                                            */
/* ---------------------------------------------------------------- */

function Hero({
  status,
  catalog,
}: {
  status: HealthcareSubstrateStatus | null;
  catalog: HealthcareSignalCatalogEntry[];
}) {
  const flagged = status?.n_provider_observations ?? 0;
  const nSignals = catalog.length;

  return (
    <section className="relative overflow-hidden rounded-2xl border border-teal-200 dark:border-teal-900 bg-gradient-to-br from-teal-50 via-white to-cyan-50 dark:from-teal-950/40 dark:via-zinc-900 dark:to-cyan-950/30 p-8 sm:p-12">
      <div className="max-w-3xl space-y-6">
        <span className="inline-flex items-center gap-2 rounded-full border border-teal-300 dark:border-teal-800 bg-teal-50 dark:bg-teal-950/40 px-3 py-1 text-xs font-semibold uppercase tracking-wider text-teal-800 dark:text-teal-200">
          <span className="h-1.5 w-1.5 rounded-full bg-teal-500" />
          Healthcare provider fraud · FRAUD-F7
        </span>

        <h1 className="text-4xl sm:text-5xl font-bold tracking-tight leading-tight">
          {flagged > 0 ? (
            <>
              <span className="text-teal-600 dark:text-teal-400">
                {flagged.toLocaleString()}
              </span>{" "}
              provider–signal overlaps flagged across{" "}
              <span className="text-teal-600 dark:text-teal-400">
                {nSignals}
              </span>{" "}
              detectors.
            </>
          ) : (
            <>
              Excluded doctors who keep billing. Outliers in their own
              specialty&rsquo;s tail.
            </>
          )}
        </h1>

        <p className="max-w-2xl text-lg text-zinc-600 dark:text-zinc-400">
          {nSignals} NPI-keyed detectors cross-reference the HHS-OIG exclusion
          list and NJ Medicaid debarments against Medicare Part&nbsp;D, Part&nbsp;B,
          and Open&nbsp;Payments &mdash; then flag prescribing and service-volume
          outliers within each medical specialty. Built entirely on free,
          keyless public data (CMS, NPPES, HHS-OIG). Every flag links to the
          federal authority it codifies and to the upstream record to verify.
        </p>

        <div className="flex flex-wrap gap-3 pt-2">
          <a
            href="#signals"
            className="inline-flex items-center gap-2 rounded-lg bg-teal-600 hover:bg-teal-500 px-5 py-2.5 text-sm font-semibold text-white shadow-sm shadow-teal-900/20"
          >
            The {nSignals} detectors
            <span aria-hidden>&darr;</span>
          </a>
          <Link
            href="/risk"
            className="inline-flex items-center gap-2 rounded-lg border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-5 py-2.5 text-sm font-semibold hover:bg-zinc-100 dark:hover:bg-zinc-800"
          >
            Civic-integrity risk
          </Link>
        </div>

        <p className="pt-3 text-xs text-zinc-500 dark:text-zinc-500">
          Research-tier: a flag routes analyst attention, it is not a finding of
          fraud · formula head{" "}
          <code className="font-mono">{PROGRAM_VERSION}</code>
        </p>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- */
/*  Substrate status bar                                            */
/* ---------------------------------------------------------------- */

function SubstrateStatusBar({ status }: { status: HealthcareSubstrateStatus }) {
  const cells: { label: string; n: number; source: string }[] = [
    { label: "OIG LEIE exclusions", n: status.n_leie, source: "HHS-OIG" },
    { label: "Part D prescribers", n: status.n_partd_prescriber, source: "CMS" },
    { label: "Part B practitioners", n: status.n_physician_provider, source: "CMS" },
    { label: "Open Payments", n: status.n_open_payments, source: "CMS" },
    { label: "NJ Medicaid debarments", n: status.n_nj_medicaid_exclusion, source: "NJ-OSC" },
    { label: "NPPES identities", n: status.n_nppes_provider, source: "CMS" },
  ];
  return (
    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Loaded substrate</h2>
        <span className="text-xs text-zinc-500">
          Row counts in the production warehouse · keyless public sources
        </span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        {cells.map((c) => (
          <div
            key={c.label}
            className="rounded-lg border border-zinc-100 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-3"
          >
            <div
              className={`font-mono text-xl font-bold ${
                c.n > 0
                  ? "text-zinc-900 dark:text-zinc-100"
                  : "text-zinc-400 dark:text-zinc-600"
              }`}
            >
              {c.n.toLocaleString()}
            </div>
            <div className="mt-0.5 text-[11px] leading-tight text-zinc-600 dark:text-zinc-400">
              {c.label}
            </div>
            <div className="mt-1 text-[10px] uppercase tracking-wider text-zinc-400 dark:text-zinc-600">
              {c.source}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- */
/*  Provider queue (populated) / dormant notice (empty)            */
/* ---------------------------------------------------------------- */

function ProviderQueue({ providers }: { providers: ProviderRiskCard[] }) {
  return (
    <section>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-tight">
          Flagged providers
          <span className="ml-2 text-sm font-normal text-zinc-500">
            ({providers.length})
          </span>
        </h2>
        <span className="text-xs text-zinc-500">
          One row per NPI · ordered by composite anomaly score
        </span>
      </div>
      <div className="space-y-2">
        {providers.map((p) => (
          <ProviderCard key={`${p.cycle}|${p.entity_id}`} provider={p} />
        ))}
      </div>
    </section>
  );
}

function ProviderCard({ provider: p }: { provider: ProviderRiskCard }) {
  const tier = riskTier(p.risk_score);
  return (
    <Link
      href={`/risk/provider/${encodeURIComponent(
        p.entity_id,
      )}?cycle=${encodeURIComponent(p.cycle)}`}
      className="block rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 hover:bg-zinc-50 dark:hover:bg-zinc-800/50 transition-colors"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
              Healthcare provider · {p.cycle}
            </span>
            {p.is_nj && (
              <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold bg-emerald-100 text-emerald-900 dark:bg-emerald-900 dark:text-emerald-100">
                NJ
              </span>
            )}
            <span
              className={`inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold ${tier.bg} ${tier.fg}`}
            >
              {tier.label}
            </span>
          </div>
          <h3 className="mt-1 truncate text-base font-semibold">
            {p.display_name ?? p.entity_id}
          </h3>
          <div className="font-mono text-xs text-zinc-500">NPI {p.entity_id}</div>
        </div>
        <div className="flex flex-col items-end">
          <div className="font-mono text-2xl font-bold leading-none">
            {fmtScore(p.risk_score)}
          </div>
          <div className="mt-0.5 text-[10px] uppercase tracking-wider text-zinc-500">
            {p.n_signals} signal{p.n_signals === 1 ? "" : "s"}
          </div>
        </div>
      </div>

      <div className="mt-3 rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-3">
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
          <span className="font-mono font-semibold text-zinc-700 dark:text-zinc-300">
            {p.preview_signal_id}
          </span>
          <span aria-hidden>·</span>
          <SeverityDots level={p.preview_severity} />
          {p.preview_raw_value != null && p.preview_raw_value > 0 && (
            <>
              <span aria-hidden>·</span>
              <span title="Dollar exposure carried by this signal">
                exposure{" "}
                <span className="font-mono text-zinc-700 dark:text-zinc-300">
                  {fmtUsd(p.preview_raw_value)}
                </span>
              </span>
            </>
          )}
          {p.preview_citation_authority && (
            <>
              <span aria-hidden>·</span>
              <span className="font-mono">{p.preview_citation_authority}</span>
            </>
          )}
        </div>
        <p className="mt-1.5 line-clamp-2 text-sm text-zinc-700 dark:text-zinc-300">
          {p.preview_explanation}
        </p>
      </div>
    </Link>
  );
}

function DormantQueueNotice({
  status,
}: {
  status: HealthcareSubstrateStatus | null;
}) {
  const hasLeie = (status?.n_leie ?? 0) > 0;
  return (
    <section className="rounded-xl border border-dashed border-teal-300 dark:border-teal-800 bg-teal-50/50 dark:bg-teal-950/20 p-6">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 inline-flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-teal-100 dark:bg-teal-900 text-teal-700 dark:text-teal-200 text-sm font-bold">
          ✓
        </span>
        <div>
          <h2 className="text-base font-semibold tracking-tight">
            Engine deployed — awaiting provider data load
          </h2>
          <p className="mt-1 max-w-3xl text-sm text-zinc-600 dark:text-zinc-400">
            All {7} detectors are live in production and the reference
            substrate{hasLeie ? " (including the OIG exclusion list) " : " "}
            is loaded. No provider observations have been materialized yet:
            the CMS Medicare / NPPES billing data (NJ-filtered, free-tier
            safe) has not been ingested. The moment it lands, this queue
            populates automatically — no code change. This is substrate
            honesty: the platform shows a hole, never a fabricated result.
          </p>
          <p className="mt-2 text-xs text-zinc-500">
            Until then, every detector below is fully specified with its
            severity, federal authority, and verification path.
          </p>
        </div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- */
/*  Signal catalog                                                 */
/* ---------------------------------------------------------------- */

function SignalCatalog({
  catalog,
}: {
  catalog: HealthcareSignalCatalogEntry[];
}) {
  // Group by family, families ordered by their worst signal severity.
  const byFamily = new Map<string, HealthcareSignalCatalogEntry[]>();
  for (const s of catalog) {
    const arr = byFamily.get(s.signal_family) ?? [];
    arr.push(s);
    byFamily.set(s.signal_family, arr);
  }
  const families = [...byFamily.entries()].sort((a, b) => {
    const aMax = Math.max(...a[1].map((s) => s.severity_level));
    const bMax = Math.max(...b[1].map((s) => s.severity_level));
    return bMax - aMax;
  });

  return (
    <section id="signals" className="scroll-mt-6">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-tight">
          The detectors
          <span className="ml-2 text-sm font-normal text-zinc-500">
            ({catalog.length})
          </span>
        </h2>
        <span className="text-xs text-zinc-500">
          Each codifies a specific federal or state predicate
        </span>
      </div>

      <div className="space-y-6">
        {families.map(([family, signals]) => {
          const meta = FAMILY_LABELS[family] ?? {
            label: family,
            blurb: "",
          };
          return (
            <div key={family}>
              <div className="mb-2">
                <h3 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200">
                  {meta.label}
                  <span className="ml-2 font-mono text-[11px] font-normal text-zinc-400">
                    {family}
                  </span>
                </h3>
                {meta.blurb && (
                  <p className="mt-0.5 max-w-3xl text-xs text-zinc-500">
                    {meta.blurb}
                  </p>
                )}
              </div>
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                {signals.map((s) => (
                  <SignalCard key={s.signal_id} signal={s} />
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function SignalCard({ signal: s }: { signal: HealthcareSignalCatalogEntry }) {
  const title = SIGNAL_TITLES[s.signal_id] ?? s.signal_id;
  const basis = s.calibration_basis
    ? BASIS_LABELS[s.calibration_basis] ?? s.calibration_basis
    : null;
  return (
    <article className="flex h-full flex-col rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h4 className="text-sm font-semibold leading-tight">{title}</h4>
          <div className="mt-0.5 font-mono text-[10px] text-zinc-400">
            {s.signal_id}
          </div>
        </div>
        <SeverityDots level={s.severity_level} />
      </div>

      {s.rule_text && (
        <p className="mt-2 line-clamp-4 text-xs leading-relaxed text-zinc-600 dark:text-zinc-400">
          {s.rule_text}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-zinc-500">
        {s.citation_authority && (
          <span className="inline-flex rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 font-mono font-semibold text-zinc-700 dark:text-zinc-300">
            {s.citation_authority}
            {s.citation_section ? ` · ${s.citation_section}` : ""}
          </span>
        )}
        {basis && (
          <span title={`Severity calibration basis: ${s.calibration_basis}`}>
            {basis}
          </span>
        )}
      </div>

      {s.precedent_summary && (
        <details className="mt-2 text-[11px]">
          <summary className="cursor-pointer font-medium text-zinc-600 dark:text-zinc-400">
            Why severity {s.severity_level}/5?
          </summary>
          <p className="mt-1.5 leading-relaxed text-zinc-500">
            {s.precedent_summary}
          </p>
        </details>
      )}

      <div className="mt-auto flex flex-wrap items-center gap-2 pt-3 text-[11px]">
        {s.citation_url && (
          <a
            href={s.citation_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 font-mono text-teal-700 underline-offset-2 hover:underline dark:text-teal-300"
          >
            Read the rule ↗
          </a>
        )}
        {s.upstream_source && (
          <span className="font-mono text-zinc-400">
            verify on {s.upstream_source}
          </span>
        )}
      </div>
    </article>
  );
}

/* ---------------------------------------------------------------- */
/*  Shared bits                                                    */
/* ---------------------------------------------------------------- */

function SeverityDots({ level }: { level: number }) {
  const labels = ["", "Low", "Low-mid", "Mid", "High", "Critical"];
  const lv = Math.max(0, Math.min(5, level));
  return (
    <span
      className="inline-flex flex-shrink-0 items-center gap-1 font-mono text-xs"
      title={`Severity ${lv}/5 (${labels[lv] ?? "?"})`}
    >
      <span className="text-zinc-700 dark:text-zinc-300">{"●".repeat(lv)}</span>
      <span className="text-zinc-300 dark:text-zinc-700">
        {"○".repeat(5 - lv)}
      </span>
    </span>
  );
}

function Methodology() {
  return (
    <details className="rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-4 text-xs text-zinc-600 dark:text-zinc-400">
      <summary className="cursor-pointer font-semibold text-zinc-800 dark:text-zinc-200">
        Methodology &amp; substrate-honesty notes
      </summary>
      <div className="mt-3 space-y-2 leading-relaxed">
        <p>
          Exclusion detectors are <em>exact-NPI</em> joins between the HHS-OIG
          LEIE (or NJ Medicaid debarment list) and CMS billing, gated to the
          window in which the exclusion was in effect. Identity is
          high-confidence; severity 5 routes every overlap to review. The
          name-resolved variant resolves blank-NPI exclusions through NPPES on
          a strict unique name+state key (it never guesses among collisions)
          and is honestly downgraded to severity 3 with an{" "}
          <span className="font-mono">inferred_identity</span> basis.
        </p>
        <p>
          Utilization detectors are specialty-relative: a provider is flagged
          only when their opioid rate or services-per-beneficiary lands in the
          extreme tail of their <em>own</em> specialty&rsquo;s distribution,
          above a volume floor, in a sufficiently large peer bucket. The
          thresholds are versioned reference data, not inline constants.
        </p>
        <p>
          A flag is a <em>structural anomaly</em>, not a confirmed violation.
          Every card links to the federal authority it codifies and to the
          upstream record so an analyst can verify before any finding.
        </p>
      </div>
    </details>
  );
}

/* ---------------------------------------------------------------- */
/*  Notices                                                        */
/* ---------------------------------------------------------------- */

function Notice({
  tone,
  title,
  body,
  mono,
}: {
  tone: "amber" | "red";
  title: string;
  body: string;
  mono?: boolean;
}) {
  const toneCls =
    tone === "amber"
      ? "bg-amber-50 dark:bg-amber-950 text-amber-800 dark:text-amber-200"
      : "bg-red-50 dark:bg-red-950 text-red-800 dark:text-red-200";
  return (
    <div className={`rounded-md p-4 text-sm ${toneCls}`}>
      <div className="font-medium">{title}</div>
      {mono ? (
        <pre className="mt-2 overflow-x-auto rounded bg-black/5 dark:bg-white/5 p-2 text-xs">
          {body}
        </pre>
      ) : (
        <p className="mt-1">{body}</p>
      )}
    </div>
  );
}
