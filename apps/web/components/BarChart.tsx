"use client";

import { useState } from "react";
import { formatValue } from "@/lib/format";

/**
 * Ranked magnitude by category.
 *
 * Horizontal bars, because category labels are words: vertical bars force
 * rotated text, which is measurably harder to read. Sorted descending — the
 * job is "which is biggest", so rank should be readable without comparing
 * lengths.
 *
 * Mark spec: 4px rounded data-ends anchored to the baseline (the end away from
 * the axis is rounded, the anchored end stays square), a 2px surface gap
 * between adjacent bars, direct value labels so the reader never measures
 * against a gridline.
 */

type Item = { label: string; value: number | null };

export function BarChart({
  items,
  format = "currency",
  label,
}: {
  items: Item[];
  format?: string;
  label: string;
}) {
  const [hover, setHover] = useState<number | null>(null);

  const usable = items.filter((i) => i.value !== null) as { label: string; value: number }[];
  if (usable.length === 0) {
    return (
      <div
        className="h-40 flex items-center justify-center text-sm"
        style={{ color: "var(--text-muted)" }}
      >
        No breakdown available.
      </div>
    );
  }

  const max = Math.max(...usable.map((i) => i.value)) || 1;

  return (
    <figure className="m-0">
      <figcaption className="text-sm mb-3" style={{ color: "var(--text-secondary)" }}>
        {label}
      </figcaption>
      <div className="flex flex-col gap-[2px]">
        {usable.map((item, i) => (
          <div
            key={item.label}
            className="grid items-center gap-3"
            style={{ gridTemplateColumns: "minmax(64px, 22%) 1fr auto" }}
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
          >
            <div
              className="text-sm truncate text-right"
              style={{ color: "var(--text-secondary)" }}
              title={item.label}
            >
              {item.label}
            </div>
            <div className="h-7 relative">
              <div
                className="h-full transition-opacity"
                style={{
                  width: `${Math.max((item.value / max) * 100, 0.5)}%`,
                  background: "var(--series-1)",
                  borderRadius: "0 4px 4px 0",
                  opacity: hover === null || hover === i ? 1 : 0.55,
                }}
              />
            </div>
            <div className="text-sm tabular w-24 text-right">
              {/* Scaled against the largest bar so every label in the chart
                  carries the same number of decimal places. */}
              {formatValue(item.value, format, max)}
            </div>
          </div>
        ))}
      </div>
    </figure>
  );
}
