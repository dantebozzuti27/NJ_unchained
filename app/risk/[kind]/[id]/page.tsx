import Link from "next/link";
import { notFound } from "next/navigation";

import { isDbReachable } from "@/lib/db";
import {
  getEntityDetail,
  getEntityEvidenceCards,
  getEntityHeader,
  isValidKind,
  resolveDefaultCycle,
} from "@/lib/queries";
import { fmtScore, riskTier } from "@/lib/format";
import type { EntityHeaderInfo, EvidenceCard } from "@/lib/types";

export const dynamic = "force-dynamic";
export const revalidate = 0;

interface RouteParams {
  kind: string;
  id: string;
}

interface SearchParams {
  cycle?: string;
}

const KIND_LABELS: Record<string, string> = {
  candidate: "Candidate",
  committee: "Committee",
  treasurer: "Treasurer",
  donor: "Donor",
  donor_cluster: "Donor cluster",
  contractor: "Contractor",
  address: "Address cluster",
  nj_state_candidate: "NJ state candidate",
};

const PARTY_STYLE: Record<string, { fg: string; bg: string }> = {
  DEM: {
    fg: "text-blue-900 dark:text-blue-100",
    bg: "bg-blue-100 dark:bg-blue-900",
  },
  REP: {
    fg: "text-red-900 dark:text-red-100",
    bg: "bg-red-100 dark:bg-red-900",
  },
  IND: {
    fg: "text-purple-900 dark:text-purple-100",
    bg: "bg-purple-100 dark:bg-purple-900",
  },
};

export default async function EntityDetailPage({
  params,
  searchParams,
}: {
  params: Promise<RouteParams>;
  searchParams: Promise<SearchParams>;
}) {
  const { kind: rawKind, id: rawId } = await params;
  const search = await searchParams;
  const reachable = await isDbReachable();
  if (!reachable.reachable) {
    return (
      <div className="rounded-md bg-amber-50 dark:bg-amber-950 p-4 text-sm text-amber-800 dark:text-amber-200">
        Database not reachable. Configure{" "}
        <code className="font-mono">NEON_DATABASE_URL</code>.
      </div>
    );
  }

  const kind = decodeURIComponent(rawKind);
  const id = decodeURIComponent(rawId);
  if (!isValidKind(kind)) {
    notFound();
  }
  const cycle = search.cycle ?? (await resolveDefaultCycle());

  // Header source priority:
  //   1. evidence-card row (richest metadata when signals fire)
  //   2. v_entity_fraud_risk row (signals fired but the evidence-card
  //      JOIN missed -- shouldn't happen but is defensive)
  //   3. raw.fec_candidate / raw.fec_committee (clean entities -- the
  //      most common case for sitting incumbents who pass every signal).
  // If none of the three find the entity, render the explicit
  // "entity not found" surface.
  const [detail, cards, header] = await Promise.all([
    getEntityDetail({ cycle, kind, id }),
    getEntityEvidenceCards({ cycle, kind, id }),
    getEntityHeader({ cycle, kind, id }),
  ]);

  const headerCard = cards[0];
  const headerSource: EntityHeaderInfo | null =
    headerCard != null
      ? {
          cycle: headerCard.cycle,
          entity_kind: headerCard.entity_kind,
          entity_id: headerCard.entity_id,
          display_name: headerCard.display_name,
          is_nj: headerCard.is_nj,
          office_code: headerCard.office_code,
          office_state: headerCard.office_state,
          office_district: headerCard.office_district,
          office_party: headerCard.office_party,
          office_incumbent_status: headerCard.office_incumbent_status,
        }
      : header;

  if (!detail && !headerSource) {
    return (
      <div className="space-y-3">
        <div className="rounded-md border border-zinc-300 dark:border-zinc-700 p-6">
          <p className="font-semibold">Entity not found.</p>
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
            <span className="font-mono">{kind}</span> /{" "}
            <span className="font-mono">{id}</span> has no rows in cycle{" "}
            <span className="font-mono">{cycle}</span>.
          </p>
          <p className="mt-3 text-sm">
            <Link href="/risk" className="underline">
              ← Back to the risk queue
            </Link>
          </p>
        </div>
      </div>
    );
  }

  const score = detail?.risk_score ?? 0;
  const tier = riskTier(score);
  const displayName =
    headerSource?.display_name ?? detail?.display_name ?? id;
  const isNj = headerSource?.is_nj ?? false;
  const office = headerSource ? formatOfficeContext(headerSource) : null;

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/risk"
          className="text-sm underline underline-offset-4 text-zinc-600 dark:text-zinc-400"
        >
          ← Back to risk queue
        </Link>
      </div>

      <header className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
        <div className="flex flex-wrap gap-3 items-baseline justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2 text-xs uppercase tracking-wider text-zinc-500">
              <span>{KIND_LABELS[kind] ?? kind}</span>
              <span aria-hidden>·</span>
              <span>cycle {cycle}</span>
              {isNj && (
                <span className="inline-flex rounded-full bg-emerald-100 dark:bg-emerald-900 px-2 py-0.5 text-[10px] font-bold text-emerald-900 dark:text-emerald-100">
                  NJ
                </span>
              )}
              {office && (
                <span className="inline-flex rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 text-[10px] font-medium text-zinc-700 dark:text-zinc-300">
                  {office.label}
                </span>
              )}
              {office?.party && (
                <span
                  className={`inline-flex rounded px-1.5 py-0.5 text-[10px] font-bold ${
                    PARTY_STYLE[office.party]?.bg ?? "bg-zinc-100"
                  } ${PARTY_STYLE[office.party]?.fg ?? "text-zinc-700"}`}
                >
                  {office.party}
                </span>
              )}
            </div>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight">
              {displayName}
            </h1>
            <div className="mt-0.5 font-mono text-xs text-zinc-500">{id}</div>
          </div>
          <div className="flex items-center gap-3">
            <span
              className={`rounded-full px-3 py-1 text-xs font-bold ${tier.bg} ${tier.fg}`}
            >
              {tier.label}
            </span>
            <div className="text-right">
              <div className="font-mono text-3xl font-bold leading-none">
                {fmtScore(score)}
              </div>
              <div className="mt-0.5 text-xs uppercase tracking-wider text-zinc-500">
                Anomaly score
              </div>
            </div>
          </div>
        </div>

        {cards.length === 0 ? (
          <div className="mt-4 rounded-md border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/40 p-3 text-sm text-emerald-900 dark:text-emerald-100">
            <strong>No structural-anomaly signals firing.</strong> This
            entity passed every signal evaluation for cycle {cycle}.
          </div>
        ) : (
          <p className="mt-4 text-sm text-zinc-600 dark:text-zinc-400">
            <span className="font-semibold text-zinc-800 dark:text-zinc-200">
              {cards.length}
            </span>{" "}
            structural-anomaly signal{cards.length === 1 ? "" : "s"} firing.
            Each card below is a single firing observation with the federal
            authority that the predicate codifies. Click <em>verify</em> to
            cross-check against the upstream source.
          </p>
        )}
      </header>

      {cards.length > 0 && (
        <section className="space-y-3">
          {cards.map((c, idx) => (
            <EvidenceCardView key={`${c.signal_id}|${idx}`} card={c} />
          ))}
        </section>
      )}

      <Methodology />
    </div>
  );
}

function EvidenceCardView({ card }: { card: EvidenceCard }) {
  return (
    <article className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-4 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-wider text-zinc-500">
            <span className="font-mono font-semibold text-zinc-800 dark:text-zinc-200">
              {card.signal_id}
            </span>
            {card.citation_authority && (
              <a
                href={card.citation_url ?? undefined}
                target="_blank"
                rel="noreferrer"
                className="font-mono rounded bg-zinc-100 dark:bg-zinc-800 px-1.5 py-0.5 underline-offset-2 hover:underline"
                title={card.rule_text ?? undefined}
              >
                {card.citation_authority}{" "}
                {card.citation_section ?? ""}
              </a>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <SeverityDots level={card.severity} />
          {card.peer_percentile != null && (
            <span
              className="rounded-full bg-zinc-100 dark:bg-zinc-800 px-2 py-0.5 text-xs font-mono"
              title={`Peer cohort: ${card.peer_bucket ?? "—"}`}
            >
              {(card.peer_percentile * 100).toFixed(1)}% pctile
            </span>
          )}
        </div>
      </div>

      <p className="mt-2 text-sm leading-relaxed text-zinc-800 dark:text-zinc-200">
        {card.rendered_explanation}
      </p>

      <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
        <KV
          label="Raw value"
          value={
            card.raw_value == null
              ? "—"
              : Number.isInteger(card.raw_value)
              ? card.raw_value.toString()
              : card.raw_value.toFixed(2)
          }
        />
        <KV label="Severity" value={`${card.severity}/5`} />
        <KV
          label="Peer cohort"
          value={card.peer_bucket ?? "—"}
          mono
        />
        <KV
          label="Authority"
          value={card.citation_authority ?? "—"}
          mono
        />
      </div>

      {card.severity_precedent_summary && (
        <details className="mt-3 rounded-md border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950 p-3 text-xs">
          <summary className="cursor-pointer font-semibold text-zinc-700 dark:text-zinc-300">
            Why severity {card.severity}/5? · basis: {card.severity_basis}
          </summary>
          <p className="mt-2 leading-relaxed text-zinc-600 dark:text-zinc-400">
            {card.severity_precedent_summary}
          </p>
          {card.severity_precedent_url && (
            <a
              href={card.severity_precedent_url}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-block underline underline-offset-2 text-blue-700 dark:text-blue-300"
            >
              {card.severity_precedent_url}
            </a>
          )}
        </details>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {card.upstream_verify_url && card.upstream_source && (
          <a
            href={card.upstream_verify_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 rounded-md bg-zinc-900 dark:bg-zinc-100 px-3 py-1.5 text-xs font-medium text-white dark:text-zinc-900 hover:opacity-90"
          >
            {card.upstream_verify_label ?? `Verify on ${card.upstream_source}`}
            <span aria-hidden>↗</span>
          </a>
        )}
        {card.citation_url && (
          <a
            href={card.citation_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-700 dark:text-zinc-300 hover:bg-zinc-50 dark:hover:bg-zinc-800"
          >
            Read the rule
            <span aria-hidden>↗</span>
          </a>
        )}
      </div>
    </article>
  );
}

function SeverityDots({ level }: { level: number }) {
  const labels = ["", "Low", "Low-mid", "Mid", "High", "Critical"];
  return (
    <span
      className="inline-flex items-center gap-1 font-mono"
      title={`Severity ${level}/5 (${labels[level] ?? "?"})`}
    >
      <span className="text-zinc-700 dark:text-zinc-300">
        {"●".repeat(level)}
      </span>
      <span className="text-zinc-300 dark:text-zinc-700">
        {"○".repeat(5 - level)}
      </span>
    </span>
  );
}

function KV({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div className={`mt-0.5 ${mono ? "font-mono" : ""}`}>{value}</div>
    </div>
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
          The anomaly score (0–100) is a composite of per-signal severities
          weighted by peer percentile within the entity&apos;s peer cohort
          (e.g., NJ candidates 2024). Each signal codifies a specific
          federal predicate (11 CFR, 42 USC, FAR) and surfaces a{" "}
          <em>structural anomaly</em>, not a confirmed violation.
        </p>
        <p>
          Severity (1–5) is calibrated against documented federal precedent
          where available (FEC MURs, OIG reports, FAR authority) or against
          empirical historical anomaly rates where no enforcement matter
          motivates the threshold. Click the &ldquo;Why severity N/5?&rdquo;
          disclosure on each card to see the precedent that anchors that
          signal&apos;s severity.
        </p>
        <p>
          The platform is research-tier: scores are designed to{" "}
          <em>route analyst attention</em>, not to assert wrongdoing.
        </p>
      </div>
    </details>
  );
}

function formatOfficeContext(info: EntityHeaderInfo): {
  label: string;
  party: string | null;
} | null {
  if (!info.office_code) return null;
  const party = info.office_party ?? null;
  if (info.office_code === "S") {
    return {
      label: `U.S. Senate (${info.office_state ?? "?"}, ${
        info.office_incumbent_status === "I" ? "incumbent" : "candidate"
      })`,
      party,
    };
  }
  if (info.office_code === "H") {
    const dist = info.office_district?.replace(/^0/, "") ?? "?";
    return {
      label: `U.S. House ${info.office_state ?? ""}-${dist} (${
        info.office_incumbent_status === "I" ? "incumbent" : "candidate"
      })`,
      party,
    };
  }
  // Fallthrough -- includes nj_state_candidate, where getEntityHeader
  // stores the human office_label (e.g. "Governor of New Jersey") in
  // office_code because there is no FEC-equivalent single-letter enum
  // for state-level offices. Rendering verbatim is the correct surface.
  return { label: info.office_code, party };
}
