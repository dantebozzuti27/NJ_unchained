import Link from "next/link";

import { isDbReachable } from "@/lib/db";
import { getHighValueLeadsSummary, listHighValueLeads } from "@/lib/queries";
import { fmtUsd } from "@/lib/format";
import type { HighValueLead, HighValueLeadsSummary } from "@/lib/types";

export const dynamic = "force-dynamic";
export const revalidate = 0;

// Formula head for the lead-ranking substrate (migration 112). Surfaced so the
// page carries its own provenance, per the verifiable-data invariant.
const PROGRAM_VERSION = "3.0.0-fraud-high-value-leads-v1";

/* Reward-tier presentation. tier 1 = highest reportability reward potential. */
const TIER_META: Record<
  number,
  { label: string; bg: string; fg: string; ring: string }
> = {
  1: {
    label: "FCA qui tam · reward-eligible",
    bg: "bg-red-100 dark:bg-red-950",
    fg: "text-red-800 dark:text-red-200",
    ring: "ring-red-500",
  },
  2: {
    label: "FCA / Anti-Kickback · reward-eligible",
    bg: "bg-orange-100 dark:bg-orange-950",
    fg: "text-orange-800 dark:text-orange-200",
    ring: "ring-orange-500",
  },
  3: {
    label: "HHS-OIG / CMS referral",
    bg: "bg-amber-100 dark:bg-amber-950",
    fg: "text-amber-800 dark:text-amber-200",
    ring: "ring-amber-500",
  },
  4: {
    label: "Exclusion / debarment flag",
    bg: "bg-blue-50 dark:bg-blue-950",
    fg: "text-blue-700 dark:text-blue-300",
    ring: "ring-blue-400",
  },
  5: {
    label: "FEC structural",
    bg: "bg-zinc-100 dark:bg-zinc-800",
    fg: "text-zinc-600 dark:text-zinc-300",
    ring: "ring-zinc-400",
  },
};

const KIND_LABELS: Record<string, string> = {
  provider: "Healthcare provider",
  candidate: "Candidate",
  committee: "Committee",
  treasurer: "Treasurer",
  donor: "Donor",
  donor_cluster: "Donor cluster",
  contractor: "Contractor",
  address: "Address",
  nj_state_candidate: "NJ state candidate",
};

export default async function LeadsPage() {
  const reachable = await isDbReachable();

  let summary: HighValueLeadsSummary | null = null;
  let leads: HighValueLead[] = [];
  let dbError: string | null = null;

  if (reachable.reachable) {
    try {
      summary = await getHighValueLeadsSummary();
      if (summary.n_total > 0) {
        leads = await listHighValueLeads({ limit: 50 });
      }
    } catch (e) {
      dbError = e instanceof Error ? e.message : String(e);
    }
  }

  return (
    <div className="space-y-8">
      <Hero summary={summary} />

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
          {summary && <SummaryBar summary={summary} />}
          {leads.length > 0 ? (
            <LeadQueue leads={leads} />
          ) : (
            <DormantNotice />
          )}
          {summary && <DormantLanes summary={summary} />}
          <Methodology />
        </>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- */
/*  Hero                                                            */
/* ---------------------------------------------------------------- */

function Hero({ summary }: { summary: HighValueLeadsSummary | null }) {
  const maxExp = summary?.max_exposure_usd ?? 0;
  const nReward = summary?.n_reward_eligible ?? 0;

  return (
    <section className="relative overflow-hidden rounded-2xl border border-rose-200 dark:border-rose-900 bg-gradient-to-br from-rose-50 via-white to-amber-50 dark:from-rose-950/40 dark:via-zinc-900 dark:to-amber-950/30 p-8 sm:p-12">
      <div className="max-w-3xl space-y-6">
        <span className="inline-flex items-center gap-2 rounded-full border border-rose-300 dark:border-rose-800 bg-rose-50 dark:bg-rose-950/40 px-3 py-1 text-xs font-semibold uppercase tracking-wider text-rose-800 dark:text-rose-200">
          <span className="h-1.5 w-1.5 rounded-full bg-rose-500" />
          Highest-value fraud · FRAUD-F8
        </span>

        <h1 className="text-4xl sm:text-5xl font-bold tracking-tight leading-tight">
          {maxExp > 0 ? (
            <>
              The biggest single lead carries{" "}
              <span className="text-rose-600 dark:text-rose-400">
                {fmtUsd(maxExp)}
              </span>{" "}
              in measured exposure.
            </>
          ) : (
            <>Every flagged entity, ranked by what it&rsquo;s worth to pursue.</>
          )}
        </h1>

        <p className="max-w-2xl text-lg text-zinc-600 dark:text-zinc-400">
          One queue across every detector, ranked by{" "}
          <strong>financial scale</strong> and{" "}
          <strong>reportability reward potential</strong> &mdash; the statutory
          whistleblower channel that can actually act on each lead. Reward-bearing
          False&nbsp;Claims&nbsp;Act candidates (excluded providers still billing
          Medicare) rise to the top, biased toward <em>repeat violators</em>{" "}
          whose prior sanction failed to deter and <em>multi-source</em> hits
          corroborated across independent datasets.
          {nReward > 0 && (
            <>
              {" "}
              <span className="font-semibold text-rose-700 dark:text-rose-300">
                {nReward} reward-eligible
              </span>{" "}
              right now.
            </>
          )}
        </p>

        <p className="pt-1 text-xs text-zinc-500 dark:text-zinc-500">
          The ranking is lexicographic over <em>measured dollars</em> and a{" "}
          <em>cited statute&rarr;reward mapping</em> &mdash; never a fabricated
          composite score · formula head{" "}
          <code className="font-mono">{PROGRAM_VERSION}</code>
        </p>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- */
/*  Summary bar                                                     */
/* ---------------------------------------------------------------- */

function SummaryBar({ summary }: { summary: HighValueLeadsSummary }) {
  const cells: { label: string; value: string; tone?: string }[] = [
    {
      label: "Reward-eligible exposure",
      value:
        summary.total_reward_eligible_exposure_usd != null
          ? fmtUsd(summary.total_reward_eligible_exposure_usd)
          : "—",
      tone: "text-rose-700 dark:text-rose-300",
    },
    {
      label: "Largest single lead",
      value:
        summary.max_exposure_usd != null
          ? fmtUsd(summary.max_exposure_usd)
          : "—",
    },
    { label: "Reward-eligible leads", value: summary.n_reward_eligible.toLocaleString() },
    { label: "Repeat violators", value: summary.n_repeat_violators.toLocaleString() },
    { label: "Multi-source hits", value: summary.n_multi_source.toLocaleString() },
    { label: "Total flagged entities", value: summary.n_total.toLocaleString() },
  ];
  return (
    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Queue at a glance</h2>
        <div className="flex flex-wrap gap-2">
          {[1, 2, 3, 4, 5].map((t) => {
            const n = summary.count_by_tier[String(t)] ?? 0;
            const meta = TIER_META[t];
            return (
              <span
                key={t}
                className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${meta.bg} ${meta.fg}`}
                title={meta.label}
              >
                T{t} · {n.toLocaleString()}
              </span>
            );
          })}
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        {cells.map((c) => (
          <div
            key={c.label}
            className="rounded-lg border border-zinc-100 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-3"
          >
            <div
              className={`font-mono text-xl font-bold ${
                c.tone ?? "text-zinc-900 dark:text-zinc-100"
              }`}
            >
              {c.value}
            </div>
            <div className="mt-0.5 text-[11px] leading-tight text-zinc-600 dark:text-zinc-400">
              {c.label}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- */
/*  Lead queue                                                      */
/* ---------------------------------------------------------------- */

function LeadQueue({ leads }: { leads: HighValueLead[] }) {
  return (
    <section>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-tight">
          Priority queue
          <span className="ml-2 text-sm font-normal text-zinc-500">
            (top {leads.length})
          </span>
        </h2>
        <span className="text-xs text-zinc-500">
          Ranked by reward tier → exposure → repeat → multi-source
        </span>
      </div>
      <div className="space-y-2">
        {leads.map((l) => (
          <LeadCard key={`${l.entity_kind}|${l.entity_id}`} lead={l} />
        ))}
      </div>
    </section>
  );
}

function LeadCard({ lead: l }: { lead: HighValueLead }) {
  const meta = TIER_META[l.best_reward_tier] ?? TIER_META[5];
  const kindLabel = KIND_LABELS[l.entity_kind] ?? l.entity_kind;
  return (
    <div
      className={`block rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 ring-1 ring-transparent hover:${meta.ring}/30 transition-colors`}
    >
      <div className="flex flex-wrap items-start gap-3">
        {/* Rank badge */}
        <div className="flex flex-col items-center justify-center">
          <span className="font-mono text-lg font-bold text-zinc-400 dark:text-zinc-600">
            #{l.lead_rank}
          </span>
        </div>

        {/* Identity + badges */}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
              {kindLabel} · {l.latest_cycle}
            </span>
            {l.is_nj && (
              <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold bg-emerald-100 text-emerald-900 dark:bg-emerald-900 dark:text-emerald-100">
                NJ
              </span>
            )}
            <span
              className={`inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold ${meta.bg} ${meta.fg}`}
            >
              T{l.best_reward_tier} · {meta.label}
            </span>
            {l.repeat_violator && (
              <span
                className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200"
                title="A prior-sanction signal recurred across ≥2 cycles — the penalty failed to deter."
              >
                REPEAT
              </span>
            )}
            {l.multi_source && (
              <span
                className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold bg-violet-100 text-violet-900 dark:bg-violet-950 dark:text-violet-200"
                title="Flagged by ≥2 independent signal families."
              >
                MULTI-SOURCE
              </span>
            )}
          </div>
          <Link
            href={`/risk/${encodeURIComponent(
              l.entity_kind,
            )}/${encodeURIComponent(l.entity_id)}?cycle=${encodeURIComponent(
              l.latest_cycle,
            )}`}
            className="mt-1 block truncate text-base font-semibold hover:underline underline-offset-4"
          >
            {l.display_name ?? l.entity_id}
          </Link>
          <div className="font-mono text-xs text-zinc-500">
            {l.entity_kind === "provider" ? "NPI " : ""}
            {l.entity_id}
            {l.n_cycles > 1 && (
              <span className="ml-2 text-zinc-400">
                · seen in {l.n_cycles} cycles
              </span>
            )}
          </div>
        </div>

        {/* Exposure + reward */}
        <div className="flex flex-col items-end text-right">
          {l.peak_exposure_usd != null && l.peak_exposure_usd > 0 ? (
            <>
              <div
                className="font-mono text-2xl font-bold leading-none"
                title="Peak single-cycle measured dollar exposure"
              >
                {fmtUsd(l.peak_exposure_usd)}
              </div>
              <div className="mt-0.5 text-[10px] uppercase tracking-wider text-zinc-500">
                peak exposure
              </div>
            </>
          ) : (
            <div className="text-[11px] text-zinc-400">no $ exposure</div>
          )}
          {l.reward_eligible &&
            l.reward_low_usd != null &&
            l.reward_high_usd != null && (
              <div
                className="mt-1.5 rounded-md bg-rose-50 dark:bg-rose-950/40 px-2 py-1 text-[11px] font-semibold text-rose-700 dark:text-rose-300"
                title="Statutory relator share (15–30%) applied to peak exposure as a conservative single-damages proxy; FCA damages can treble."
              >
                est. reward {fmtUsd(l.reward_low_usd)}–{fmtUsd(l.reward_high_usd)}
              </div>
            )}
        </div>
      </div>

      {/* Driver + recovery channel */}
      <div className="mt-3 rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-3">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-zinc-500">
          <span className="font-mono font-semibold text-zinc-700 dark:text-zinc-300">
            {l.driver_signal_id}
          </span>
          <span aria-hidden>·</span>
          <span>{l.n_signals} signal{l.n_signals === 1 ? "" : "s"}</span>
          <span aria-hidden>·</span>
          <span className="font-medium text-zinc-700 dark:text-zinc-300">
            {l.recovery_program}
          </span>
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
          <span className="text-zinc-600 dark:text-zinc-400">
            Report: {l.recovery_channel}
          </span>
          {l.recovery_channel_url && (
            <a
              href={l.recovery_channel_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-mono text-rose-700 underline-offset-2 hover:underline dark:text-rose-300"
            >
              channel ↗
            </a>
          )}
          {l.statute_url && (
            <a
              href={l.statute_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-mono text-zinc-500 underline-offset-2 hover:underline"
              title={l.statute_citation}
            >
              {l.statute_citation} ↗
            </a>
          )}
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- */
/*  Dormant queue notice                                           */
/* ---------------------------------------------------------------- */

function DormantNotice() {
  return (
    <section className="rounded-xl border border-dashed border-rose-300 dark:border-rose-800 bg-rose-50/50 dark:bg-rose-950/20 p-6">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 inline-flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-rose-100 dark:bg-rose-900 text-rose-700 dark:text-rose-200 text-sm font-bold">
          ✓
        </span>
        <div>
          <h2 className="text-base font-semibold tracking-tight">
            Ranking engine deployed — awaiting flagged entities
          </h2>
          <p className="mt-1 max-w-3xl text-sm text-zinc-600 dark:text-zinc-400">
            The lead-ranking substrate and the cited statute&rarr;reward mapping
            are live, but no entity has fired a signal yet. The moment the engine
            emits observations, this queue populates automatically — ranked by
            value, no code change. Substrate honesty: an empty queue, never a
            fabricated one.
          </p>
        </div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- */
/*  Dormant lanes (honest "cannot yet rank")                        */
/* ---------------------------------------------------------------- */

function DormantLanes({ summary }: { summary: HighValueLeadsSummary }) {
  const fecLoaded = summary.n_fec_contribution > 0;
  return (
    <section className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
      <h2 className="text-sm font-semibold tracking-tight">
        Lanes this queue cannot rank yet
      </h2>
      <p className="mt-1 max-w-3xl text-xs text-zinc-500">
        The brief asked to prioritize IRS whistleblower candidates (501c4/527
        dark money). Those lanes need substrate the platform does not hold, so
        they are shown here as gaps rather than ranked with fabricated numbers.
      </p>
      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="rounded-lg border border-dashed border-zinc-300 dark:border-zinc-700 p-3">
          <div className="flex items-center gap-2">
            <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
              DORMANT
            </span>
            <h3 className="text-sm font-semibold">
              IRS whistleblower — 501c4 / 527 unexplained flows
            </h3>
          </div>
          <p className="mt-1.5 text-xs text-zinc-600 dark:text-zinc-400">
            Pure 501c4 dark money never files with the FEC; it surfaces only in
            IRS Form&nbsp;990 / 8872 filings, which are not ingested. Without that
            substrate there is no honest way to measure an &ldquo;unexplained
            flow,&rdquo; so no signal maps to the IRS reward lane
            (26&nbsp;U.S.C.&nbsp;§&nbsp;7623).
          </p>
        </div>
        <div className="rounded-lg border border-dashed border-zinc-300 dark:border-zinc-700 p-3">
          <div className="flex items-center gap-2">
            <span
              className={`inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold ${
                fecLoaded
                  ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900 dark:text-emerald-100"
                  : "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"
              }`}
            >
              {fecLoaded ? "LOADED" : "DORMANT"}
            </span>
            <h3 className="text-sm font-semibold">
              FEC itemized contribution flows
            </h3>
          </div>
          <p className="mt-1.5 text-xs text-zinc-600 dark:text-zinc-400">
            Every FEC signal today is <em>structural</em> (count-based), not
            dollar-denominated.{" "}
            {fecLoaded ? (
              <>
                {summary.n_fec_contribution.toLocaleString()} itemized
                contribution rows are loaded — a &ldquo;large inflow into an
                IE-only / Super-PAC committee&rdquo; signal can be built on top.
              </>
            ) : (
              <>
                The <code className="font-mono">raw.fec_contribution</code> table
                is empty, so super-PAC inflow scale cannot yet be ranked. Loading
                NJ-filtered itemized contributions would light this lane up.
              </>
            )}
          </p>
        </div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- */
/*  Methodology                                                     */
/* ---------------------------------------------------------------- */

function Methodology() {
  return (
    <details className="rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 p-4 text-xs text-zinc-600 dark:text-zinc-400">
      <summary className="cursor-pointer font-semibold text-zinc-800 dark:text-zinc-200">
        How the ranking works
      </summary>
      <div className="mt-3 space-y-2 leading-relaxed">
        <p>
          Each entity is ranked <em>lexicographically</em>, not by a weighted
          blend: first by <strong>reportability reward tier</strong> (does a
          statutory whistleblower bounty attach — False Claims Act qui tam ranks
          highest), then by <strong>peak measured USD exposure</strong>, then by
          whether a prior-sanction signal <strong>recurred across cycles</strong>{" "}
          (the penalty-failed-to-deter case), then by{" "}
          <strong>multi-source breadth</strong>. Every ordering key is either a
          dollar figure measured on the observation or a value from the
          versioned, cited{" "}
          <code className="font-mono">ref.fraud_reportability_channel</code>{" "}
          mapping. No magic composite score exists.
        </p>
        <p>
          The <strong>estimated reward</strong> applies the statutory relator
          share (15–30%, 31&nbsp;U.S.C.&nbsp;§&nbsp;3730(d)) to the peak
          single-cycle exposure as a conservative single-damages proxy. Actual
          FCA damages can be trebled, so this band is a floor, not a prediction.
        </p>
        <p>
          A lead is a <em>structural anomaly routed for review</em>, not a
          finding of fraud. Each card links to the enforcement channel and the
          governing statute so an analyst can verify before acting.
        </p>
      </div>
    </details>
  );
}

/* ---------------------------------------------------------------- */
/*  Notices                                                         */
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
