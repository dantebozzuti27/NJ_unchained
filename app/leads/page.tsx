import Link from "next/link";

import { isDbReachable } from "@/lib/db";
import {
  getHighValueLeadsSummary,
  getSignalValidation,
  listHighValueLeads,
} from "@/lib/queries";
import { fmtUsd } from "@/lib/format";
import type {
  HighValueLead,
  HighValueLeadsSummary,
  SignalValidationRow,
} from "@/lib/types";

export const dynamic = "force-dynamic";
export const revalidate = 0;

// Formula head for the lead-ranking substrate (migration 113, undetected-first
// reframe). Surfaced so the page carries its own provenance, per the
// verifiable-data invariant.
const PROGRAM_VERSION = "3.1.0-fraud-leads-undetected-first-v1";

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
  let undetected: HighValueLead[] = [];
  let alreadyCaught: HighValueLead[] = [];
  let validation: SignalValidationRow[] = [];
  let dbError: string | null = null;

  if (reachable.reachable) {
    try {
      summary = await getHighValueLeadsSummary();
      if (summary.n_total > 0) {
        [undetected, alreadyCaught, validation] = await Promise.all([
          listHighValueLeads({ limit: 40, priorEnforcement: false }),
          listHighValueLeads({ limit: 12, priorEnforcement: true }),
          getSignalValidation(),
        ]);
      }
    } catch (e) {
      dbError = e instanceof Error ? e.message : String(e);
    }
  }

  const hasLeads = undetected.length > 0 || alreadyCaught.length > 0;

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
          {hasLeads ? (
            <>
              <UndetectedQueue leads={undetected} />
              {alreadyCaught.length > 0 && (
                <AlreadyCaughtLane leads={alreadyCaught} summary={summary} />
              )}
            </>
          ) : (
            <DormantNotice />
          )}
          {validation.length > 0 && <SignalValidation rows={validation} />}
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
  const maxScale = summary?.max_undetected_scale_usd ?? 0;
  const nUndetected = summary?.n_undetected ?? 0;

  return (
    <section className="relative overflow-hidden rounded-2xl border border-rose-200 dark:border-rose-900 bg-gradient-to-br from-rose-50 via-white to-amber-50 dark:from-rose-950/40 dark:via-zinc-900 dark:to-amber-950/30 p-8 sm:p-12">
      <div className="max-w-3xl space-y-6">
        <span className="inline-flex items-center gap-2 rounded-full border border-rose-300 dark:border-rose-800 bg-rose-50 dark:bg-rose-950/40 px-3 py-1 text-xs font-semibold uppercase tracking-wider text-rose-800 dark:text-rose-200">
          <span className="h-1.5 w-1.5 rounded-full bg-rose-500" />
          Undetected fraud · FRAUD-F8
        </span>

        <h1 className="text-4xl sm:text-5xl font-bold tracking-tight leading-tight">
          {maxScale > 0 ? (
            <>
              The biggest <span className="text-rose-600 dark:text-rose-400">not-yet-flagged</span>{" "}
              provider bills{" "}
              <span className="text-rose-600 dark:text-rose-400">
                {fmtUsd(maxScale)}
              </span>{" "}
              of Medicare a year.
            </>
          ) : (
            <>Find the fraud no one has caught yet.</>
          )}
        </h1>

        <p className="max-w-2xl text-lg text-zinc-600 dark:text-zinc-400">
          This queue deliberately demotes anyone already on an exclusion or
          debarment list &mdash; those cases are <em>already caught</em>, and
          under the False&nbsp;Claims&nbsp;Act public-disclosure bar
          (31&nbsp;U.S.C.&nbsp;§&nbsp;3730(e)(4)) a provider derivable from public
          lists is weak whistleblower material. Instead it leads with{" "}
          <strong>undetected providers</strong>: those with no enforcement action
          but a <strong>behavioral billing pattern</strong> that fired a detector,
          ranked by their real <strong>Medicare dollar scale</strong> and how many
          independent patterns corroborate it.
          {nUndetected > 0 && (
            <>
              {" "}
              <span className="font-semibold text-rose-700 dark:text-rose-300">
                {nUndetected.toLocaleString()} undetected leads
              </span>{" "}
              right now.
            </>
          )}
        </p>

        <p className="pt-1 text-xs text-zinc-500 dark:text-zinc-500">
          Ranking is lexicographic over <em>measured dollars and counts</em>{" "}
          (enforcement status &rarr; Medicare scale &rarr; corroboration) &mdash;
          never a fabricated composite score · formula head{" "}
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
      label: "Undetected leads",
      value: summary.n_undetected.toLocaleString(),
      tone: "text-rose-700 dark:text-rose-300",
    },
    {
      label: "Largest undetected scale",
      value:
        summary.max_undetected_scale_usd != null
          ? fmtUsd(summary.max_undetected_scale_usd)
          : "—",
    },
    { label: "Multi-source hits", value: summary.n_multi_source.toLocaleString() },
    {
      label: "Already on enforcement radar",
      value: summary.n_already_caught.toLocaleString(),
      tone: "text-zinc-500 dark:text-zinc-400",
    },
    { label: "Repeat violators (caught)", value: summary.n_repeat_violators.toLocaleString() },
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

function UndetectedQueue({ leads }: { leads: HighValueLead[] }) {
  return (
    <section>
      <div className="mb-1 flex items-baseline justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-tight">
          Undetected leads
          <span className="ml-2 text-sm font-normal text-zinc-500">
            (top {leads.length})
          </span>
        </h2>
        <span className="text-xs text-zinc-500">
          Ranked by Medicare scale → corroboration → severity
        </span>
      </div>
      <p className="mb-3 max-w-3xl text-xs text-zinc-500">
        Providers with a behavioral billing pattern but{" "}
        <strong>no exclusion, debarment, or sanction on record</strong> &mdash;
        the cases that haven&rsquo;t happened yet. A statistical outlier is a lead
        for review, not a finding of fraud.
      </p>
      <div className="space-y-2">
        {leads.map((l) => (
          <LeadCard key={`${l.entity_kind}|${l.entity_id}`} lead={l} />
        ))}
      </div>
    </section>
  );
}

function AlreadyCaughtLane({
  leads,
  summary,
}: {
  leads: HighValueLead[];
  summary: HighValueLeadsSummary | null;
}) {
  const total = summary?.n_already_caught ?? leads.length;
  return (
    <section className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-zinc-50/60 dark:bg-zinc-950/40 p-4">
      <div className="mb-1 flex items-baseline justify-between gap-3">
        <h2 className="text-base font-semibold tracking-tight text-zinc-600 dark:text-zinc-400">
          Already on the enforcement radar
          <span className="ml-2 text-sm font-normal text-zinc-500">
            ({total.toLocaleString()} total · showing {leads.length})
          </span>
        </h2>
      </div>
      <p className="mb-3 max-w-3xl text-xs text-zinc-500">
        These entities are already on an exclusion or debarment list (HHS-OIG
        LEIE / NJ-Medicaid / SAM). The enforcement system has acted &mdash; and
        the FCA public-disclosure bar weakens any whistleblower claim built on
        public lists. Kept for completeness, demoted on purpose.
      </p>
      <div className="space-y-2 opacity-80">
        {leads.map((l) => (
          <LeadCard key={`${l.entity_kind}|${l.entity_id}`} lead={l} dimmed />
        ))}
      </div>
    </section>
  );
}

function LeadCard({ lead: l, dimmed }: { lead: HighValueLead; dimmed?: boolean }) {
  const meta = TIER_META[l.best_reward_tier] ?? TIER_META[5];
  const kindLabel = KIND_LABELS[l.entity_kind] ?? l.entity_kind;
  // Financial-scale figure: measured exposure if the signal is dollar-denominated,
  // else the provider's real Medicare volume (the undetected-lead yardstick).
  const scale =
    l.peak_exposure_usd != null && l.peak_exposure_usd > 0
      ? { usd: l.peak_exposure_usd, label: "peak exposure" }
      : l.provider_scale_usd != null && l.provider_scale_usd > 0
        ? { usd: l.provider_scale_usd, label: "Medicare billed / yr" }
        : null;
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
            {dimmed ? (
              <span
                className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
                title="Already on an exclusion/debarment list — enforcement has acted."
              >
                ALREADY CAUGHT
              </span>
            ) : (
              <span
                className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold bg-rose-100 text-rose-900 dark:bg-rose-950 dark:text-rose-200"
                title="No exclusion, debarment, or sanction on record — a prospective lead."
              >
                UNDETECTED
              </span>
            )}
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

        {/* Financial scale + reward */}
        <div className="flex flex-col items-end text-right">
          {scale ? (
            <>
              <div
                className="font-mono text-2xl font-bold leading-none"
                title={
                  scale.label === "peak exposure"
                    ? "Peak single-cycle measured dollar exposure"
                    : "Provider's peak single-year Medicare volume (Part B payment + Part D drug cost)"
                }
              >
                {fmtUsd(scale.usd)}
              </div>
              <div className="mt-0.5 text-[10px] uppercase tracking-wider text-zinc-500">
                {scale.label}
              </div>
            </>
          ) : (
            <div className="text-[11px] text-zinc-400">no $ scale</div>
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
/*  Signal validation harness (honest precision/lift)              */
/* ---------------------------------------------------------------- */

const VALIDATION_VERSION = "3.5.0-fraud-signal-validation-harness-v1";

function pct(x: number | null): string {
  return x == null ? "—" : `${(x * 100).toFixed(2)}%`;
}

function SignalValidation({ rows }: { rows: SignalValidationRow[] }) {
  // A detector is "distinguishable from chance" only if the conservative
  // (Wilson 95%) lower bound on its precision exceeds the background sanctioned
  // rate. This is a derived comparison — no magic threshold — so the verdict is
  // honest about thin samples by construction.
  const distinguishable = (r: SignalValidationRow) =>
    r.precision_wilson_lo95 != null &&
    r.base_rate != null &&
    r.precision_wilson_lo95 > r.base_rate;

  return (
    <section className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold tracking-tight">
          Do these detectors actually predict known fraud?
        </h2>
        <code className="font-mono text-[10px] text-zinc-400">
          {VALIDATION_VERSION}
        </code>
      </div>
      <p className="max-w-3xl text-xs text-zinc-500">
        Each behavioral detector is scored against the platform&rsquo;s own
        ground truth &mdash; providers already on an exclusion list
        (LEIE/NJ-Medicaid/SAM). <strong>Precision</strong> = share of flagged
        providers who are sanctioned; <strong>base rate</strong> = sanctioned
        share of all billing providers; <strong>lift</strong> = precision ÷ base
        rate. A detector only &ldquo;beats chance&rdquo; if the conservative 95%
        lower bound on its precision clears the base rate.
      </p>

      <div className="mt-3 rounded-md border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30 p-3 text-[11px] leading-relaxed text-amber-800 dark:text-amber-200">
        <strong>Honest caveat — labels are too thin at NJ scale.</strong> The
        ground-truth set (providers excluded <em>and</em> still billing) is only
        a handful of NPIs per year, so the &ldquo;true positive&rdquo; overlaps
        below are single-digit and the lift figures are <em>not yet
        statistically meaningful</em>. The counts are shown precisely so the
        thinness is visible, never hidden. Validating precision properly needs a
        national billing universe (thousands of labels) &mdash; the next data
        step. This panel is the platform measuring itself, not a claim that any
        detector is proven.
      </div>

      <div className="mt-3 overflow-x-auto">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="border-b border-zinc-200 dark:border-zinc-800 text-left text-[10px] uppercase tracking-wider text-zinc-500">
              <th className="py-2 pr-3 font-semibold">Detector</th>
              <th className="py-2 pr-3 font-semibold">Cycle</th>
              <th className="py-2 pr-3 text-right font-semibold">Flagged</th>
              <th className="py-2 pr-3 text-right font-semibold">
                Also sanctioned
              </th>
              <th className="py-2 pr-3 text-right font-semibold">
                Precision (95% LB)
              </th>
              <th className="py-2 pr-3 text-right font-semibold">Base rate</th>
              <th className="py-2 pr-3 text-right font-semibold">Lift</th>
              <th className="py-2 pl-3 font-semibold">Verdict</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {rows.map((r) => {
              const ok = distinguishable(r);
              return (
                <tr
                  key={`${r.cycle}|${r.signal_id}`}
                  className="border-b border-zinc-100 dark:border-zinc-800/60"
                >
                  <td className="py-1.5 pr-3">
                    <span className="font-sans text-zinc-700 dark:text-zinc-300">
                      {r.signal_id}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 text-zinc-500">{r.cycle}</td>
                  <td className="py-1.5 pr-3 text-right">
                    {r.n_flagged.toLocaleString()}
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    {r.n_true_positive}
                    <span className="text-zinc-400"> / {r.n_positives}</span>
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    {pct(r.precision)}
                    <span className="text-zinc-400">
                      {" "}
                      ({pct(r.precision_wilson_lo95)})
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 text-right text-zinc-500">
                    {pct(r.base_rate)}
                  </td>
                  <td className="py-1.5 pr-3 text-right">
                    {r.lift == null ? "—" : `${r.lift.toFixed(2)}×`}
                  </td>
                  <td className="py-1.5 pl-3">
                    <span
                      className={`inline-flex rounded-full px-2 py-0.5 text-[10px] font-sans font-semibold ${
                        ok
                          ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900 dark:text-emerald-100"
                          : "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"
                      }`}
                      title={
                        ok
                          ? `95% lower bound on precision (${pct(
                              r.precision_wilson_lo95,
                            )}) exceeds the base rate (${pct(
                              r.base_rate,
                            )}) — but on only ${r.n_true_positive} overlap${
                              r.n_true_positive === 1 ? "" : "s"
                            }, so read it as marginal, not proven.`
                          : `95% lower bound on precision (${pct(
                              r.precision_wilson_lo95,
                            )}) does not clear the base rate (${pct(
                              r.base_rate,
                            )}) — not statistically distinguishable from chance.`
                      }
                    >
                      {ok ? "sig. @95%" : "not sig."}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
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
          <strong>Enforcement status comes first.</strong> An entity already on
          an exclusion or debarment list (HHS-OIG&nbsp;LEIE, NJ-Medicaid, SAM)
          has, by definition, already been caught &mdash; the listing <em>is</em>{" "}
          the enforcement action. Worse for a whistleblower, the False Claims Act
          public-disclosure bar (31&nbsp;U.S.C.&nbsp;§&nbsp;3730(e)(4)) makes a
          provider derivable from public lists weak relator material. So those
          entities are demoted to a separate lane, and the queue leads with{" "}
          <strong>undetected</strong> providers: a behavioral billing pattern
          fired, but no enforcement action exists yet.
        </p>
        <p>
          Within the undetected lane the ranking is{" "}
          <em>lexicographic</em>, not a weighted blend:{" "}
          <strong>financial scale</strong> &mdash; the provider&rsquo;s real peak
          single-year Medicare volume (Part&nbsp;B payment + Part&nbsp;D drug
          cost), since a behavioral outlier&rsquo;s raw value is a rate, not a
          dollar &mdash; then <strong>multi-source corroboration</strong> (how
          many independent detector families fired), then{" "}
          <strong>severity</strong>. Every ordering key is a measured dollar or a
          count; there is no composite score.
        </p>
        <p>
          A lead is a <em>statistical anomaly routed for review</em>, not a
          finding of fraud &mdash; a high-volume practice can be perfectly
          legitimate. Each card links to the entity&rsquo;s evidence, the
          enforcement channel, and the governing statute so an analyst can verify
          before acting.
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
