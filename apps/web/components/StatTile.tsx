"use client";

import { useState } from "react";
import { formatValue } from "@/lib/format";
import type { Kpi } from "@/lib/api";

/**
 * A single headline figure.
 *
 * Deliberately not a chart: one number's job is to be read, and a plot around
 * it adds ink without adding information.
 *
 * The delta is never color-alone — it carries an arrow glyph and the words
 * "vs previous", so the meaning survives for a colorblind reader, in print, and
 * under forced-colors.
 */
export function StatTile({ kpi }: { kpi: Kpi }) {
  const [showSql, setShowSql] = useState(false);
  const growth = kpi.details?.growth;
  const change = growth?.change ?? null;

  // "Higher is better" is not universal — for a cost metric a rise is bad.
  const isGood = change === null ? null : kpi.higher_is_better ? change >= 0 : change < 0;

  return (
    <div className="card p-4 flex flex-col gap-1" data-testid="kpi-tile" data-kpi={kpi.key}>
      <div className="text-sm" style={{ color: "var(--text-secondary)" }}>
        {kpi.label}
      </div>
      <div className="text-2xl font-semibold">{formatValue(kpi.value, kpi.format)}</div>

      {change !== null && (
        <div
          className="text-sm flex items-center gap-1"
          style={{ color: isGood ? "var(--delta-good)" : "var(--status-critical)" }}
        >
          <span aria-hidden="true">{change >= 0 ? "▲" : "▼"}</span>
          <span className="tabular">
            {change >= 0 ? "+" : ""}
            {(change * 100).toFixed(1)}%
          </span>
          <span style={{ color: "var(--text-muted)" }}>vs previous</span>
        </div>
      )}

      <button
        onClick={() => setShowSql((v) => !v)}
        className="text-xs text-left mt-1 underline underline-offset-2"
        style={{ color: "var(--text-muted)" }}
      >
        {showSql ? "Hide" : "How is this calculated?"}
      </button>

      {showSql && (
        <pre
          className="text-xs mt-1 p-2 rounded overflow-x-auto"
          style={{ background: "var(--page)", color: "var(--text-secondary)" }}
        >
          {kpi.sql}
        </pre>
      )}
    </div>
  );
}
