"use client";

import { useState } from "react";
import type { Briefing, BriefingFinding, FleetCapability } from "@/lib/api";

/**
 * The agent fleet's brief.
 *
 * Two layers, kept visibly apart. The narrative at the top is the model's
 * phrasing; the findings below are what the specialists actually computed, each
 * with the query or method behind it. A reader who distrusts a sentence can
 * open the finding it came from and check the arithmetic — which is the only
 * reason to trust the sentence in the first place.
 *
 * Refusals are shown, not hidden. "There were only 6 customers, so churn could
 * not be modelled" is a finding, and dropping it would leave the reader
 * assuming retention had been looked at.
 */

const SPECIALIST_LABEL: Record<string, string> = {
  analyst: "Analyst",
  investigator: "Investigator",
  sentinel: "Sentinel",
  forecaster: "Forecaster",
  retention: "Retention",
};

const SPECIALIST_ROLE: Record<string, string> = {
  analyst: "queried the data directly",
  investigator: "decomposed the change across every dimension",
  sentinel: "looked for periods off the trend",
  forecaster: "projected the measure forward",
  retention: "scored customers on lapse risk",
};

const STATUS_COLOR: Record<string, string> = {
  ok: "var(--status-good)",
  refused: "var(--text-muted)",
  failed: "var(--status-critical)",
};

const STATUS_LABEL: Record<string, string> = {
  ok: "reported",
  refused: "declined",
  failed: "failed",
};

const PRIORITY_COLOR: Record<string, string> = {
  high: "var(--status-critical)",
  medium: "var(--status-warning)",
  low: "var(--text-muted)",
};

function FindingCard({ finding }: { finding: BriefingFinding }) {
  const [showEvidence, setShowEvidence] = useState(false);
  const evidence = finding.sql ?? null;

  return (
    <div className="card p-4" data-testid="briefing-finding" data-status={finding.status}>
      <div className="flex items-center gap-2 mb-1 flex-wrap">
        {/* Status is a dot plus a word — never color alone. */}
        <span
          aria-hidden="true"
          className="inline-block w-2 h-2 rounded-full shrink-0"
          style={{ background: STATUS_COLOR[finding.status] }}
        />
        <span className="font-medium text-sm">
          {SPECIALIST_LABEL[finding.specialist] ?? finding.specialist}
        </span>
        <span className="text-xs" style={{ color: "var(--text-muted)" }}>
          {STATUS_LABEL[finding.status]}
          {SPECIALIST_ROLE[finding.specialist]
            ? ` · ${SPECIALIST_ROLE[finding.specialist]}`
            : ""}
        </span>
      </div>

      <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
        {finding.headline}
      </p>

      {(finding.method || evidence) && (
        <div className="mt-2 flex items-center gap-3 flex-wrap">
          {finding.method && (
            <span className="text-xs" style={{ color: "var(--text-muted)" }}>
              {/* Method names arrive as identifiers from the ML layer
                  (`linear_trend`). The reader is not looking at an enum. */}
              via {finding.method.replace(/_/g, " ")}
            </span>
          )}
          {evidence && (
            <button
              className="text-xs underline"
              style={{ color: "var(--text-muted)" }}
              onClick={() => setShowEvidence((open) => !open)}
            >
              {showEvidence ? "Hide the query" : "Show the query"}
            </button>
          )}
        </div>
      )}

      {showEvidence && evidence && (
        <pre
          className="text-xs mt-2 p-2 rounded overflow-x-auto"
          style={{ background: "var(--page)", color: "var(--text-secondary)" }}
        >
          {evidence}
        </pre>
      )}
    </div>
  );
}

function label(name: string): string {
  return SPECIALIST_LABEL[name] ?? name;
}

export function BriefingPanel({
  briefing,
  roster,
  busy,
  onRun,
  error,
}: {
  briefing: Briefing | null;
  roster: FleetCapability | null;
  busy: boolean;
  onRun: (question?: string) => void;
  error: string | null;
}) {
  const [question, setQuestion] = useState("");

  const standing = roster?.specialists ?? [];
  // Specialists that only apply once something has been asked — the analyst
  // has nothing to query without a question. Naming them separately keeps the
  // panel from promising five when a blank run consults four.
  const questionOnly = (roster?.with_question ?? []).filter(
    (name) => !standing.includes(name),
  );

  return (
    <div className="flex flex-col gap-4" data-testid="briefing-panel">
      <section className="card p-5">
        <h2 className="font-semibold mb-1">Ask the fleet</h2>
        <p className="text-sm mb-3" style={{ color: "var(--text-secondary)" }}>
          A lead analyst routes your question to the specialists that can answer
          it, then writes up what they found. Leave it blank for a standing
          briefing across everything this dataset supports.
        </p>

        {standing.length > 0 && (
          <p className="text-xs mb-3" style={{ color: "var(--text-muted)" }}>
            A blank run consults {standing.map(label).join(", ")}
            {questionOnly.length > 0 &&
              `, and the ${questionOnly.map(label).join(", ")} once you ask something specific`}
            .
          </p>
        )}

        <div className="flex gap-2 flex-wrap">
          <input
            className="input flex-1 min-w-64"
            placeholder="Why did revenue change last month?"
            value={question}
            disabled={busy}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !busy) onRun(question.trim() || undefined);
            }}
            aria-label="Question for the fleet"
          />
          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() => onRun(question.trim() || undefined)}
            data-testid="run-briefing"
          >
            {busy ? "Working…" : question.trim() ? "Ask" : "Run briefing"}
          </button>
        </div>

        {busy && (
          <p className="text-sm mt-3" style={{ color: "var(--text-secondary)" }}>
            Specialists are running in parallel. This takes a few seconds.
          </p>
        )}
      </section>

      {error && (
        <p
          role="alert"
          className="text-sm px-3 py-2 rounded"
          style={{ background: "var(--surface)", color: "var(--status-critical)" }}
        >
          {error}
        </p>
      )}

      {briefing && (
        <>
          <section className="card p-5" data-testid="briefing-summary">
            {briefing.question && (
              <p className="text-sm mb-2" style={{ color: "var(--text-muted)" }}>
                You asked: {briefing.question}
              </p>
            )}
            <h2 className="text-lg font-semibold mb-2">{briefing.headline}</h2>
            <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
              {briefing.narrative}
            </p>

            <p className="text-xs mt-3" style={{ color: "var(--text-muted)" }}>
              {briefing.findings.length}{" "}
              {briefing.findings.length === 1 ? "specialist" : "specialists"}
              {briefing.duration_ms !== null &&
                ` · ${(briefing.duration_ms / 1000).toFixed(1)}s`}
              {briefing.input_tokens !== null &&
                ` · ${(
                  briefing.input_tokens + (briefing.output_tokens ?? 0)
                ).toLocaleString()} tokens`}
            </p>
          </section>

          {briefing.recommendations.length > 0 && (
            <section className="card p-5" data-testid="briefing-recommendations">
              <h2 className="font-semibold mb-3">What to do</h2>
              <ol className="flex flex-col gap-3">
                {briefing.recommendations.map((item, index) => (
                  <li key={index} className="flex gap-3">
                    {/* Priority carries a word beside the dot. */}
                    <span
                      aria-hidden="true"
                      className="inline-block w-2 h-2 rounded-full mt-1.5 shrink-0"
                      style={{
                        background:
                          PRIORITY_COLOR[item.priority ?? "medium"] ??
                          PRIORITY_COLOR.medium,
                      }}
                    />
                    <div className="min-w-0">
                      <div className="text-sm font-medium">
                        {item.action}
                        <span
                          className="ml-2 text-xs font-normal"
                          style={{ color: "var(--text-muted)" }}
                        >
                          {item.priority ?? "medium"} priority
                        </span>
                      </div>
                      {item.rationale && (
                        <p className="text-sm mt-0.5" style={{ color: "var(--text-secondary)" }}>
                          {item.rationale}
                        </p>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            </section>
          )}

          <section>
            <h2 className="font-semibold mb-1">The evidence</h2>
            <p className="text-sm mb-3" style={{ color: "var(--text-secondary)" }}>
              Every figure above came from one of these. The write-up is
              generated; these are computed.
            </p>
            <div className="flex flex-col gap-2">
              {briefing.findings.map((finding) => (
                <FindingCard key={finding.id} finding={finding} />
              ))}
            </div>

            {briefing.plan_reasoning && (
              <p className="text-xs mt-3" style={{ color: "var(--text-muted)" }}>
                Routing: {briefing.plan_reasoning}
              </p>
            )}
          </section>
        </>
      )}
    </div>
  );
}
