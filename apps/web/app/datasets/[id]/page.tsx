"use client";

import { use, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  RequestError,
  type Anomalies,
  type Breakdown,
  type Churn,
  type DatasetDetail,
  type Forecast,
  type Kpi,
  type Preview,
  type QualityReport,
  type Timeseries,
} from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";
import { AppShell } from "@/components/AppShell";
import { BarChart } from "@/components/BarChart";
import { Chat } from "@/components/Chat";
import { ChurnPanel } from "@/components/ChurnPanel";
import { ForecastChart } from "@/components/ForecastChart";
import { LineChart } from "@/components/LineChart";
import { StatTile } from "@/components/StatTile";
import { Tabs } from "@/components/Tabs";
import { formatCell } from "@/lib/format";

const SEVERITY_COLOR: Record<string, string> = {
  error: "var(--status-critical)",
  warning: "var(--status-warning)",
  serious: "var(--status-serious)",
  info: "var(--text-muted)",
};

const SEVERITY_ICON: Record<string, string> = {
  error: "✕",
  warning: "!",
  serious: "!",
  info: "i",
};

export default function DatasetPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { me, loading } = useRequireAuth();

  const [tab, setTab] = useState("overview");
  const [dataset, setDataset] = useState<DatasetDetail | null>(null);
  const [kpis, setKpis] = useState<Kpi[]>([]);
  const [quality, setQuality] = useState<QualityReport | null>(null);
  const [series, setSeries] = useState<Timeseries | null>(null);
  const [breakdown, setBreakdown] = useState<Breakdown | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [forecast, setForecast] = useState<Forecast | null>(null);
  const [forecastError, setForecastError] = useState<string | null>(null);
  const [anomalies, setAnomalies] = useState<Anomalies | null>(null);
  const [churn, setChurn] = useState<Churn | null>(null);
  const [churnError, setChurnError] = useState<string | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [summaryBusy, setSummaryBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const detail = await api.dataset(id);
      setDataset(detail);
      if (detail.status !== "ready" || !detail.cleaned_at) return;

      // Not swallowed: an empty dashboard that looks "loaded" is worse than an
      // error message.
      setKpis(await api.kpis(id));

      // `caps` gates the prediction fetches below rather than being stored:
      // nothing renders from it directly, so keeping it in state would only
      // add a render.
      const [q, p, caps] = await Promise.all([
        api.quality(id).catch(() => null),
        api.preview(id).catch(() => null),
        api.capabilities(id).catch(() => null),
      ]);
      setQuality(q);
      setPreview(p);

      // Absence is expected for these — a dataset with no date column has no
      // time series — so their failure is not an error.
      setSeries(await api.timeseries(id).catch(() => null));
      setBreakdown(await api.breakdown(id).catch(() => null));

      if (caps?.forecast) {
        // A refusal ("too few periods") is a real answer worth showing, so the
        // message is kept rather than discarded.
        await api
          .forecast(id)
          .then(setForecast)
          .catch((err) =>
            setForecastError(
              err instanceof RequestError ? err.error.message : "Forecast unavailable.",
            ),
          );
        setAnomalies(await api.anomalies(id).catch(() => null));
      }
      if (caps?.churn) {
        await api
          .churn(id)
          .then(setChurn)
          .catch((err) =>
            setChurnError(
              err instanceof RequestError ? err.error.message : "Churn unavailable.",
            ),
          );
      }
    } catch (err) {
      if (err instanceof RequestError && err.status !== 401) setError(err.error.message);
    }
  }, [id]);

  // `load` sets state only after awaiting the network, so this is not the
  // cascading synchronous render the rule guards against — the analysis just
  // cannot see through the useCallback into the await. Fetch-on-mount is the
  // documented pattern without a data layer; adopting TanStack Query would
  // remove the effect altogether and is the real fix.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (me) void load();
  }, [me, load]);

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
      setSummary(
        err instanceof RequestError ? err.error.message : "Could not generate summary.",
      );
    } finally {
      setSummaryBusy(false);
    }
  }

  if (loading || !me) {
    return (
      <div
        className="min-h-screen grid place-items-center text-sm"
        style={{ color: "var(--text-muted)" }}
      >
        Loading…
      </div>
    );
  }

  const anomalyCount = anomalies?.anomalies.length ?? 0;
  const predictionCount =
    (forecast ? 1 : 0) + anomalyCount + (churn ? 1 : 0);

  // Counts on the labels: a tab's only cost is that it hides things, so each
  // one advertises what is behind it.
  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "predictions", label: "Predictions", count: predictionCount || null },
    { id: "data", label: "Data" },
    { id: "quality", label: "Quality", count: quality?.issues.length || null },
  ];

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
          <div className="flex items-end justify-between mt-3 mb-6 gap-4 flex-wrap">
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
              <Tabs tabs={tabs} active={tab} onChange={setTab} />

              {tab === "overview" && (
                <div role="tabpanel">
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

                  <Chat datasetId={id} />
                </div>
              )}

              {tab === "predictions" && (
                <div role="tabpanel" className="flex flex-col gap-6">
                  {forecast && (
                    <section className="card p-5">
                      <h2 className="font-semibold mb-3">Forecast</h2>
                      <ForecastChart
                        forecast={forecast}
                        anomalies={anomalies}
                        format={forecast.measure === "records" ? "number" : "currency"}
                      />
                    </section>
                  )}

                  {forecastError && (
                    <section className="card p-5">
                      <h2 className="font-semibold mb-1">Forecast</h2>
                      <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
                        {forecastError}
                      </p>
                    </section>
                  )}

                  {anomalyCount > 0 && (
                    <section className="card p-5" data-testid="anomaly-list">
                      <h2 className="font-semibold mb-1">Unusual periods</h2>
                      <p className="text-sm mb-3" style={{ color: "var(--text-secondary)" }}>
                        Measured against the trend, not against size — a record month
                        in a growing business is not an anomaly.
                      </p>
                      <ul className="flex flex-col gap-2">
                        {anomalies?.anomalies.map((item) => (
                          <li key={item.period} className="flex gap-3 text-sm">
                            <span
                              aria-hidden="true"
                              className="w-5 h-5 rounded-full grid place-items-center text-xs shrink-0 mt-[1px]"
                              style={{
                                background: SEVERITY_COLOR[item.severity],
                                color: item.severity === "warning" ? "#0b0b0b" : "#fff",
                              }}
                            >
                              {SEVERITY_ICON[item.severity]}
                            </span>
                            <span>
                              <span className="sr-only">{item.severity}: </span>
                              {item.message}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </section>
                  )}

                  {churn && <ChurnPanel churn={churn} />}

                  {churnError && (
                    <section className="card p-5">
                      <h2 className="font-semibold mb-1">Customer retention</h2>
                      <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
                        {churnError}
                      </p>
                    </section>
                  )}

                  {!forecast && !forecastError && !churn && !churnError && (
                    <div className="card p-10 text-center">
                      <p className="font-medium mb-1">No predictions available</p>
                      <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
                        Forecasting needs a date column; churn additionally needs a
                        customer identifier. This dataset has neither.
                      </p>
                    </div>
                  )}
                </div>
              )}

              {tab === "data" && (
                <div role="tabpanel" className="flex flex-col gap-6">
                  <section className="card p-5">
                    <h2 className="font-semibold mb-3">Columns</h2>
                    <div className="overflow-x-auto">
                      <table className="text-sm w-full">
                        <thead>
                          <tr style={{ borderBottom: "1px solid var(--border)" }}>
                            {["Column", "Type", "Original name", "Nullable"].map((h) => (
                              <th
                                key={h}
                                className="text-left px-3 py-2 font-medium whitespace-nowrap"
                                style={{ color: "var(--text-muted)" }}
                              >
                                {h}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {dataset.columns.map((column) => (
                            <tr
                              key={column.id}
                              style={{ borderBottom: "1px solid var(--border)" }}
                            >
                              <td className="px-3 py-2">{column.normalized_name}</td>
                              <td className="px-3 py-2">
                                {column.effective_type}
                                {column.cleaned_type && (
                                  <span style={{ color: "var(--text-muted)" }}>
                                    {" "}
                                    (was {column.data_type})
                                  </span>
                                )}
                              </td>
                              <td className="px-3 py-2" style={{ color: "var(--text-muted)" }}>
                                {column.name}
                              </td>
                              <td className="px-3 py-2">{column.nullable ? "yes" : "no"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </section>

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
                                  <td
                                    key={column}
                                    className="px-3 py-2 tabular whitespace-nowrap"
                                  >
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
                </div>
              )}

              {tab === "quality" && (
                <div role="tabpanel" className="flex flex-col gap-6">
                  <section className="card p-5">
                    <h2 className="font-semibold mb-3">Data quality</h2>
                    {quality && quality.issues.length > 0 ? (
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
                    ) : (
                      <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
                        No quality issues found.
                      </p>
                    )}

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
                              {action.reason
                                ? ` — ${action.reason}`
                                : ` — ${action.actions.join(", ")}`}
                            </li>
                          ))}
                        </ul>
                      </>
                    )}
                  </section>
                </div>
              )}
            </>
          )}
        </>
      )}
    </AppShell>
  );
}
