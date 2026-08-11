"use client";

import { useState } from "react";
import { api, RequestError, type Answer } from "@/lib/api";
import { formatCell } from "@/lib/format";

type Turn = { question: string; answer: Answer | null; error?: string };

const SUGGESTIONS = [
  "What is our total revenue?",
  "Which category performs best?",
  "How did revenue change month over month?",
];

export function Chat({ datasetId }: { datasetId: string }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [conversationId, setConversationId] = useState<string | undefined>();

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy) return;

    setQuestion("");
    setBusy(true);
    setTurns((prev) => [...prev, { question: trimmed, answer: null }]);

    try {
      const answer = await api.ask(datasetId, trimmed, conversationId);
      setConversationId(answer.conversation_id);
      setTurns((prev) => {
        const next = [...prev];
        next[next.length - 1] = { question: trimmed, answer };
        return next;
      });
    } catch (err) {
      const message =
        err instanceof RequestError ? err.error.message : "Something went wrong.";
      setTurns((prev) => {
        const next = [...prev];
        next[next.length - 1] = { question: trimmed, answer: null, error: message };
        return next;
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card p-5">
      <h2 className="font-semibold mb-1">Ask your data</h2>
      <p className="text-sm mb-4" style={{ color: "var(--text-secondary)" }}>
        Questions are answered by querying your cleaned data — every answer shows its SQL.
      </p>

      {turns.length === 0 && (
        <div className="flex flex-wrap gap-2 mb-4">
          {SUGGESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              className="btn btn-ghost text-sm"
              onClick={() => void send(suggestion)}
            >
              {suggestion}
            </button>
          ))}
        </div>
      )}

      <div className="flex flex-col gap-4 mb-4">
        {turns.map((turn, i) => (
          <div key={i} className="flex flex-col gap-2">
            <div className="text-sm font-medium">{turn.question}</div>

            {turn.answer === null && !turn.error && (
              <div className="text-sm" style={{ color: "var(--text-muted)" }}>
                Thinking…
              </div>
            )}

            {turn.error && (
              <div className="text-sm" style={{ color: "var(--status-critical)" }}>
                {turn.error}
              </div>
            )}

            {turn.answer && <AnswerBlock answer={turn.answer} />}
          </div>
        ))}
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void send(question);
        }}
        className="flex gap-2"
      >
        <input
          className="input"
          placeholder="Ask a question about this data…"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          disabled={busy}
        />
        <button className="btn btn-primary shrink-0" disabled={busy || !question.trim()}>
          Ask
        </button>
      </form>
    </div>
  );
}

function AnswerBlock({ answer }: { answer: Answer }) {
  const [showDetail, setShowDetail] = useState(false);

  return (
    <div className="rounded p-3" style={{ background: "var(--page)" }}>
      <p className="text-sm whitespace-pre-wrap">{answer.answer}</p>

      {!answer.answerable && (
        <p className="text-xs mt-2" style={{ color: "var(--text-muted)" }}>
          This question could not be answered from this dataset.
        </p>
      )}

      {answer.sql && (
        <>
          <button
            onClick={() => setShowDetail((v) => !v)}
            className="text-xs underline underline-offset-2 mt-2"
            style={{ color: "var(--text-muted)" }}
          >
            {showDetail ? "Hide query" : `Show query · ${answer.row_count} rows`}
          </button>

          {showDetail && (
            <div className="mt-2">
              <pre
                className="text-xs p-2 rounded overflow-x-auto"
                style={{ background: "var(--surface)", color: "var(--text-secondary)" }}
              >
                {answer.sql}
              </pre>

              {answer.rows.length > 0 && (
                <div className="overflow-x-auto mt-2">
                  <table className="text-xs w-full">
                    <thead>
                      <tr>
                        {Object.keys(answer.rows[0]).map((key) => (
                          <th
                            key={key}
                            className="text-left px-2 py-1 font-medium"
                            style={{ color: "var(--text-muted)" }}
                          >
                            {key}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {answer.rows.slice(0, 10).map((row, i) => (
                        <tr key={i}>
                          {Object.values(row).map((value, j) => (
                            <td key={j} className="px-2 py-1 tabular">
                              {formatCell(value)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {answer.corrections.length > 0 && (
                <p className="text-xs mt-2" style={{ color: "var(--text-muted)" }}>
                  Retried {answer.attempts}× — {answer.corrections[0]}
                </p>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
