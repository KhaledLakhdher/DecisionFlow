"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, RequestError, type Dataset } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";
import { AppShell } from "@/components/AppShell";
import { formatBytes } from "@/lib/format";

const STATUS_LABEL: Record<Dataset["status"], string> = {
  uploaded: "Queued",
  analyzing: "Processing",
  ready: "Ready",
  failed: "Failed",
};

const STATUS_COLOR: Record<Dataset["status"], string> = {
  uploaded: "var(--text-muted)",
  analyzing: "var(--status-warning)",
  ready: "var(--status-good)",
  failed: "var(--status-critical)",
};

export default function DatasetsPage() {
  const { me, loading } = useRequireAuth();
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      setDatasets(await api.datasets());
    } catch (err) {
      if (err instanceof RequestError && err.status !== 401) setError(err.error.message);
    }
  }, []);

  // `refresh` sets state only after awaiting the network. See the dataset page
  // for the fuller note on why the rule misreads this.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (me) void refresh();
  }, [me, refresh]);

  // Ingestion runs in a worker, so the list has to be polled to see a dataset
  // become ready. Only poll while something is actually in flight — a fixed
  // interval would keep hitting the API forever on an idle dashboard.
  useEffect(() => {
    const pending = datasets.some((d) => d.status === "uploaded" || d.status === "analyzing");
    if (!pending) return;
    const timer = setInterval(refresh, 2000);
    return () => clearInterval(timer);
  }, [datasets, refresh]);

  async function upload(file: File) {
    setError(null);
    setUploading(true);
    try {
      await api.upload(file);
      await refresh();
    } catch (err) {
      setError(err instanceof RequestError ? err.error.message : "Upload failed.");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
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
      <div className="flex items-end justify-between mb-6">
        <div>
          <h1 className="text-xl font-semibold">Datasets</h1>
          <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
            Upload a CSV or Excel file. Cleaning and analysis run automatically.
          </p>
        </div>
        <div>
          <input
            ref={fileInput}
            type="file"
            accept=".csv,.tsv,.xlsx,.xls"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void upload(file);
            }}
          />
          <button
            className="btn btn-primary"
            disabled={uploading}
            onClick={() => fileInput.current?.click()}
          >
            {uploading ? "Uploading…" : "Upload data"}
          </button>
        </div>
      </div>

      {error && (
        <p
          role="alert"
          className="text-sm px-3 py-2 rounded mb-4"
          style={{ background: "var(--surface)", color: "var(--status-critical)" }}
        >
          {error}
        </p>
      )}

      {datasets.length === 0 ? (
        <div className="card p-10 text-center">
          <p className="font-medium mb-1">No datasets yet</p>
          <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
            Upload sales.csv, orders.csv, or any spreadsheet to get started.
          </p>
        </div>
      ) : (
        <div className="card divide-y" style={{ borderColor: "var(--border)" }}>
          {datasets.map((dataset) => (
            <Link
              key={dataset.id}
              href={`/datasets/${dataset.id}`}
              data-testid="dataset-row"
              data-status={dataset.status}
              className="flex items-center justify-between px-5 py-4 hover:opacity-80"
            >
              <div className="min-w-0">
                <div className="font-medium truncate" data-testid="dataset-name">
                  {dataset.name}
                </div>
                <div className="text-sm truncate" style={{ color: "var(--text-muted)" }}>
                  {dataset.original_filename} · {formatBytes(dataset.size_bytes)}
                  {dataset.clean_row_count !== null &&
                    ` · ${dataset.clean_row_count.toLocaleString()} rows`}
                </div>
              </div>
              <div className="flex items-center gap-4 shrink-0">
                {dataset.quality_score !== null && (
                  <span className="text-sm tabular" style={{ color: "var(--text-secondary)" }}>
                    {dataset.quality_score}/100
                  </span>
                )}
                {/* Status is a dot plus a word — never color alone. */}
                <span className="text-sm flex items-center gap-2">
                  <span
                    aria-hidden="true"
                    className="inline-block w-2 h-2 rounded-full"
                    style={{ background: STATUS_COLOR[dataset.status] }}
                  />
                  {STATUS_LABEL[dataset.status]}
                </span>
              </div>
            </Link>
          ))}
        </div>
      )}
    </AppShell>
  );
}
