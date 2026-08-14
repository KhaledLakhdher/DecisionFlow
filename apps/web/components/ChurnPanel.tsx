"use client";

import type { Churn } from "@/lib/api";
import { formatValue } from "@/lib/format";

/**
 * Customer retention risk.
 *
 * The definition is shown, not buried. No upload says "this customer churned" —
 * the label is derived from a cutoff the system chose — so a reader who cannot
 * see that rule has no way to disagree with the number. Making it arguable is
 * the point.
 *
 * Risk bands carry a word as well as a color, and every listed customer carries
 * the reason they were flagged.
 */

const BAND_COLOR: Record<string, string> = {
  high: "var(--status-critical)",
  medium: "var(--status-warning)",
  low: "var(--text-muted)",
};

export function ChurnPanel({ churn }: { churn: Churn }) {
  return (
    <section className="card p-5" data-testid="churn-panel">
      <h2 className="font-semibold mb-1">Customer retention</h2>

      <div className="flex items-baseline gap-3 mb-2">
        <span className="text-2xl font-semibold">
          {(churn.lapsed_rate * 100).toFixed(0)}%
        </span>
        <span className="text-sm" style={{ color: "var(--text-secondary)" }}>
          {churn.lapsed} of {churn.customers} customers have lapsed
        </span>
      </div>

      <p
        className="text-xs mb-4 p-2 rounded"
        style={{ background: "var(--page)", color: "var(--text-secondary)" }}
      >
        {churn.definition}
      </p>

      {churn.drivers.length > 0 && (
        <>
          <h3 className="text-sm font-medium mb-2">What predicts lapsing</h3>
          <ul className="flex flex-col gap-1 mb-4 text-sm">
            {churn.drivers.map((driver) => (
              <li key={driver.feature} className="flex items-center gap-2">
                <span aria-hidden="true">{driver.coefficient > 0 ? "▲" : "▼"}</span>
                <span style={{ color: "var(--text-secondary)" }}>{driver.label}</span>
                <span style={{ color: "var(--text-muted)" }}>— {driver.direction}</span>
              </li>
            ))}
          </ul>
        </>
      )}

      {churn.at_risk.length > 0 && (
        <>
          <h3 className="text-sm font-medium mb-2">Most at risk</h3>
          <div className="overflow-x-auto">
            <table className="text-sm w-full">
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  {["Customer", "Risk", "Last seen", "Orders", "Spend"].map((h) => (
                    <th
                      key={h}
                      className="text-left px-2 py-1 font-medium whitespace-nowrap"
                      style={{ color: "var(--text-muted)" }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {churn.at_risk.slice(0, 8).map((item) => (
                  <tr key={item.customer} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td className="px-2 py-1.5">{item.customer}</td>
                    <td className="px-2 py-1.5">
                      {/* Band is a dot plus a word — never color alone. */}
                      <span className="flex items-center gap-2">
                        <span
                          aria-hidden="true"
                          className="inline-block w-2 h-2 rounded-full"
                          style={{ background: BAND_COLOR[item.band] }}
                        />
                        <span className="tabular">{(item.risk * 100).toFixed(0)}%</span>
                        <span style={{ color: "var(--text-muted)" }}>{item.band}</span>
                      </span>
                    </td>
                    <td className="px-2 py-1.5 tabular">{item.recency_days}d ago</td>
                    <td className="px-2 py-1.5 tabular">{item.frequency}</td>
                    <td className="px-2 py-1.5 tabular">
                      {formatValue(item.monetary, "currency")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {churn.accuracy !== null && (
        <p className="text-xs mt-3" style={{ color: "var(--text-muted)" }}>
          In-sample accuracy {(churn.accuracy * 100).toFixed(0)}% — reported for
          transparency, not as evidence the model generalises.
        </p>
      )}
    </section>
  );
}
