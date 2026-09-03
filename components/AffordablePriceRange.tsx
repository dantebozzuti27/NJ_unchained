import Link from "next/link";

import {
  fmtUsd,
  type AffordablePriceRange as PriceRange,
} from "@/lib/personalize";

/**
 * Headline "what house can I afford?" band. Numbers come from
 * derived.f_user_max_affordable_home_price_dti / _post_tax (mig 074).
 * The spread is county property-tax variation inside the same DTI
 * rule, not a second model.
 */
export function AffordablePriceRangeCard({
  range,
  year,
  fullResultsHref,
}: {
  range: PriceRange;
  year: number;
  fullResultsHref?: string;
}) {
  if (range.dti_lo == null || range.dti_hi == null) {
    return (
      <section className="rounded-lg border border-amber-200 dark:border-amber-900 bg-amber-50/60 dark:bg-amber-950/30 p-5">
        <h2 className="text-lg font-semibold">House you can afford</h2>
        <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
          No max-affordable price for {year}. The DCA / FRED mortgage
          substrate is missing for that year — try 2024.
        </p>
      </section>
    );
  }

  const collapsed = range.dti_hi / range.dti_lo <= 1.05;

  return (
    <section className="rounded-lg border border-rose-200 dark:border-rose-900 bg-rose-50/70 dark:bg-rose-950/30 p-5 sm:p-6">
      <div className="text-xs uppercase tracking-wider text-rose-700 dark:text-rose-300">
        House you can afford · {year} substrate
      </div>
      <h2 className="mt-1 text-2xl sm:text-3xl font-semibold tracking-tight">
        {collapsed ? (
          <>
            About{" "}
            <span className="font-mono text-rose-800 dark:text-rose-200">
              {fmtUsd(range.dti_hi)}
            </span>
          </>
        ) : (
          <>
            <span className="font-mono text-rose-800 dark:text-rose-200">
              {fmtUsd(range.dti_lo)}
            </span>
            <span className="mx-2 text-zinc-400">–</span>
            <span className="font-mono text-rose-800 dark:text-rose-200">
              {fmtUsd(range.dti_hi)}
            </span>
          </>
        )}
      </h2>
      <p className="mt-2 text-sm text-zinc-700 dark:text-zinc-300 max-w-3xl">
        Fannie Mae conventional DTI (front 28% / back 36% of gross) on
        your profile, across {range.n_counties} NJ{" "}
        {range.n_counties === 1 ? "county" : "counties"}. The spread is
        local property tax inside the PITI coefficient, not a second
        model.{" "}
        <Link href="/about" className="underline underline-offset-4">
          Methodology
        </Link>
        .
      </p>
      <dl className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
        {range.post_tax_lo != null && range.post_tax_hi != null && (
          <div className="rounded-md border border-zinc-200 dark:border-zinc-800 bg-white/70 dark:bg-zinc-900/50 p-3">
            <dt className="text-xs uppercase tracking-wider text-zinc-500">
              After-tax DTI (more conservative)
            </dt>
            <dd className="mt-1 font-mono">
              {fmtUsd(range.post_tax_lo)} – {fmtUsd(range.post_tax_hi)}
            </dd>
          </div>
        )}
        {range.stretch_hi != null && (
          <div className="rounded-md border border-zinc-200 dark:border-zinc-800 bg-white/70 dark:bg-zinc-900/50 p-3">
            <dt className="text-xs uppercase tracking-wider text-zinc-500">
              Stretch ceiling (HUD outreach multiplier)
            </dt>
            <dd className="mt-1 font-mono">{fmtUsd(range.stretch_hi)}</dd>
          </div>
        )}
      </dl>
      {fullResultsHref && (
        <p className="mt-4 text-sm">
          <Link
            href={fullResultsHref}
            className="font-semibold text-rose-700 dark:text-rose-300 underline underline-offset-4"
          >
            See every county and town →
          </Link>
        </p>
      )}
    </section>
  );
}
