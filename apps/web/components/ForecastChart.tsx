"use client";

import { useState } from "react";
import type { Anomalies, Forecast } from "@/lib/api";
import { compact, formatPeriod, formatValue } from "@/lib/format";

/**
 * History and projection on one continuous axis.
 *
 * A forecast is the same series continued, so it shares the chart. Splitting it
 * into a second card would make the reader compare two scales across a gap for
 * no reason.
 *
 * Three encodings distinguish the halves, because one is never enough:
 *   - solid vs dashed line (survives greyscale and colorblindness)
 *   - a shaded interval band, which only the forecast has
 *   - a labelled divider at the last actual period
 *
 * The band is the point of the chart. A bare projected line implies a precision
 * no forecast has, and the width of the interval is usually the most
 * decision-relevant thing on screen.
 */

type Props = {
  forecast: Forecast;
  anomalies?: Anomalies | null;
  format?: string;
};

/** Shared with the callout list, so one anomaly never wears two colours. */
export const ANOMALY_COLOR: Record<string, string> = {
  warning: "var(--status-warning)",
  serious: "var(--status-serious)",
};

export function ForecastChart({ forecast, anomalies, format = "currency" }: Props) {
  const [hover, setHover] = useState<number | null>(null);

  const history = forecast.history
    .filter((p) => p.value !== null)
    .map((p) => ({ period: p.period, value: p.value as number, kind: "actual" as const }));

  const projected = forecast.points.map((p) => ({
    period: p.period,
    value: p.value,
    lower: p.lower,
    upper: p.upper,
    kind: "forecast" as const,
  }));

  if (history.length === 0) return null;

  // The forecast path starts at the last actual point so the two halves join
  // rather than floating apart with a visual gap.
  const series = [...history, ...projected];
  const anomalyByPeriod = new Map(
    (anomalies?.anomalies ?? []).map((a) => [a.period.slice(0, 10), a]),
  );

  const width = 760;
  const height = 300;
  const pad = { top: 20, right: 24, bottom: 40, left: 60 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;

  const values = [
    ...series.map((p) => p.value),
    ...projected.map((p) => p.upper),
    ...projected.map((p) => p.lower),
  ];
  const max = Math.max(...values);
  const min = 0; // anchored: a truncated axis exaggerates the trend
  const span = max - min || 1;

  const x = (i: number) => pad.left + (i / Math.max(series.length - 1, 1)) * plotWidth;
  const y = (v: number) => pad.top + plotHeight - ((v - min) / span) * plotHeight;

  const historyPath = history
    .map((p, i) => `${i === 0 ? "M" : "L"} ${x(i)} ${y(p.value)}`)
    .join(" ");

  const joinIndex = history.length - 1;
  const forecastPath = [
    `M ${x(joinIndex)} ${y(history[joinIndex].value)}`,
    ...projected.map((p, i) => `L ${x(history.length + i)} ${y(p.value)}`),
  ].join(" ");

  // Band polygon: upper edge forward, lower edge back.
  const bandPoints = [
    `${x(joinIndex)},${y(history[joinIndex].value)}`,
    ...projected.map((p, i) => `${x(history.length + i)},${y(p.upper)}`),
    ...projected
      .map((p, i) => `${x(history.length + i)},${y(p.lower)}`)
      .reverse(),
  ].join(" ");

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => min + t * span);
  const hovered = hover === null ? null : series[hover];

  return (
    <figure className="m-0">
      <div className="flex items-baseline justify-between mb-1 flex-wrap gap-2">
        <figcaption className="text-sm" style={{ color: "var(--text-secondary)" }}>
          {forecast.measure} by {forecast.grain}, with {forecast.points.length} projected
        </figcaption>
        {/* Two series, so a legend is required — identity must never rest on
            color alone. */}
        <div className="flex gap-4 text-xs" style={{ color: "var(--text-secondary)" }}>
          <span className="flex items-center gap-1.5">
            <svg width="20" height="8" aria-hidden="true">
              <line x1="0" y1="4" x2="20" y2="4" stroke="var(--series-1)" strokeWidth="2" />
            </svg>
            Actual
          </span>
          <span className="flex items-center gap-1.5">
            <svg width="20" height="8" aria-hidden="true">
              <line
                x1="0"
                y1="4"
                x2="20"
                y2="4"
                stroke="var(--series-2)"
                strokeWidth="2"
                strokeDasharray="4 3"
              />
            </svg>
            Forecast ({Math.round(forecast.confidence * 100)}% range)
          </span>
        </div>
      </div>

      <div className="relative">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          className="w-full h-auto"
          role="img"
          aria-label={`${forecast.measure} history and forecast`}
          onMouseLeave={() => setHover(null)}
        >
          {ticks.map((tick, i) => (
            <g key={i}>
              <line
                x1={pad.left}
                x2={width - pad.right}
                y1={y(tick)}
                y2={y(tick)}
                stroke="var(--gridline)"
                strokeWidth={1}
              />
              <text
                x={pad.left - 8}
                y={y(tick) + 4}
                textAnchor="end"
                fontSize={11}
                fill="var(--text-muted)"
                className="tabular"
              >
                {compact(tick)}
              </text>
            </g>
          ))}

          <polygon points={bandPoints} fill="var(--series-2)" opacity={0.15} />

          <path d={historyPath} fill="none" stroke="var(--series-1)" strokeWidth={2}
                strokeLinecap="round" strokeLinejoin="round" data-testid="history-series" />
          <path d={forecastPath} fill="none" stroke="var(--series-2)" strokeWidth={2}
                strokeDasharray="5 4" strokeLinecap="round" data-testid="forecast-series" />

          {/* Where measurement ends and projection begins. */}
          <line
            x1={x(joinIndex)}
            x2={x(joinIndex)}
            y1={pad.top}
            y2={pad.top + plotHeight}
            stroke="var(--axis)"
            strokeWidth={1}
            strokeDasharray="2 3"
          />
          <text
            x={x(joinIndex) + 4}
            y={pad.top + 10}
            fontSize={10}
            fill="var(--text-muted)"
          >
            now
          </text>

          {history.map((p, i) => {
            const anomaly = anomalyByPeriod.get(p.period.slice(0, 10));
            return (
              <g key={`h-${i}`}>
                {anomaly && (
                  // Ringed, and also written out below the chart — a colored
                  // ring alone carries no meaning in print or for a colorblind
                  // reader.
                  <circle
                    cx={x(i)}
                    cy={y(p.value)}
                    r={8}
                    fill="none"
                    // Same mapping the callout list uses. Two colours for one
                    // finding — a red ring against an orange icon — reads as
                    // two separate problems.
                    stroke={ANOMALY_COLOR[anomaly.severity]}
                    strokeWidth={2}
                  />
                )}
                <circle
                  cx={x(i)}
                  cy={y(p.value)}
                  r={hover === i ? 5 : 3.5}
                  fill="var(--series-1)"
                  stroke="var(--surface)"
                  strokeWidth={2}
                />
              </g>
            );
          })}

          {projected.map((p, i) => (
            <circle
              key={`f-${i}`}
              cx={x(history.length + i)}
              cy={y(p.value)}
              r={hover === history.length + i ? 5 : 3.5}
              fill="var(--surface)"
              stroke="var(--series-2)"
              strokeWidth={2}
            />
          ))}

          {series.map((p, i) => (
            <text
              key={`t-${i}`}
              x={x(i)}
              y={height - 14}
              textAnchor="middle"
              fontSize={11}
              fill="var(--text-muted)"
            >
              {series.length <= 10 || i % Math.ceil(series.length / 10) === 0
                ? formatPeriod(p.period, forecast.grain)
                : ""}
            </text>
          ))}

          {series.map((_, i) => (
            <rect
              key={`hit-${i}`}
              x={x(i) - plotWidth / (series.length * 2)}
              y={pad.top}
              width={plotWidth / series.length}
              height={plotHeight}
              fill="transparent"
              onMouseEnter={() => setHover(i)}
            />
          ))}
        </svg>

        {hovered && (
          <div
            className="absolute pointer-events-none card px-3 py-2 text-sm shadow-lg"
            style={{
              left: `${(x(hover as number) / width) * 100}%`,
              top: 0,
              transform: "translate(-50%, -8px)",
            }}
          >
            <div style={{ color: "var(--text-muted)" }}>
              {formatPeriod(hovered.period, forecast.grain)}
              {hovered.kind === "forecast" && " · projected"}
            </div>
            <div className="font-semibold tabular">
              {formatValue(hovered.value, format)}
            </div>
            {hovered.kind === "forecast" && (
              <div className="text-xs tabular" style={{ color: "var(--text-muted)" }}>
                {formatValue(hovered.lower, format)} – {formatValue(hovered.upper, format)}
              </div>
            )}
          </div>
        )}
      </div>

      <p className="text-xs mt-2" style={{ color: "var(--text-muted)" }}>
        {forecast.rationale}
      </p>
    </figure>
  );
}
