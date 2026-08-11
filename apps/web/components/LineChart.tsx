"use client";

import { useState } from "react";
import { compact, formatPeriod, formatValue } from "@/lib/format";

/**
 * Time series — one measure over time.
 *
 * A line, because the job is change-over-time on a continuous axis. Single
 * series, so no legend: the title names it, and a legend box for one thing is
 * noise.
 *
 * Mark spec: 2px stroke, ≥8px hover marker, recessive hairline grid, crosshair
 * plus tooltip on hover (an SVG chart in a browser is interactive by default —
 * the hover layer is not optional).
 */

type Point = { period: string; value: number | null };

export function LineChart({
  points,
  grain,
  format = "currency",
  label,
}: {
  points: Point[];
  grain: string;
  format?: string;
  label: string;
}) {
  const [hover, setHover] = useState<number | null>(null);

  const usable = points.filter((p) => p.value !== null) as { period: string; value: number }[];
  if (usable.length === 0) {
    return <Empty message="No time series data available." />;
  }

  const width = 720;
  const height = 260;
  const pad = { top: 16, right: 20, bottom: 32, left: 56 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;

  const values = usable.map((p) => p.value);
  const max = Math.max(...values);
  const min = Math.min(0, ...values); // anchor to zero — a truncated axis exaggerates change
  const span = max - min || 1;

  const x = (i: number) =>
    pad.left + (usable.length === 1 ? plotWidth / 2 : (i / (usable.length - 1)) * plotWidth);
  const y = (v: number) => pad.top + plotHeight - ((v - min) / span) * plotHeight;

  const path = usable.map((p, i) => `${i === 0 ? "M" : "L"} ${x(i)} ${y(p.value)}`).join(" ");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => min + t * span);

  return (
    <figure className="m-0">
      <figcaption className="text-sm mb-2" style={{ color: "var(--text-secondary)" }}>
        {label}
      </figcaption>
      <div className="relative">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          className="w-full h-auto"
          role="img"
          aria-label={`${label} over time`}
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

          <line
            x1={pad.left}
            x2={width - pad.right}
            y1={y(min)}
            y2={y(min)}
            stroke="var(--axis)"
            strokeWidth={1}
          />

          <path
            data-testid="line-series"
            d={path}
            fill="none"
            stroke="var(--series-1)"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          />

          {usable.map((p, i) => (
            <circle
              key={i}
              cx={x(i)}
              cy={y(p.value)}
              r={hover === i ? 5 : 3.5}
              fill="var(--series-1)"
              stroke="var(--surface)"
              strokeWidth={2}
            />
          ))}

          {hover !== null && (
            <line
              x1={x(hover)}
              x2={x(hover)}
              y1={pad.top}
              y2={pad.top + plotHeight}
              stroke="var(--axis)"
              strokeWidth={1}
              strokeDasharray="3 3"
            />
          )}

          {usable.map((p, i) => (
            <text
              key={`t-${i}`}
              x={x(i)}
              y={height - 10}
              textAnchor="middle"
              fontSize={11}
              fill="var(--text-muted)"
            >
              {/* Thin out labels so they cannot collide on a dense axis. */}
              {usable.length <= 8 || i % Math.ceil(usable.length / 8) === 0
                ? formatPeriod(p.period, grain)
                : ""}
            </text>
          ))}

          {/* Hit targets deliberately wider than the marks. */}
          {usable.map((p, i) => (
            <rect
              key={`h-${i}`}
              x={x(i) - plotWidth / (usable.length * 2 || 1) - 6}
              y={pad.top}
              width={plotWidth / (usable.length || 1) + 12}
              height={plotHeight}
              fill="transparent"
              onMouseEnter={() => setHover(i)}
            />
          ))}
        </svg>

        {hover !== null && (
          <div
            className="absolute pointer-events-none card px-3 py-2 text-sm shadow-lg"
            style={{
              left: `${(x(hover) / width) * 100}%`,
              top: 0,
              transform: "translate(-50%, -8px)",
            }}
          >
            <div style={{ color: "var(--text-muted)" }}>
              {formatPeriod(usable[hover].period, grain)}
            </div>
            <div className="font-semibold tabular">
              {formatValue(usable[hover].value, format)}
            </div>
          </div>
        )}
      </div>
    </figure>
  );
}

function Empty({ message }: { message: string }) {
  return (
    <div
      className="h-40 flex items-center justify-center text-sm"
      style={{ color: "var(--text-muted)" }}
    >
      {message}
    </div>
  );
}
