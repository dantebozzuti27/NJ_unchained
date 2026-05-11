/**
 * The Collapse Curve -- the spec's "viral insight chart" (idea §7.3).
 *
 * Three series in dollar units (NOT indexed) so the gap is legible
 * in the units that actually matter to the reader:
 *
 *   1. Actual median household income (blue line)
 *   2. Required income to afford the median home at HUD 30% (red line)
 *   3. The shaded area between them
 *        - red-tinted when required > actual (housing unaffordable)
 *        - green-tinted when required < actual (housing affordable)
 *
 * Server-rendered SVG, zero client JS. Same architectural choice as
 * `<Sparkline />`: chart libraries weigh 80-200KB and a static SVG
 * does the same job at 0KB.
 *
 * Why dollar y-axis vs indexed
 * ----------------------------
 * The Sparkline component plots indexed values (base=100) because it's
 * comparing two series with different absolute units. The Collapse
 * Curve compares two series in the SAME unit (dollars), and the
 * absolute gap is the headline -- a "$80K shortfall" lands harder
 * than "1.6x ratio". We plot in dollars and label the axis in K.
 *
 * Nominal vs real-dollar lens
 * ---------------------------
 * The chart's `lens` is a UI concern, not a data concern: the caller
 * decides which series to pass in. When the caller passes real-dollar
 * (CPI-deflated) values, it also supplies `realDollarsBaseYear` so the
 * legend / axis can explicitly label "(in 2024 dollars)" -- substrate-
 * honesty rule: never let a reader guess which year's dollars are on
 * the axis. The chart itself is dimensionless across the two lenses;
 * only the labels change.
 */

export interface CollapseCurvePoint {
  year: number;
  median_income: number | null;
  required_income: number | null;
}

export interface CollapseCurveProps {
  points: CollapseCurvePoint[];
  /** Pixel width / height. */
  width?: number;
  height?: number;
  /** Optional title for tooltip / screen reader. */
  title?: string;
  /**
   * Dollar lens that the points are denominated in. Defaults to
   * "nominal" so the existing caller behavior is preserved. When set
   * to "real", the legend appends "(in <realDollarsBaseYear> dollars)".
   */
  lens?: "nominal" | "real";
  /**
   * Real-dollar base year for labeling. Required when lens === "real";
   * ignored when lens === "nominal". Substrate-honest: if the caller
   * cannot supply a base year (CPI substrate empty), it should pass
   * lens="nominal" rather than mislabel real-dollar values.
   */
  realDollarsBaseYear?: number | null;
}

const PADDING = { top: 24, right: 28, bottom: 40, left: 72 };

/** "Nice" round number rounded UP to a clean tick boundary. */
function roundUpTo(n: number, step: number): number {
  return Math.ceil(n / step) * step;
}

/** Choose a y-axis tick step appropriate to the value range. */
function pickTickStep(range: number): number {
  if (range <= 50_000) return 10_000;
  if (range <= 100_000) return 25_000;
  if (range <= 250_000) return 50_000;
  if (range <= 500_000) return 100_000;
  return 250_000;
}

function fmtK(dollars: number): string {
  if (dollars >= 1_000_000)
    return `$${(dollars / 1_000_000).toFixed(1)}M`;
  if (dollars >= 1_000) return `$${Math.round(dollars / 1_000)}K`;
  return `$${dollars.toFixed(0)}`;
}

export function CollapseCurve({
  points,
  width = 720,
  height = 380,
  title,
  lens = "nominal",
  realDollarsBaseYear = null,
}: CollapseCurveProps) {
  const lensSuffix =
    lens === "real" && realDollarsBaseYear != null
      ? ` (in ${realDollarsBaseYear} dollars)`
      : "";
  // Only plot points where BOTH series are non-null. Substrate honesty:
  // years with only median income (no required because tax tables not
  // seeded) are omitted from the chart, not extrapolated.
  const plotted = points.filter(
    (p) => p.median_income != null && p.required_income != null,
  );

  if (plotted.length < 2) {
    return (
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        className="text-zinc-400"
        role="img"
      >
        {title ? <title>{title}</title> : null}
        <text
          x={width / 2}
          y={height / 2}
          textAnchor="middle"
          dominantBaseline="middle"
          fontSize="14"
          fill="currentColor"
        >
          Curve under construction — at least 2 years of joined data
          required (tax tables seeded only for 2023, 2024 currently).
        </text>
      </svg>
    );
  }

  const xs = plotted.map((p) => p.year);
  const allYs = plotted
    .flatMap((p) => [p.median_income!, p.required_income!])
    .filter((y): y is number => y != null);

  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMinRaw = Math.min(...allYs);
  const yMaxRaw = Math.max(...allYs);
  const range = yMaxRaw - yMinRaw;
  const tickStep = pickTickStep(yMaxRaw);
  // Clean axis bounds: round y_max up to tick, y_min down to nearest
  // tick (or 0 if close enough). Ensures gridlines align with labels.
  const yMax = roundUpTo(yMaxRaw + range * 0.05, tickStep);
  const yMin = Math.max(0, Math.floor(yMinRaw / tickStep) * tickStep);

  const innerW = width - PADDING.left - PADDING.right;
  const innerH = height - PADDING.top - PADDING.bottom;

  const sx = (year: number) =>
    PADDING.left +
    ((year - xMin) / Math.max(xMax - xMin, 1)) * innerW;
  const sy = (val: number) =>
    PADDING.top +
    innerH -
    ((val - yMin) / Math.max(yMax - yMin, 1)) * innerH;

  // Build the two line paths.
  const medianPath =
    "M " +
    plotted
      .map((p) => `${sx(p.year).toFixed(2)},${sy(p.median_income!).toFixed(2)}`)
      .join(" L ");
  const requiredPath =
    "M " +
    plotted
      .map(
        (p) =>
          `${sx(p.year).toFixed(2)},${sy(p.required_income!).toFixed(2)}`,
      )
      .join(" L ");

  // Filled gap polygon: trace required-line forward, then median-line
  // backward, then close. Tinted by overall sign (most points either
  // unaffordable or affordable; mixed ranges still render meaningfully).
  const requiredAvg =
    plotted.reduce((s, p) => s + (p.required_income ?? 0), 0) / plotted.length;
  const medianAvg =
    plotted.reduce((s, p) => s + (p.median_income ?? 0), 0) / plotted.length;
  const gapIsUnfavorable = requiredAvg > medianAvg;

  const gapPath =
    "M " +
    plotted
      .map(
        (p) =>
          `${sx(p.year).toFixed(2)},${sy(p.required_income!).toFixed(2)}`,
      )
      .join(" L ") +
    " L " +
    plotted
      .slice()
      .reverse()
      .map(
        (p) => `${sx(p.year).toFixed(2)},${sy(p.median_income!).toFixed(2)}`,
      )
      .join(" L ") +
    " Z";

  // Y-axis ticks.
  const tickValues: number[] = [];
  for (let v = yMin; v <= yMax + 1; v += tickStep) tickValues.push(v);

  // X-axis labels: every year if <=8 points, otherwise every other year.
  const labelEvery = plotted.length <= 8 ? 1 : 2;

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      role="img"
      className="text-zinc-700 dark:text-zinc-300"
    >
      {title ? <title>{title}</title> : null}

      {/* Y-axis tick lines + labels */}
      {tickValues.map((v) => (
        <g key={`yt-${v}`}>
          <line
            x1={PADDING.left}
            x2={width - PADDING.right}
            y1={sy(v)}
            y2={sy(v)}
            stroke="currentColor"
            strokeWidth={0.5}
            opacity={0.12}
          />
          <text
            x={PADDING.left - 8}
            y={sy(v)}
            textAnchor="end"
            dominantBaseline="middle"
            fontSize="11"
            fill="currentColor"
            opacity={0.7}
            className="font-mono"
          >
            {fmtK(v)}
          </text>
        </g>
      ))}

      {/* X-axis baseline */}
      <line
        x1={PADDING.left}
        x2={width - PADDING.right}
        y1={PADDING.top + innerH}
        y2={PADDING.top + innerH}
        stroke="currentColor"
        strokeWidth={0.8}
        opacity={0.4}
      />

      {/* X-axis year labels */}
      {plotted.map((p, i) =>
        i % labelEvery === 0 || i === plotted.length - 1 ? (
          <text
            key={`xt-${p.year}`}
            x={sx(p.year)}
            y={PADDING.top + innerH + 16}
            textAnchor="middle"
            fontSize="11"
            fill="currentColor"
            opacity={0.7}
            className="font-mono"
          >
            {p.year}
          </text>
        ) : null,
      )}

      {/* Filled gap area between the two series */}
      <path
        d={gapPath}
        fill={gapIsUnfavorable ? "#dc262620" : "#16a34a20"}
        stroke="none"
      />

      {/* Required-income series (red) */}
      <path
        d={requiredPath}
        fill="none"
        stroke="#dc2626"
        strokeWidth={2.2}
        strokeLinejoin="round"
        strokeLinecap="round"
      />

      {/* Median-income series (blue) */}
      <path
        d={medianPath}
        fill="none"
        stroke="#2563eb"
        strokeWidth={2.2}
        strokeLinejoin="round"
        strokeLinecap="round"
      />

      {/* Per-year markers + invisible hover targets with tooltips */}
      {plotted.map((p) => (
        <g key={`pt-${p.year}`}>
          <circle
            cx={sx(p.year)}
            cy={sy(p.required_income!)}
            r={2.5}
            fill="#dc2626"
          />
          <circle
            cx={sx(p.year)}
            cy={sy(p.median_income!)}
            r={2.5}
            fill="#2563eb"
          />
          {/* Wide invisible hit target with the tooltip; one per year. */}
          <rect
            x={sx(p.year) - 12}
            y={PADDING.top}
            width={24}
            height={innerH}
            fill="transparent"
          >
            <title>
              {`${p.year}: median ${fmtK(p.median_income!)} / required ${fmtK(p.required_income!)} / gap ${fmtK(p.required_income! - p.median_income!)}${lensSuffix}`}
            </title>
          </rect>
        </g>
      ))}

      {/* Legend. Suffixed with the real-dollar base year when lens="real"
          so the reader cannot misread which year's dollars the axis is
          measuring -- the deflation arithmetic is visible to the eye,
          not buried in caller code. */}
      <g transform={`translate(${PADDING.left}, ${PADDING.top - 8})`}>
        <rect x={0} y={-9} width={10} height={3} fill="#2563eb" />
        <text
          x={14}
          y={-7}
          fontSize="11"
          fill="currentColor"
          opacity={0.85}
        >
          {`Median income (actual)${lensSuffix}`}
        </text>
        <rect x={200} y={-9} width={10} height={3} fill="#dc2626" />
        <text
          x={214}
          y={-7}
          fontSize="11"
          fill="currentColor"
          opacity={0.85}
        >
          {`Required income (HUD 30%)${lensSuffix}`}
        </text>
      </g>
    </svg>
  );
}
