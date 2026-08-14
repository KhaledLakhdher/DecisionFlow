"use client";

import { useCallback, useEffect, useState } from "react";
import { api, RequestError, type DataModel } from "@/lib/api";
import { useRequireAuth } from "@/lib/auth";
import { AppShell } from "@/components/AppShell";
import { StarDiagram } from "@/components/StarDiagram";

const ROLE_COLOR: Record<string, string> = {
  fact: "var(--series-1)",
  dimension: "var(--series-3)",
  unknown: "var(--text-muted)",
};

export default function ModelPage() {
  const { me, loading } = useRequireAuth();
  const [model, setModel] = useState<DataModel | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setModel(await api.model());
    } catch (err) {
      if (err instanceof RequestError && err.status !== 401) setError(err.error.message);
    }
  }, []);

  // `load` sets state only after awaiting the network. See the dataset page
  // for the fuller note on why the rule misreads this.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (me) void load();
  }, [me, load]);

  async function detect() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.detectRelationships();
      await load();
      setNotice("Scan complete. Review each proposal below before confirming.");
    } catch (err) {
      setError(err instanceof RequestError ? err.error.message : "Detection failed.");
    } finally {
      setBusy(false);
    }
  }

  async function decide(id: string, confirmed: boolean) {
    setError(null);
    try {
      setModel((await api.decideRelationship(id, confirmed)).model);
    } catch (err) {
      setError(err instanceof RequestError ? err.error.message : "Could not save.");
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

  const proposed = model?.relationships.filter((r) => r.confirmed === null) ?? [];
  const confirmed = model?.relationships.filter((r) => r.confirmed === true) ?? [];
  const rejected = model?.relationships.filter((r) => r.confirmed === false) ?? [];

  return (
    <AppShell>
      <div className="flex items-end justify-between mb-6 gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-semibold">Data model</h1>
          <p className="text-sm max-w-2xl" style={{ color: "var(--text-secondary)" }}>
            Uploaded files carry no foreign keys, so relationships are detected from
            the data itself. Nothing is joined until you confirm it — a wrong join
            does not fail, it quietly inflates every total.
          </p>
        </div>
        <button className="btn btn-primary" onClick={detect} disabled={busy}>
          {busy ? "Scanning…" : "Detect relationships"}
        </button>
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
      {notice && (
        <p className="text-sm mb-4" style={{ color: "var(--text-secondary)" }}>
          {notice}
        </p>
      )}

      {model && model.tables.length > 0 && (
        <section className="mb-6">
          <h2 className="font-semibold mb-3">Tables</h2>
          <div className="flex flex-wrap gap-2">
            {model.tables.map((table) => (
              <span
                key={table.dataset_id}
                className="card px-3 py-2 text-sm flex items-center gap-2"
              >
                {/* Role is a dot plus the word, never color alone. */}
                <span
                  aria-hidden="true"
                  className="inline-block w-2 h-2 rounded-full"
                  style={{ background: ROLE_COLOR[table.role] }}
                />
                <span className="font-medium">{table.table}</span>
                <span style={{ color: "var(--text-muted)" }}>
                  {table.role} · {table.rows?.toLocaleString() ?? "—"} rows
                </span>
              </span>
            ))}
          </div>
        </section>
      )}

      {model && (
        <section className="mb-6">
          <h2 className="font-semibold mb-3">Star schema</h2>
          <StarDiagram model={model} />
        </section>
      )}

      {proposed.length > 0 && (
        <section className="mb-6">
          <h2 className="font-semibold mb-1">Proposed relationships</h2>
          <p className="text-sm mb-3" style={{ color: "var(--text-secondary)" }}>
            Detected from value overlap. Confirm only the ones that are real.
          </p>
          <div className="flex flex-col gap-2">
            {proposed.map((rel) => (
              <div
                key={rel.id}
                className="card p-4 flex items-start justify-between gap-4 flex-wrap"
                data-testid="proposed-relationship"
              >
                <div className="min-w-0">
                  <div className="font-medium text-sm">
                    {rel.from_table}.{rel.from_column}
                    <span aria-hidden="true" style={{ color: "var(--text-muted)" }}>
                      {" → "}
                    </span>
                    {rel.to_table}.{rel.to_column}
                  </div>
                  <p className="text-sm mt-1" style={{ color: "var(--text-secondary)" }}>
                    {rel.rationale}
                  </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-sm tabular" style={{ color: "var(--text-muted)" }}>
                    {(rel.confidence * 100).toFixed(0)}%
                  </span>
                  <button className="btn btn-primary text-sm" onClick={() => decide(rel.id, true)}>
                    Confirm
                  </button>
                  <button className="btn btn-ghost text-sm" onClick={() => decide(rel.id, false)}>
                    Reject
                  </button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {confirmed.length > 0 && (
        <section className="mb-6">
          <h2 className="font-semibold mb-3">Confirmed</h2>
          <div className="flex flex-col gap-2">
            {confirmed.map((rel) => (
              <div key={rel.id} className="card p-3 flex items-center justify-between gap-4">
                <span className="text-sm">
                  <span aria-hidden="true" style={{ color: "var(--status-good)" }}>
                    ✓{" "}
                  </span>
                  {rel.from_table}.{rel.from_column} → {rel.to_table}.{rel.to_column}
                </span>
                <button className="btn btn-ghost text-sm" onClick={() => decide(rel.id, false)}>
                  Remove
                </button>
              </div>
            ))}
          </div>
        </section>
      )}

      {rejected.length > 0 && (
        <section>
          <h2 className="font-semibold mb-3">Rejected</h2>
          <div className="flex flex-col gap-2">
            {rejected.map((rel) => (
              <div key={rel.id} className="card p-3 flex items-center justify-between gap-4">
                <span className="text-sm" style={{ color: "var(--text-muted)" }}>
                  {rel.from_table}.{rel.from_column} → {rel.to_table}.{rel.to_column}
                </span>
                <button className="btn btn-ghost text-sm" onClick={() => decide(rel.id, true)}>
                  Confirm instead
                </button>
              </div>
            ))}
          </div>
        </section>
      )}

      {model && model.relationships.length === 0 && (
        <div className="card p-10 text-center">
          <p className="font-medium mb-1">No relationships detected yet</p>
          <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
            Upload at least two related files — orders and customers, for example —
            then run a scan.
          </p>
        </div>
      )}
    </AppShell>
  );
}
