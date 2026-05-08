/**
 * Horizontal bar chart of all 21 NJ counties by burden ratio.
 *
 * Server-rendered SVG. Each row is a county; bar length is proportional
 * to (ratio - 1.0), so a county at exactly parity (1.0) draws no bar.
 * Bars to the right of the centerline mean housing outpaced wages;
 * bars to the left (rare) mean wages outpaced housing.
 *
 * Why a baseline-centered chart, not a 0-anchored one
 * --------------------------------------------------
 * The interesting question is "is this county above or below parity?",
 * not "what is the absolute ratio?" Centering on 1.0 makes the
 * STRESS / TRACKING / LAGGING distinction immediate and lets a viewer
 * scan the column for outliers without doing math.
 */

import { burdenTier, type CountyBurdenRow } from "@/lib/housing";

interface BurdenBarChartProps {
  rows: CountyBurdenRow[];
  /** Pixel height per row. */
  rowHeight?: number;
  /** Total chart width including labels. */
  width?: number;
}

export function BurdenBarChart({
  rows,
  rowHeight = 22,
  width = 900,
}: BurdenBarChartProps) {
  const labelW = 130;
  const valueW = 70;
  const gutter = 8;
  const innerW = width - labelW - valueW - gutter * 2;
  const totalH = rows.length * rowHeight + 30;

  // Compute symmetric range so 1.0 sits in the visual center.
  const ratios = rows.map((r) => r.burden_ratio).filter((r): r is number => r != null);
  const maxAbsDelta = ratios.length
    ? Math.max(...ratios.map((r) => Math.abs(r - 1.0)), 0.5)
    : 0.5;
  const padded = maxAbsDelta * 1.1;
  const xMin = 1.0 - padded;
  const xMax = 1.0 + padded;
  const sx = (v: number) =>
    labelW + gutter + ((v - xMin) / (xMax - xMin)) * innerW;
  const x0 = sx(1.0);

  return (
    <svg
      viewBox={`0 0 ${width} ${totalH}`}
      width="100%"
      style={{ maxWidth: width }}
      role="img"
    >
      <title>Housing burden ratio by NJ county (1.0 = parity vs base year)</title>

      {/* Vertical gridlines at 0.5 increments */}
      {axisTicks(xMin, xMax).map((t) => (
        <g key={t}>
          <line
            x1={sx(t)}
            x2={sx(t)}
            y1={20}
            y2={totalH - 4}
            stroke="currentColor"
            strokeWidth={t === 1.0 ? 1 : 0.4}
            opacity={t === 1.0 ? 0.5 : 0.18}
          />
          <text
            x={sx(t)}
            y={14}
            textAnchor="middle"
            fontSize="10"
            fill="currentColor"
            opacity={0.55}
            fontFamily="monospace"
          >
            {t.toFixed(2)}
          </text>
        </g>
      ))}

      {rows.map((r, i) => {
        const y = 24 + i * rowHeight;
        const tier = burdenTier(r.burden_ratio);
        const ratio = r.burden_ratio;
        const barFill = tier.label === "STRESS"
          ? "#fca5a5"
          : tier.label === "ELEVATED"
            ? "#fdba74"
            : tier.label === "TRACKING"
              ? "#86efac"
              : tier.label === "LAGGING"
                ? "#93c5fd"
                : "#a1a1aa";

        return (
          <g key={r.county_id}>
            <text
              x={labelW - 4}
              y={y + rowHeight / 2 + 4}
              textAnchor="end"
              fontSize="11"
              fill="currentColor"
            >
              {r.county_name}
            </text>
            {ratio != null ? (
              <>
                <rect
                  x={Math.min(x0, sx(ratio))}
                  y={y + 4}
                  width={Math.abs(sx(ratio) - x0)}
                  height={rowHeight - 8}
                  fill={barFill}
                  opacity={0.85}
                  rx={2}
                />
                <text
                  x={width - valueW + 4}
                  y={y + rowHeight / 2 + 4}
                  fontSize="11"
                  fill="currentColor"
                  fontFamily="monospace"
                  fontWeight="600"
                >
                  {ratio.toFixed(2)}×
                </text>
              </>
            ) : (
              <text
                x={width - valueW + 4}
                y={y + rowHeight / 2 + 4}
                fontSize="11"
                fill="currentColor"
                opacity={0.45}
                fontFamily="monospace"
              >
                —
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

function axisTicks(xMin: number, xMax: number): number[] {
  const ticks: number[] = [];
  const step = 0.25;
  const lo = Math.ceil(xMin / step) * step;
  for (let v = lo; v <= xMax + 1e-9; v += step) {
    ticks.push(Math.round(v * 100) / 100);
  }
  if (!ticks.includes(1.0)) ticks.push(1.0);
  return ticks.sort((a, b) => a - b);
}
