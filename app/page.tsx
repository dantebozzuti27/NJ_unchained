import Link from "next/link";

import { isDbReachable } from "@/lib/db";
import { getPlatformStatus } from "@/lib/queries";
import { fmtVintage } from "@/lib/format";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export default async function HomePage() {
  const reachable = await isDbReachable();

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-3xl font-bold tracking-tight">
          New Jersey, unchained from corruption and cost.
        </h1>
        <p className="mt-3 max-w-2xl text-zinc-600 dark:text-zinc-400">
          Two open-data screeners on one platform. The{" "}
          <strong>housing tracker</strong> measures the divergence between
          home-price growth (FHFA HPI) and real wage growth (ACS) for each
          of New Jersey&rsquo;s 21 counties. The{" "}
          <strong>civic-integrity screener</strong> ranks political and
          federal-procurement entities by cross-source risk evidence
          (FEC × USAspending × LEIE × SAM.gov). Every figure links to its
          underlying source. No probability of fraud is asserted; risk is
          a percentile of anomalousness within an entity&rsquo;s peer
          group.
        </p>
        <div className="mt-6 flex flex-wrap gap-3">
          <Link
            href="/housing"
            className="inline-flex items-center gap-2 rounded-md bg-zinc-900 dark:bg-zinc-100 px-4 py-2 text-sm font-medium text-white dark:text-zinc-900 hover:bg-zinc-700 dark:hover:bg-zinc-200"
          >
            Housing burden →
          </Link>
          <Link
            href="/risk"
            className="inline-flex items-center gap-2 rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm font-medium hover:bg-zinc-100 dark:hover:bg-zinc-800"
          >
            Risk queue →
          </Link>
          <Link
            href="/about"
            className="inline-flex items-center gap-2 rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm font-medium hover:bg-zinc-100 dark:hover:bg-zinc-800"
          >
            Methodology
          </Link>
        </div>
      </section>

      <section className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-5">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500">
          Platform status
        </h2>
        {!reachable.reachable ? (
          <DbDownNotice error={reachable.error} />
        ) : (
          <PlatformStats />
        )}
      </section>
    </div>
  );
}

function DbDownNotice({ error }: { error?: string }) {
  return (
    <div className="mt-3 rounded-md bg-amber-50 dark:bg-amber-950 p-4 text-sm">
      <div className="font-medium text-amber-800 dark:text-amber-200">
        Database not reachable yet.
      </div>
      <p className="mt-1 text-amber-700 dark:text-amber-300">
        This deployment has not been wired to a Postgres instance, or the{" "}
        <code className="font-mono">NEON_DATABASE_URL</code> env var has
        not been set. The screener works once a Neon (or compatible)
        Postgres URL is configured in the Vercel project. See the
        repository <code className="font-mono">web/README.md</code> for
        setup steps.
      </p>
      {error && (
        <pre className="mt-2 overflow-x-auto rounded bg-amber-100/60 dark:bg-amber-900/40 p-2 text-xs text-amber-900 dark:text-amber-200">
          {error}
        </pre>
      )}
    </div>
  );
}

async function PlatformStats() {
  let status;
  try {
    status = await getPlatformStatus();
  } catch (e) {
    return (
      <div className="mt-3 rounded-md bg-red-50 dark:bg-red-950 p-4 text-sm">
        <div className="font-medium text-red-800 dark:text-red-200">
          Database reachable, but the fraud-engine schema is not present.
        </div>
        <p className="mt-1 text-red-700 dark:text-red-300">
          Run the migrations (<code className="font-mono">db/migrations/*.sql</code>)
          against your Neon instance to populate the schema. See{" "}
          <code className="font-mono">README.md</code> for the bootstrap
          command.
        </p>
        <pre className="mt-2 overflow-x-auto rounded bg-red-100/60 dark:bg-red-900/40 p-2 text-xs text-red-900 dark:text-red-200">
          {e instanceof Error ? e.message : String(e)}
        </pre>
      </div>
    );
  }

  return (
    <dl className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
      <Stat label="Default cycle" value={status.cycle_default} />
      <Stat
        label="Scored entities"
        value={status.total_entities.toLocaleString()}
      />
      <Stat
        label="Signals fired"
        value={status.total_signals_fired.toLocaleString()}
      />
      <Stat
        label="Last refreshed"
        value={fmtVintage(status.vintage_iso)}
      />
      <div className="col-span-full">
        <div className="text-xs uppercase tracking-wider text-zinc-500">
          Signals by family
        </div>
        <div className="mt-1 flex flex-wrap gap-2">
          {Object.entries(status.signal_count_by_family).length === 0 ? (
            <span className="text-zinc-500 italic">none yet</span>
          ) : (
            Object.entries(status.signal_count_by_family).map(([fam, n]) => (
              <span
                key={fam}
                className="rounded-full bg-zinc-100 dark:bg-zinc-800 px-2.5 py-0.5 text-xs font-medium font-mono"
              >
                {fam}: {n.toLocaleString()}
              </span>
            ))
          )}
        </div>
      </div>
    </dl>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-zinc-500">
        {label}
      </dt>
      <dd className="mt-0.5 font-mono text-base font-semibold">{value}</dd>
    </div>
  );
}
