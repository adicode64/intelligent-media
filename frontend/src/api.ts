/** Thin typed client for the FastAPI media-processing service. */

export const API_BASE =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8000";

const V1 = `${API_BASE}/api/v1`;

export type ProcessingStatus = "pending" | "processing" | "completed" | "failed";

export interface CheckResult {
  name: string;
  status: "pass" | "warning" | "fail" | "uncertain" | "skipped" | "error";
  message: string;
  score?: number | null;
  value?: unknown;
  confidence: number;
  heuristic: boolean;
  details?: Record<string, unknown>;
}

export interface AnalysisSummary {
  overall_status: string;
  confidence: number;
  passed: number;
  warnings: number;
  failures: number;
  uncertain: number;
  notes: string[];
}

export interface AnalysisResponse {
  image_id: string;
  status: ProcessingStatus;
  summary?: AnalysisSummary | null;
  checks: CheckResult[];
  vehicle_number?: string | null;
  analyzed_at?: string | null;
  message?: string | null;
}

export interface StatusResponse {
  id: string;
  status: ProcessingStatus;
  attempts: number;
  created_at?: string | null;
  updated_at?: string | null;
  failure_reason?: string | null;
}

async function unwrap<T>(res: Response): Promise<T> {
  const body = await res.text();
  let json: any = null;
  try {
    json = body ? JSON.parse(body) : null;
  } catch {
    /* non-JSON error page */
  }
  if (!res.ok) {
    throw new Error(json?.detail || json?.error || `Request failed (${res.status})`);
  }
  return json as T;
}

export async function uploadImage(file: File): Promise<{ id: string; status: ProcessingStatus }> {
  const form = new FormData();
  form.append("file", file);
  return unwrap(await fetch(`${V1}/images/upload`, { method: "POST", body: form }));
}

export async function getStatus(id: string): Promise<StatusResponse> {
  return unwrap(await fetch(`${V1}/images/${id}/status`));
}

export async function getResults(id: string): Promise<AnalysisResponse> {
  return unwrap(await fetch(`${V1}/images/${id}/results`));
}

export async function getHealth(): Promise<{ status: string; version: string }> {
  return unwrap(await fetch(`${API_BASE}/health`));
}
