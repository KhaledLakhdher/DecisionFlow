"use client";

import type { DataModel } from "@/lib/api";

/**
 * The dimensional model, drawn.
 *
 * A table of edges makes the reader assemble the shape in their head; the shape
 * *is* the thing this module produces, so it is drawn instead. Fact in the
 * centre, dimensions on a ring around it, each edge labelled with the column it
 * joins on.
 *
 * Only confirmed relationships appear. A proposal is not part of the model yet,
 * and drawing it as though it were would misrepresent what the system will
 * actually query.
 */

const BOX_WIDTH = 132;
const BOX_HEIGHT = 46;

// Far enough apart that the join-column label sits in clear space between the
// two boxes. At a shorter radius the boxes nearly touch and the label — the
// thing that explains the edge — disappears underneath them.
const RADIUS_HORIZONTAL = 230;
const RADIUS_RADIAL = 190;

export function StarDiagram({ model }: { model: DataModel }) {
  const confirmed = model.relationships.filter((rel) => rel.confirmed === true);
  const facts = model.tables.filter((t) => t.role === "fact");

  if (facts.length === 0 || confirmed.length === 0) {
    return (
      <div
        className="card p-8 text-center text-sm"
        style={{ color: "var(--text-muted)" }}
        data-testid="star-diagram-empty"
      >
        No confirmed relationships yet. Detect and confirm one to build a star
        schema.
      </div>
    );
  }

  // One diagram per fact. Most workspaces have exactly one.
  return (
    <div className="flex flex-col gap-6" data-testid="star-diagram">
      {facts.map((fact) => {
        const edges = confirmed.filter((rel) => rel.from_table === fact.table);
        if (edges.length === 0) return null;

        // One or two dimensions sit left and right of the fact; three or more
        // fan around it starting from the top. A fixed radial layout puts two
        // dimensions directly above and below, which wastes the whole width of
        // a wide card and makes the figure needlessly tall.
        const horizontal = edges.length <= 2;
        const startAngle = horizontal ? 0 : -Math.PI / 2;
        const radius = horizontal ? RADIUS_HORIZONTAL : RADIUS_RADIAL;

        const width = 2 * radius + BOX_WIDTH + 60;
        const height = horizontal
          ? BOX_HEIGHT + 140
          : 2 * radius + BOX_HEIGHT + 80;
        const cx = width / 2;
        const cy = height / 2;

        return (
          <figure key={fact.dataset_id} className="card p-4 m-0 overflow-x-auto">
            <figcaption className="text-sm mb-2" style={{ color: "var(--text-secondary)" }}>
              star.{fact.table} — {fact.table} joined to {edges.length}{" "}
              {edges.length === 1 ? "dimension" : "dimensions"}
            </figcaption>

            <svg
              viewBox={`0 0 ${width} ${height}`}
              className="w-full h-auto mx-auto"
              role="img"
              aria-label={`Star schema for ${fact.table}`}
              // Capped at its natural size. Left to fill a wide card the whole
              // figure scales up and the labels balloon; a diagram should not
              // get bigger just because the window did.
              style={{ maxWidth: width }}
            >
              {edges.map((edge, i) => {
                // Spread evenly around the fact from the chosen start angle.
                const angle = (2 * Math.PI * i) / edges.length + startAngle;
                const dx = cx + radius * Math.cos(angle);
                const dy = cy + radius * Math.sin(angle);

                return (
                  <g key={edge.id}>
                    <line
                      x1={cx}
                      y1={cy}
                      x2={dx}
                      y2={dy}
                      stroke="var(--axis)"
                      strokeWidth={1.5}
                    />
                    {/* Join column, placed just off the midpoint so it does not
                        sit on the line itself. */}
                    <text
                      x={(cx + dx) / 2}
                      y={(cy + dy) / 2 - 8}
                      textAnchor="middle"
                      fontSize={11}
                      fill="var(--text-secondary)"
                    >
                      {edge.from_column}
                    </text>

                    <g transform={`translate(${dx - BOX_WIDTH / 2}, ${dy - BOX_HEIGHT / 2})`}>
                      <rect
                        width={BOX_WIDTH}
                        height={BOX_HEIGHT}
                        rx={8}
                        fill="var(--surface)"
                        stroke="var(--series-3)"
                        strokeWidth={1.5}
                      />
                      <text
                        x={BOX_WIDTH / 2}
                        y={19}
                        textAnchor="middle"
                        fontSize={12}
                        fontWeight={600}
                        fill="var(--text-primary)"
                      >
                        {edge.to_table}
                      </text>
                      <text
                        x={BOX_WIDTH / 2}
                        y={34}
                        textAnchor="middle"
                        fontSize={10}
                        fill="var(--text-muted)"
                      >
                        dimension
                      </text>
                    </g>
                  </g>
                );
              })}

              {/* Fact drawn last so the edges pass behind it. */}
              <g transform={`translate(${cx - BOX_WIDTH / 2}, ${cy - BOX_HEIGHT / 2})`}>
                <rect
                  width={BOX_WIDTH}
                  height={BOX_HEIGHT}
                  rx={8}
                  fill="var(--series-1)"
                  stroke="var(--series-1)"
                  strokeWidth={2}
                />
                <text
                  x={BOX_WIDTH / 2}
                  y={19}
                  textAnchor="middle"
                  fontSize={12}
                  fontWeight={600}
                  fill="#ffffff"
                >
                  {fact.table}
                </text>
                <text
                  x={BOX_WIDTH / 2}
                  y={34}
                  textAnchor="middle"
                  fontSize={10}
                  fill="rgba(255,255,255,0.85)"
                >
                  fact · {fact.rows?.toLocaleString() ?? "—"} rows
                </text>
              </g>
            </svg>
          </figure>
        );
      })}
    </div>
  );
}
