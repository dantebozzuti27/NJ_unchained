/**
 * Disposable Income Trajectory chart -- spec §5.3.
 *
 * Plots disposable income (gross - federal/NJ/FICA tax - PITI on the
 * county's median home) over time. Two visible series:
 *
 *   1. DI in nominal dollars         (faint blue line)
 *   2. DI in real {base_year} dollars (solid blue line, headline)
 *
 * The headline is the REAL series because it isolates the affordability
 * collapse from CPI inflation -- a flat line in real terms tells a very
 * different story than a flat line in nominal terms.
 *
 * Server-rendered SVG, zero client JS. Same architectural choice as
 * <CollapseCurve />.
 *
 * Why the area under the line is shaded
 * -------------------------------------
 * The chart's core message is "after housing and taxes, this is what
 * the median household has left." Filling the area under the line
 * makes that "what's left" quantity visually concrete -- a thin band
 * means little headroom, a thick band means lots of headroom.
 *
 * Empty/sparse-data behavior matches CollapseCurve: <2 plottable
 * points renders an explainer instead of a misleading near-empty chart.
 */

export interface DisposableIncomePoint {
  year: number;
  /** DI in nominal dollars. Plotted as the secondary (faint) line. */
  di_nominal: number | null;
  /** DI in real {base_year} dollars. The headline series. */
  di_real: number | null;
}

export interface DisposableIncomeChartProps {
  points: DisposableIncomePoint[];
  /** Year the di_real values are denominated in (e.g. 2024). */
  realDollarsBaseYear: number | null;
  width?: number;
  height?: number;
  title?: string;
}

const PADDING = { top: 24, right: 28, bottom: 40, left: 72 };

function roundUpTo(n: number, step: number): number {
  return Math.ceil(n / step) * step;
}

function pickTickStep(range: number): number {
  if (range <= 25_000) return 5_000;
  if (range <= 50_000) return 10_000;
  if (range <= 100_000) return 25_000;
  if (range <= 250_000) return 50_000;
  return 100_000;
}

function fmtK(dollars: number): string {
  if (dollars >= 1_000_000)
    return `$${(dollars / 1_000_000).toFixed(1)}M`;
  if (dollars >= 1_000) return `$${Math.round(dollars / 1_000)}K`;
  if (dollars <= -1_000) return `-$${Math.round(-dollars / 1_000)}K`;
  return `$${dollars.toFixed(0)}`;
}

export function DisposableIncomeChart({
  points,
  realDollarsBaseYear,
  width = 720,
  height = 320,
  title,
}: DisposableIncomeChartProps) {
  // Plot only points where the headline (di_real) is populated. Points
  // with nominal-only DI (e.g. CPI not yet seeded for that year) are
  // omitted -- substrate honesty over visual continuity.
  const plotted = points.filter((p) => p.di_real != null);

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
          fontSize="13"
          fill="currentColor"
        >
          Disposable income trajectory needs at least 2 years with all
          four substrates (DCA + ACS + IRS/NJ tax + CPI).
        </text>
      </svg>
    );
  }

  const xs = plotted.map((p) => p.year);
  const allYs = plotted
    .flatMap((p) => [p.di_real!, p.di_nominal])
    .filter((y): y is number => y != null);

  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMinRaw = Math.min(...allYs);
  const yMaxRaw = Math.max(...allYs);
  const range = Math.max(yMaxRaw - yMinRaw, 1);
  const tickStep = pickTickStep(yMaxRaw - Math.min(0, yMinRaw));

  // We anchor the y-axis at $0 when DI is non-negative, so the area-
  // under-line shading reads as "headroom above zero." For counties
  // where DI dips below zero (housing+tax > income, which would
  // indicate a true crisis) we extend the axis below zero.
  const yMax = roundUpTo(yMaxRaw + range * 0.05, tickStep);
  const yMin =
    yMinRaw < 0 ? Math.floor(yMinRaw / tickStep) * tickStep : 0;

  const innerW = width - PADDING.left - PADDING.right;
  const innerH = height - PADDING.top - PADDING.bottom;

  const sx = (year: number) =>
    PADDING.left +
    ((year - xMin) / Math.max(xMax - xMin, 1)) * innerW;
  const sy = (val: number) =>
    PADDING.top +
    innerH -
    ((val - yMin) / Math.max(yMax - yMin, 1)) * innerH;

  const realPath =
    "M " +
    plotted
      .map((p) => `${sx(p.year).toFixed(2)},${sy(p.di_real!).toFixed(2)}`)
      .join(" L ");

  const nominalPath =
    "M " +
    plotted
      .filter((p) => p.di_nominal != null)
      .map(
        (p) => `${sx(p.year).toFixed(2)},${sy(p.di_nominal!).toFixed(2)}`,
      )
      .join(" L ");

  // Filled area under the real DI line: trace the line forward, then
  // along the x-axis (y=yMin -- $0 if non-negative) backwards.
  const baselineY = sy(Math.max(yMin, 0));
  const areaPath =
    "M " +
    plotted
      .map((p) => `${sx(p.year).toFixed(2)},${sy(p.di_real!).toFixed(2)}`)
      .join(" L ") +
    ` L ${sx(plotted[plotted.length - 1].year).toFixed(2)},${baselineY.toFixed(2)}` +
    ` L ${sx(plotted[0].year).toFixed(2)},${baselineY.toFixed(2)}` +
    " Z";

  const tickValues: number[] = [];
  for (let v = yMin; v <= yMax + 1; v += tickStep) tickValues.push(v);

  const labelEvery = plotted.length <= 8 ? 1 : 2;

  // Detect direction: is real DI eroding (idea §5.3 the spec's
  // headline question)? Compare first half to second half.
  const half = Math.floor(plotted.length / 2);
  const firstHalfAvg =
    plotted.slice(0, half).reduce((s, p) => s + (p.di_real ?? 0), 0) /
    Math.max(half, 1);
  const secondHalfAvg =
    plotted.slice(half).reduce((s, p) => s + (p.di_real ?? 0), 0) /
    (plotted.length - half);
  const eroding = secondHalfAvg < firstHalfAvg;

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      role="img"
      className="text-zinc-700 dark:text-zinc-300"
    >
      {title ? <title>{title}</title> : null}

      {tickValues.map((v) => (
        <g key={`yt-${v}`}>
          <line
            x1={PADDING.left}
            x2={width - PADDING.right}
            y1={sy(v)}
            y2={sy(v)}
            stroke="currentColor"
            strokeWidth={0.5}
            opacity={v === 0 ? 0.4 : 0.12}
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

      <line
        x1={PADDING.left}
        x2={width - PADDING.right}
        y1={PADDING.top + innerH}
        y2={PADDING.top + innerH}
        stroke="currentColor"
        strokeWidth={0.8}
        opacity={0.4}
      />

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

      {/* Area under line -- "what's left over" visual */}
      <path
        d={areaPath}
        fill={eroding ? "#dc262615" : "#2563eb15"}
        stroke="none"
      />

      {/* Nominal DI -- secondary, faint */}
      {plotted.some((p) => p.di_nominal != null) ? (
        <path
          d={nominalPath}
          fill="none"
          stroke="#94a3b8"
          strokeWidth={1.5}
          strokeDasharray="4 3"
          strokeLinejoin="round"
        />
      ) : null}

      {/* Real DI -- the headline series */}
      <path
        d={realPath}
        fill="none"
        stroke="#2563eb"
        strokeWidth={2.4}
        strokeLinejoin="round"
        strokeLinecap="round"
      />

      {plotted.map((p) => (
        <g key={`pt-${p.year}`}>
          <circle
            cx={sx(p.year)}
            cy={sy(p.di_real!)}
            r={2.8}
            fill="#2563eb"
          />
          <rect
            x={sx(p.year) - 12}
            y={PADDING.top}
            width={24}
            height={innerH}
            fill="transparent"
          >
            <title>
              {`${p.year}: real DI ${fmtK(p.di_real!)}` +
                (p.di_nominal != null
                  ? ` (nominal ${fmtK(p.di_nominal)})`
                  : "")}
            </title>
          </rect>
        </g>
      ))}

      <g transform={`translate(${PADDING.left}, ${PADDING.top - 8})`}>
        <rect x={0} y={-9} width={10} height={3} fill="#2563eb" />
        <text
          x={14}
          y={-7}
          fontSize="11"
          fill="currentColor"
          opacity={0.85}
        >
          {`DI in ${realDollarsBaseYear ?? "?"} dollars (real)`}
        </text>
        <rect x={210} y={-9} width={10} height={3} fill="#94a3b8" />
        <text
          x={224}
          y={-7}
          fontSize="11"
          fill="currentColor"
          opacity={0.85}
        >
          DI nominal
        </text>
      </g>
    </svg>
  );
}
