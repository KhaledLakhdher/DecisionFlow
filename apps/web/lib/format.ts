/** Value formatting, kept in one place so a figure reads the same everywhere. */

export function formatValue(
  value: number | null,
  format: string,
  // Set when several values are shown together. Decimal places are chosen for
  // the whole set rather than per value, so a chart cannot render "$7,460"
  // beside "$840.00" and look like two different units.
  scaleReference?: number,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";

  switch (format) {
    case "currency":
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: (scaleReference ?? value) >= 1000 ? 0 : 2,
      }).format(value);
    case "percent":
      return `${(value * 100).toFixed(1)}%`;
    case "decimal":
      return value.toFixed(2);
    default:
      return new Intl.NumberFormat().format(Math.round(value));
  }
}

/** Compact form for axis ticks, where space is scarce. */
export function compact(value: number): string {
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatPeriod(period: string, grain: string): string {
  // Parsed from the string's own components rather than via `new Date(period)`.
  // The API sends "2026-01-01 00:00:00"; JS treats a space-separated timestamp
  // as *local* time, so rendering it back in UTC shifts it backwards — far
  // enough to move January 2026 into December 2025 and make consecutive
  // months collide on the axis.
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(period);
  if (!match) return period;

  const [, year, month, day] = match;
  const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));

  if (grain === "year") return year;
  if (grain === "month" || grain === "quarter") {
    return date.toLocaleDateString(undefined, {
      month: "short",
      year: "2-digit",
      timeZone: "UTC",
    });
  }
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

export function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return new Intl.NumberFormat().format(value);
  return String(value);
}
