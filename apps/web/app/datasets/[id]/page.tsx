"use client";

import { use, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  RequestError,
  type Breakdown,
  type DatasetDetail,
  type Kpi,
  type Preview,
  type QualityReport,
  type Timeseries,
} from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";
import { AppShell } from "@/components/AppShell";
import { BarChart } from "@/components/BarChart";
import { Chat } from "@/components/Chat";
import { LineChart } from "@/components/LineChart";
import { StatTile } from "@/components/StatTile";
import { formatCell } from "@/lib/format";

const SEVERITY_COLOR: Record<string, string> = {
  error: "var(--status-critical)",
  warning: "var(--status-warning)",
  info: "var(--text-muted)",
};

// Icon plus label, so a status never depends on color alone.
const SEVERITY_ICON: Record<string, string> = {
  error: "✕",
  warning: "!",
  info: "i",
};

export default function DatasetPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { me, loading } = useRequireAuth();

  const [dataset, setDataset] = useState<DatasetDetail | null>(null);
  const [kpis, setKpis] = useState<Kpi[]>([]);
  const [quality, setQuality] = useState<QualityReport | null>(null);
  const [series, setSeries] = useState<Timeseries | null>(null);
  const [breakdown, setBreakdown] = useState<Breakdown | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [summaryBusy, setSummaryBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const detail = await api.dataset(id);
      setDataset(detail);

      if (detail.status !== "ready" || !detail.cleaned_at) return;

      // The KPI call is deliberately NOT swallowed. Catching everything here
      // rendered an empty dashboard whenever a request failed — the page
      // looked "loaded" with nothing on it, which is worse than an error.
      const kpiResult = await api.kpis(id);
      setKpis(kpiResult);

      // These three legitimately may not exist: a dataset with no date column
      // has no time series, and that must not blank out the KPIs beside it.
      // Their absence is expected, so it is not an error.
      const [q, p] = await Promise.all([
        api.quality(id).catch(() => null),
        api.preview(id).catch(() => null),
      ]);
      setQuality(q);
      setPreview(p);

      setSeries(await api.timeseries(id).catch(() => null));
      setBreakdown(await api.breakdown(id).catch(() => null));
    } catch (err) {
      if (err instanceof RequestError && err.status !== 401) setError(err.error.message);
    }
  }, [id]);

  useEffect(() => {
    if (me) void load();
  }, [me, load]);

  // Poll only while the worker is still processing.
  useEffect(() => {
    if (!dataset || dataset.status === "ready" || dataset.status === "failed") return;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [dataset, load]);

  async function generateSummary() {
    setSummaryBusy(true);
    try {
      setSummary((await api.summary(id)).summary);
    } catch (err) {
      setSummary(err instanceof RequestError ? err.error.message : "Could not generate summary.");
    } finally {
      setSummaryBusy(false);
    }
  }

  if (loading || !me) {
    return (
      <div className="min-h-screen grid place-items-center text-sm" style={{ color: "var(--text-muted)" }}>
        Loading…
      </div>
    );
  }

  return (
    <AppShell>
      <Link
        href="/"
        className="text-sm underline underline-offset-2"
        style={{ color: "var(--text-secondary)" }}
      >
        ← All datasets
      </Link>

      {error && (
        <p role="alert" className="text-sm mt-4" style={{ color: "var(--status-critical)" }}>
          {error}
        </p>
      )}

      {dataset && (
        <>
          <div className="flex items-end justify-between mt-3 mb-6">
            <div>
              <h1 className="text-xl font-semibold">{dataset.name}</h1>
              <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
                {dataset.clean_row_count?.toLocaleString() ?? "—"} rows ·{" "}
                {dataset.column_count ?? "—"} columns
                {dataset.quality_score !== null && ` · quality ${dataset.quality_score}/100`}
              </p>
            </div>
            {dataset.status === "ready" && (
              <button className="btn btn-ghost" onClick={generateSummary} disabled={summaryBusy}>
                {summaryBusy ? "Writing…" : "Executive summary"}
              </button>
            )}
          </div>

          {dataset.status !== "ready" && (
            <div className="card p-6 mb-6">
              <p className="font-medium">
                {dataset.status === "failed" ? "Processing failed" : "Processing…"}
              </p>
              <p className="text-sm mt-1" style={{ color: "var(--text-secondary)" }}>
                {dataset.status_message ??
                  "Cleaning and analysing your data. This page updates automatically."}
              </p>
            </div>
          )}

          {summary && (
            <div className="card p-5 mb-6">
              <h2 className="font-semibold mb-2">Executive summary</h2>
              <p className="text-sm whitespace-pre-wrap">{summary}</p>
            </div>
          )}

          {dataset.status === "ready" && (
            <>
              {kpis.length > 0 && (
                <section className="grid gap-3 mb-6 grid-cols-[repeat(auto-fill,minmax(200px,1fr))]">
                  {kpis.map((kpi) => (
                    <StatTile key={kpi.key} kpi={kpi} />
                  ))}
                </section>
              )}

              <div className="grid gap-6 lg:grid-cols-2 mb-6">
                {series && series.points.length > 0 && (
                  <div className="card p-5">
                    <LineChart
                      points={series.points}
                      grain={series.grain}
                      label={`${series.measure} by ${series.grain}`}
                      format={series.measure === "records" ? "number" : "currency"}
                    />
                  </div>
                )}

                {breakdown && breakdown.items.length > 0 && (
                  <div className="card p-5">
                    <BarChart
                      items={breakdown.items}
                      label={`${breakdown.measure} by ${breakdown.dimension}`}
                      format={breakdown.measure === "records" ? "number" : "currency"}
                    />
                  </div>
                )}
              </div>

              <div className="mb-6">
                <Chat datasetId={id} />
              </div>

              {quality && quality.issues.length > 0 && (
                <section className="card p-5 mb-6">
                  <h2 className="font-semibold mb-3">Data quality</h2>
                  <ul className="flex flex-col gap-2">
                    {quality.issues.map((issue) => (
                      <li key={issue.id} className="flex gap-3 text-sm">
                        <span
                          aria-hidden="true"
                          className="w-5 h-5 rounded-full grid place-items-center text-xs shrink-0 mt-[1px]"
                          style={{
                            background: SEVERITY_COLOR[issue.severity],
                            color: issue.severity === "warning" ? "#0b0b0b" : "#fff",
                          }}
                        >
                          {SEVERITY_ICON[issue.severity]}
                        </span>
                        <span>
                          <span className="sr-only">{issue.severity}: </span>
                          {issue.message}
                        </span>
                      </li>
                    ))}
                  </ul>

                  {dataset.cleaning_actions.length > 0 && (
                    <>
                      <h3 className="font-medium mt-5 mb-2 text-sm">What was cleaned</h3>
                      <ul
                        className="flex flex-col gap-1 text-sm"
                        style={{ color: "var(--text-secondary)" }}
                      >
                        {dataset.cleaning_actions.map((action, i) => (
                          <li key={i}>
                            <span className="font-medium">
                              {action.column ?? "whole table"}
                            </span>
                            {action.reason ? ` — ${action.reason}` : ` — ${action.actions.join(", ")}`}
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </section>
              )}

              {preview && preview.rows.length > 0 && (
                <section className="card p-5">
                  <h2 className="font-semibold mb-1">Cleaned data</h2>
                  <p className="text-sm mb-3" style={{ color: "var(--text-secondary)" }}>
                    First {preview.rows.length} of{" "}
                    {preview.total_rows?.toLocaleString() ?? "—"} rows.
                  </p>
                  <div className="overflow-x-auto">
                    <table className="text-sm w-full">
                      <thead>
                        <tr style={{ borderBottom: "1px solid var(--border)" }}>
                          {preview.columns.map((column) => (
                            <th
                              key={column}
                              className="text-left px-3 py-2 font-medium whitespace-nowrap"
                              style={{ color: "var(--text-muted)" }}
                            >
                              {column}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {preview.rows.map((row, i) => (
                          <tr key={i} style={{ borderBottom: "1px solid var(--border)" }}>
                            {preview.columns.map((column) => (
                              <td key={column} className="px-3 py-2 tabular whitespace-nowrap">
                                {formatCell(row[column])}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}
            </>
          )}
        </>
      )}
    </AppShell>
  );
}
