import Link from "next/link";
import { notFound } from "next/navigation";

import { isDbReachable } from "@/lib/db";
import {
  getEntityDetail,
  isValidKind,
  resolveDefaultCycle,
} from "@/lib/queries";
import { fmtPct, fmtScore, fmtUsd, riskTier } from "@/lib/format";
import type { SignalRow } from "@/lib/types";

export const dynamic = "force-dynamic";
export const revalidate = 0;

interface RouteParams {
  kind: string;
  id: string;
}

interface SearchParams {
  cycle?: string;
}

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

  const detail = await getEntityDetail({ cycle, kind, id });
  if (!detail) {
    return (
      <div className="space-y-3">
        <div className="rounded-md border border-zinc-300 dark:border-zinc-700 p-6">
          <p className="font-semibold">Entity not found.</p>
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
            <span className="font-mono">{kind}</span> /{" "}
            <span className="font-mono">{id}</span> has no rows in
            cycle <span className="font-mono">{cycle}</span>.
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

  const tier = riskTier(detail.risk_score);

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
          <div>
            <div className="text-xs uppercase tracking-wider text-zinc-500">
              {detail.entity_kind} / cycle {detail.cycle}
            </div>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight">
              {detail.display_name ?? detail.entity_id}
            </h1>
            {detail.display_name && (
              <div className="mt-0.5 font-mono text-xs text-zinc-500">
                {detail.entity_id}
              </div>
            )}
          </div>
          <div className="flex items-center gap-3">
            <span
              className={`rounded-full px-3 py-1 text-xs font-bold ${tier.bg} ${tier.fg}`}
            >
              {tier.label}
            </span>
            <div className="text-right">
              <div className="font-mono text-3xl font-bold leading-none">
                {fmtScore(detail.risk_score)}
              </div>
              <div className="mt-0.5 text-xs uppercase tracking-wider text-zinc-500">
                Risk score
              </div>
            </div>
          </div>
        </div>

        <div className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
          <KV label="Contributing families" value={String(detail.n_contributing_families)} />
          <KV
            label="Signal families"
            value={
              detail.signal_families.length
                ? detail.signal_families.join(", ")
                : "(none above 0.95 percentile)"
            }
          />
          <KV label="Signals firing" value={String(detail.signals.length)} />
          <KV
            label="Diversity bonus"
            value={
              detail.n_contributing_families > 1
                ? `+${(
                    0.01 *
                    Math.pow(detail.n_contributing_families - 1, 2)
                  ).toFixed(2)}`
                : "—"
            }
          />
        </div>
      </header>

      <section>
        <h2 className="text-base font-semibold tracking-tight mb-3">
          Signal evidence
        </h2>
        {detail.signals.length === 0 ? (
          <p className="text-sm text-zinc-500">
            No L1 signal observations for this entity.
          </p>
        ) : (
          <SignalsTable signals={detail.signals} />
        )}
      </section>
    </div>
  );
}

function KV({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div className="mt-0.5 font-mono">{value}</div>
    </div>
  );
}

function SignalsTable({ signals }: { signals: SignalRow[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
      <table className="min-w-full text-sm">
        <thead className="bg-zinc-100 dark:bg-zinc-900 text-left text-xs uppercase tracking-wider text-zinc-500">
          <tr>
            <th className="px-3 py-2">Signal</th>
            <th className="px-3 py-2">Family</th>
            <th className="px-3 py-2">Severity</th>
            <th className="px-3 py-2 text-right">Raw value</th>
            <th className="px-3 py-2 text-right">Percentile</th>
            <th className="px-3 py-2">Peer bucket</th>
            <th className="px-3 py-2">Evidence</th>
          </tr>
        </thead>
        <tbody>
          {signals.map((s) => (
            <tr
              key={s.signal_id}
              className={`border-t border-zinc-200 dark:border-zinc-800 ${
                s.is_contributing
                  ? "bg-amber-50/40 dark:bg-amber-950/20"
                  : ""
              }`}
            >
              <td className="px-3 py-2 font-mono text-xs">
                {s.signal_id}
                {s.is_contributing && (
                  <span
                    title="Above 0.95 percentile -- contributes to risk_score"
                    className="ml-1.5 rounded bg-amber-200 dark:bg-amber-800 px-1 text-[10px] font-bold text-amber-900 dark:text-amber-100"
                  >
                    ★
                  </span>
                )}
              </td>
              <td className="px-3 py-2 text-xs font-mono text-zinc-600 dark:text-zinc-400">
                {s.signal_family ?? "(none)"}
              </td>
              <td className="px-3 py-2 text-center font-mono">
                {"●".repeat(s.severity)}
                <span className="text-zinc-300 dark:text-zinc-700">
                  {"○".repeat(5 - s.severity)}
                </span>
              </td>
              <td className="px-3 py-2 text-right font-mono">
                {fmtUsd(s.raw_value)}
                {s.min_actionable_threshold != null && (
                  <span
                    className="block text-[10px] text-zinc-500"
                    title="min_actionable_threshold from fraud_signal_config"
                  >
                    floor {fmtUsd(s.min_actionable_threshold)}
                  </span>
                )}
              </td>
              <td className="px-3 py-2 text-right font-mono">
                {fmtPct(s.peer_percentile)}
              </td>
              <td className="px-3 py-2 text-xs font-mono text-zinc-600 dark:text-zinc-400">
                {s.peer_bucket}
              </td>
              <td className="px-3 py-2 text-xs">
                <a
                  href={s.evidence_url}
                  className="underline underline-offset-2 text-blue-700 dark:text-blue-300"
                >
                  link
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="border-t border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-900 px-3 py-2 text-xs text-zinc-500">
        ★ = signal&apos;s peer percentile is &ge; 0.95, contributing to the
        composite score. Signals below 0.95 are surfaced for context but
        do NOT add to the score directly.
      </div>
    </div>
  );
}
