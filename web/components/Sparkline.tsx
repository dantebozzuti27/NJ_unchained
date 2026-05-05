/**
 * Server-rendered SVG sparkline.
 *
 * Why server-rendered SVG vs a chart library
 * ------------------------------------------
 * recharts/victory/d3 add 80-200KB of client JS per page just to draw
 * a 5-pixel-tall trend line. SVG paths render natively at zero JS cost
 * and can be sized via CSS. The sparkline conveys direction +
 * volatility at a glance, which is all a screener row needs.
 *
 * Layout: x = year (linearly spaced), y = value (auto-scaled to series
 * min/max with a 5% pad). Optional baseline (e.g. y=100 for "no
 * change vs base year") drawn as a faint horizontal reference line.
 *
 * Accessibility: an SVG <title> describes the series. For screen
 * readers we also expose the latest value via aria-label on the
 * outer wrapper (the parent component handles that, not us).
 */

export interface SparklinePoint {
  year: number;
  indexed: number;
}

export interface SparklineProps {
  points: SparklinePoint[];
  /** Pixel width / height. Aspect ratio drives the visual cadence. */
  width?: number;
  height?: number;
  /** Stroke color (Tailwind text-class consumed via currentColor). */
  className?: string;
  /** Optional reference y-value drawn as a faint dashed line. */
  baseline?: number;
  /** Title attribute for hover tooltip + screen readers. */
  title?: string;
  /** Whether to show min/max year labels under the chart. */
  showAxisYears?: boolean;
}

export function Sparkline({
  points,
  width = 160,
  height = 36,
  className = "text-zinc-700 dark:text-zinc-300",
  baseline,
  title,
  showAxisYears = false,
}: SparklineProps) {
  if (points.length < 2) {
    return (
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        className={className}
      >
        {title ? <title>{title}</title> : null}
        <text
          x={width / 2}
          y={height / 2}
          textAnchor="middle"
          dominantBaseline="middle"
          fontSize="10"
          fill="currentColor"
          opacity={0.4}
        >
          —
        </text>
      </svg>
    );
  }

  const xs = points.map((p) => p.year);
  const ys = points.map((p) => p.indexed);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMin = Math.min(...ys, baseline ?? Number.POSITIVE_INFINITY);
  const yMax = Math.max(...ys, baseline ?? Number.NEGATIVE_INFINITY);
  const yPad = (yMax - yMin) * 0.08 || 1;

  const PADDING = 2;
  const innerW = width - PADDING * 2;
  const innerH = height - PADDING * 2;

  const sx = (year: number) =>
    PADDING +
    ((year - xMin) / Math.max(xMax - xMin, 1)) * innerW;
  const sy = (val: number) =>
    PADDING +
    innerH -
    ((val - (yMin - yPad)) / ((yMax + yPad) - (yMin - yPad))) * innerH;

  const path =
    "M " +
    points
      .map((p) => `${sx(p.year).toFixed(2)},${sy(p.indexed).toFixed(2)}`)
      .join(" L ");

  const last = points[points.length - 1];

  return (
    <div className="inline-block">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        className={className}
        role="img"
      >
        {title ? <title>{title}</title> : null}
        {baseline != null && (
          <line
            x1={PADDING}
            x2={width - PADDING}
            y1={sy(baseline)}
            y2={sy(baseline)}
            stroke="currentColor"
            strokeWidth={0.5}
            strokeDasharray="2 2"
            opacity={0.35}
          />
        )}
        <path
          d={path}
          fill="none"
          stroke="currentColor"
          strokeWidth={1.4}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        <circle
          cx={sx(last.year)}
          cy={sy(last.indexed)}
          r={1.8}
          fill="currentColor"
        />
      </svg>
      {showAxisYears && (
        <div className="flex justify-between text-[10px] text-zinc-500 font-mono mt-0.5"
             style={{ width }}>
          <span>{xMin}</span>
          <span>{xMax}</span>
        </div>
      )}
    </div>
  );
}
