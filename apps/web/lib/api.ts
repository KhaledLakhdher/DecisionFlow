/**
 * API client.
 *
 * The access token is held in memory only; the refresh token goes to
 * localStorage. That split matters: a short-lived token in memory is gone when
 * the tab closes, while the refresh token is what survives a reload — and it is
 * the only one the server can revoke.
 *
 * On a 401 the client refreshes once and replays the original request, so an
 * expired access token is invisible to callers rather than surfacing as a
 * spurious logout mid-session.
 */

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const API = `${BASE}/api/v1`;
const REFRESH_KEY = "decisionflow.refresh";

let accessToken: string | null = null;
let refreshInFlight: Promise<boolean> | null = null;

export type ApiError = { code: string; message: string; details?: unknown };

export class RequestError extends Error {
  constructor(
    readonly status: number,
    readonly error: ApiError,
  ) {
    super(error.message);
  }
}

export function setTokens(access: string, refresh: string) {
  accessToken = access;
  localStorage.setItem(REFRESH_KEY, refresh);
}

export function clearTokens() {
  accessToken = null;
  localStorage.removeItem(REFRESH_KEY);
}

export function hasSession(): boolean {
  return Boolean(accessToken || localStorage.getItem(REFRESH_KEY));
}

async function refreshSession(): Promise<boolean> {
  const refresh = localStorage.getItem(REFRESH_KEY);
  if (!refresh) return false;

  // Collapse concurrent refreshes. Several requests failing at once would
  // otherwise each rotate the token, and rotation invalidates its predecessor —
  // so the parallel attempts would log the user out.
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const response = await fetch(`${API}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refresh }),
        });
        if (!response.ok) {
          clearTokens();
          return false;
        }
        const data = await response.json();
        setTokens(data.access_token, data.refresh_token);
        return true;
      } catch {
        return false;
      } finally {
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}

type RequestOptions = {
  method?: string;
  body?: unknown;
  formData?: FormData;
  retryOn401?: boolean;
};

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, formData, retryOn401 = true } = options;

  const headers: Record<string, string> = {};
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(`${API}${path}`, {
    method,
    headers,
    body: formData ?? (body !== undefined ? JSON.stringify(body) : undefined),
  });

  if (response.status === 401 && retryOn401 && (await refreshSession())) {
    return request<T>(path, { ...options, retryOn401: false });
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;

  if (!response.ok) {
    throw new RequestError(
      response.status,
      payload?.error ?? { code: "unknown", message: `Request failed (${response.status})` },
    );
  }
  return payload as T;
}

// --------------------------------------------------------------------------
// Types mirroring the API's response models
// --------------------------------------------------------------------------
export type TokenPair = {
  access_token: string;
  refresh_token: string;
  active_org_id: string | null;
  active_role: string | null;
};

export type Me = {
  user: { id: string; email: string; full_name: string };
  active_org_id: string | null;
  active_role: string | null;
  memberships: { org_id: string; role: string; organization: { id: string; name: string } }[];
};

export type Dataset = {
  id: string;
  name: string;
  slug: string;
  status: "uploaded" | "analyzing" | "ready" | "failed";
  status_message: string | null;
  original_filename: string;
  size_bytes: number;
  row_count: number | null;
  column_count: number | null;
  clean_row_count: number | null;
  cleaned_at: string | null;
  quality_score: number | null;
  created_at: string;
};

export type DatasetColumn = {
  id: string;
  position: number;
  name: string;
  normalized_name: string;
  data_type: string;
  cleaned_type: string | null;
  effective_type: string;
  nullable: boolean;
  sample_values: unknown[];
  profile: Record<string, unknown>;
};

export type DatasetDetail = Dataset & {
  columns: DatasetColumn[];
  checksum: string;
  cleaning_actions: {
    column: string | null;
    actions: string[];
    reason: string;
  }[];
};

export type Kpi = {
  key: string;
  label: string;
  description: string | null;
  value: number | null;
  format: "currency" | "number" | "percent" | "decimal";
  higher_is_better: boolean;
  sql: string;
  details: { depends_on?: string[]; growth?: { change: number | null; previous: number } };
};

export type QualityIssue = {
  id: string;
  code: string;
  severity: "info" | "warning" | "error";
  column_name: string | null;
  message: string;
};

export type QualityReport = {
  quality_score: number | null;
  raw_row_count: number | null;
  clean_row_count: number | null;
  rows_removed: number | null;
  issue_counts: Record<string, number>;
  issues: QualityIssue[];
};

export type Timeseries = {
  grain: string;
  measure: string;
  time_column: string | null;
  points: { period: string; value: number | null }[];
};

export type Breakdown = {
  dimension: string;
  measure: string;
  available_dimensions: string[];
  items: { label: string; value: number | null }[];
};

export type Preview = {
  layer: string;
  columns: string[];
  rows: Record<string, unknown>[];
  total_rows: number | null;
};

/** Which prediction panels this dataset's shape can support. */
export type Capabilities = {
  forecast: boolean;
  anomalies: boolean;
  churn: boolean;
  dimensions: boolean;
};

export type Forecast = {
  measure: string;
  grain: string;
  method: string;
  confidence: number;
  rationale: string;
  history_points: number;
  noise_ratio: number | null;
  history: { period: string; value: number | null }[];
  points: { period: string; value: number; lower: number; upper: number }[];
};

export type Anomalies = {
  measure: string;
  grain: string;
  periods_examined: number;
  anomalies: {
    period: string;
    value: number;
    expected: number;
    deviation_ratio: number;
    direction: "above" | "below";
    severity: "warning" | "serious";
    message: string;
  }[];
};

export type Churn = {
  definition: string;
  cutoff_days: number;
  customers: number;
  lapsed: number;
  lapsed_rate: number;
  accuracy: number | null;
  drivers: {
    feature: string;
    label: string;
    coefficient: number;
    direction: string;
  }[];
  at_risk: {
    customer: string;
    risk: number;
    band: "high" | "medium" | "low";
    recency_days: number;
    frequency: number;
    monetary: number;
    reason: string;
  }[];
};

export type DataModel = {
  tables: {
    dataset_id: string;
    name: string;
    table: string;
    role: "fact" | "dimension" | "unknown";
    rows: number | null;
  }[];
  relationships: {
    id: string;
    from_table: string | null;
    from_column: string;
    to_table: string | null;
    to_column: string;
    confidence: number;
    confirmed: boolean | null;
    rationale: string;
  }[];
};

export type Answer = {
  conversation_id: string;
  answer: string;
  sql: string | null;
  rows: Record<string, unknown>[];
  row_count: number;
  answerable: boolean;
  attempts: number;
  corrections: string[];
};

// --------------------------------------------------------------------------
// Endpoints
// --------------------------------------------------------------------------
export const api = {
  login: (email: string, password: string) =>
    request<TokenPair>("/auth/login", { method: "POST", body: { email, password } }),

  register: (payload: {
    email: string;
    password: string;
    full_name: string;
    organization_name: string;
  }) => request<TokenPair>("/auth/register", { method: "POST", body: payload }),

  me: () => request<Me>("/auth/me"),

  datasets: () => request<Dataset[]>("/datasets"),
  dataset: (id: string) => request<DatasetDetail>(`/datasets/${id}`),

  upload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ dataset: Dataset; message: string }>("/datasets/upload", {
      method: "POST",
      formData: form,
    });
  },

  deleteDataset: (id: string) => request<void>(`/datasets/${id}`, { method: "DELETE" }),

  kpis: (id: string) => request<Kpi[]>(`/datasets/${id}/kpis`),
  quality: (id: string) => request<QualityReport>(`/datasets/${id}/quality`),
  timeseries: (id: string, grain = "month") =>
    request<Timeseries>(`/datasets/${id}/timeseries?grain=${grain}`),
  breakdown: (id: string, dimension?: string) =>
    request<Breakdown>(
      `/datasets/${id}/breakdown${dimension ? `?dimension=${encodeURIComponent(dimension)}` : ""}`,
    ),
  preview: (id: string, layer: "clean" | "raw" = "clean") =>
    request<Preview>(`/datasets/${id}/preview?layer=${layer}&limit=25`),

  capabilities: (id: string) => request<Capabilities>(`/datasets/${id}/capabilities`),
  forecast: (id: string, horizon = 3) =>
    request<Forecast>(`/datasets/${id}/forecast?horizon=${horizon}`),
  anomalies: (id: string) => request<Anomalies>(`/datasets/${id}/anomalies`),
  churn: (id: string) => request<Churn>(`/datasets/${id}/churn`),

  model: () => request<DataModel>("/model"),
  detectRelationships: () => request<unknown>("/model/detect", { method: "POST" }),
  decideRelationship: (id: string, confirmed: boolean) =>
    request<{ model: DataModel }>(`/model/relationships/${id}`, {
      method: "PATCH",
      body: { confirmed },
    }),

  ask: (id: string, question: string, conversationId?: string) =>
    request<Answer>(`/datasets/${id}/ask`, {
      method: "POST",
      body: { question, conversation_id: conversationId ?? null },
    }),

  summary: (id: string) =>
    request<{ summary: string }>(`/datasets/${id}/summary`, { method: "POST" }),
};
