"use client";

/**
 * Tab strip.
 *
 * Labels carry a count when there is something behind them ("Predictions ·3"),
 * because a tab's whole cost is that it hides content — a reader with no signal
 * has no reason to click. The count is the signal.
 *
 * Real buttons with `role="tab"` and arrow-key navigation rather than styled
 * divs, so the strip works for keyboard and screen-reader users.
 */

export type Tab = {
  id: string;
  label: string;
  count?: number | null;
};

export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: Tab[];
  active: string;
  onChange: (id: string) => void;
}) {
  function onKeyDown(event: React.KeyboardEvent) {
    const index = tabs.findIndex((tab) => tab.id === active);
    if (event.key === "ArrowRight") {
      onChange(tabs[(index + 1) % tabs.length].id);
    } else if (event.key === "ArrowLeft") {
      onChange(tabs[(index - 1 + tabs.length) % tabs.length].id);
    }
  }

  return (
    <div
      role="tablist"
      onKeyDown={onKeyDown}
      className="flex gap-1 border-b mb-6 overflow-x-auto"
      style={{ borderColor: "var(--border)" }}
    >
      {tabs.map((tab) => {
        const selected = tab.id === active;
        return (
          <button
            key={tab.id}
            role="tab"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)}
            className="px-4 py-2 text-sm whitespace-nowrap -mb-px border-b-2 transition-colors"
            style={{
              borderColor: selected ? "var(--series-1)" : "transparent",
              color: selected ? "var(--text-primary)" : "var(--text-secondary)",
              fontWeight: selected ? 600 : 400,
            }}
          >
            {tab.label}
            {tab.count ? (
              <span
                className="ml-2 text-xs tabular px-1.5 py-0.5 rounded"
                style={{ background: "var(--gridline)", color: "var(--text-secondary)" }}
              >
                {tab.count}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
