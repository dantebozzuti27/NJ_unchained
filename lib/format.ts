/** Display formatters used across pages. */

export function fmtScore(n: number): string {
  if (!Number.isFinite(n)) return "-";
  return n.toFixed(2);
}

export function fmtUsd(n: number): string {
  if (!Number.isFinite(n)) return "-";
  if (Math.abs(n) >= 1_000_000)
    return `$${(n / 1_000_000).toFixed(2)}M`;
  if (Math.abs(n) >= 1_000)
    return `$${(n / 1_000).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

export function fmtPct(n: number): string {
  if (!Number.isFinite(n)) return "-";
  return `${(n * 100).toFixed(1)}%`;
}

/**
 * Map a 0-100 risk score to a tier label + Tailwind class trio.
 * Thresholds are calibrated to the L3a v_entity_fraud_risk
 * distribution: in steady state ~99% of entities score < 5,
 * ~99.9% < 25, and a single critical-fraud entity reaches 60+.
 */
export function riskTier(score: number): {
  label: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "INFO";
  bg: string;
  fg: string;
  ring: string;
} {
  if (score >= 60)
    return {
      label: "CRITICAL",
      bg: "bg-red-100 dark:bg-red-950",
      fg: "text-red-800 dark:text-red-200",
      ring: "ring-red-500",
    };
  if (score >= 25)
    return {
      label: "HIGH",
      bg: "bg-orange-100 dark:bg-orange-950",
      fg: "text-orange-800 dark:text-orange-200",
      ring: "ring-orange-500",
    };
  if (score >= 10)
    return {
      label: "MEDIUM",
      bg: "bg-amber-100 dark:bg-amber-950",
      fg: "text-amber-800 dark:text-amber-200",
      ring: "ring-amber-500",
    };
  if (score >= 1)
    return {
      label: "LOW",
      bg: "bg-emerald-100 dark:bg-emerald-950",
      fg: "text-emerald-800 dark:text-emerald-200",
      ring: "ring-emerald-500",
    };
  return {
    label: "INFO",
    bg: "bg-blue-50 dark:bg-blue-950",
    fg: "text-blue-700 dark:text-blue-300",
    ring: "ring-blue-400",
  };
}

export function fmtVintage(iso: string | null): string {
  if (!iso) return "no data yet";
  const d = new Date(iso);
  return d.toLocaleString("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
