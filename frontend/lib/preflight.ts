// Typed client for the OpsMind pre-flight API (backend/app/api/preflight.py).

export type CheckStatus = "pass" | "warn" | "fail" | "skipped";
export type PreflightStatus = "ready" | "ready_with_warnings" | "needs_config" | "blocked";
export type Framework = "nextjs" | "vite" | "cra" | "vue-cli" | "static" | "unknown";
export type FixItem = "build_script" | "spa_routing" | "env_template" | "dockerfile" | "dockerignore";

export interface PreflightCheck {
  id: string;
  title: string;
  status: CheckStatus;
  weight: number;
  message: string;
  why: string;
  file: string | null;
  auto_fixable: boolean;
  manual_allowed: boolean;
  details: string[];
}

export interface MissingFile {
  file: string;
  check_id: string;
  severity: "fail" | "warn";
  reason: string;
  message: string;
  auto_fixable: boolean;
  manual_allowed: boolean;
}

export interface PreflightReport {
  status: PreflightStatus;
  health_score: number;
  config_needed: number;
  detected_framework: Framework;
  framework_label: string;
  app_root: string;
  package_manager: "npm" | "yarn" | "pnpm" | "bun";
  lockfile: string | null;
  node_version: string;
  output_dir: string | null;
  runtime: "static" | "node" | null;
  next_mode: "server" | "standalone" | "export" | null;
  container_port: number;
  env_keys: string[];
  public_env_keys: string[];
  has_client_router: boolean;
  checks: PreflightCheck[];
  missing_files: MissingFile[];
  auto_fixable_items: FixItem[];
  manual_items: string[];
  inspected_at: string;
}

export interface GeneratedFile {
  path: string;
  check_id: string | null;
  description: string | null;
  /** Only present on dry runs. */
  content?: string;
}

export interface InjectRequest {
  repo_path: string;
  mode?: "auto" | "manual";
  items?: FixItem[];
  files?: { path: string; content: string }[];
  overwrite?: boolean;
  dry_run?: boolean;
}

export interface InjectResponse {
  dry_run: boolean;
  files: GeneratedFile[];
  written: { path: string; check_id: string }[];
  skipped: { item: string; reason: string }[];
  notes: string[];
  report: PreflightReport;
}

export class PreflightApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "PreflightApiError";
  }
}

export const DEFAULT_API_BASE = process.env.NEXT_PUBLIC_OPSMIND_API_URL ?? "http://localhost:8000";

interface RequestOptions {
  baseUrl?: string;
  signal?: AbortSignal;
}

function errorMessage(body: unknown, fallback: string): string {
  // FastAPI returns {detail: string} for HTTPException and {detail: [{msg}]} for validation errors.
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d))).join("; ");
    }
  }
  return fallback;
}

async function post<T>(path: string, body: unknown, { baseUrl = DEFAULT_API_BASE, signal }: RequestOptions = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${baseUrl.replace(/\/$/, "")}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new PreflightApiError("Could not reach the OpsMind API. Check your connection and try again.", 0);
  }

  const data: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    throw new PreflightApiError(errorMessage(data, `Request failed with status ${res.status}`), res.status);
  }
  return data as T;
}

export function inspectFrontend(repoPath: string, options?: RequestOptions): Promise<PreflightReport> {
  return post<PreflightReport>("/api/inspect/frontend", { repo_path: repoPath }, options);
}

export function injectFixes(request: InjectRequest, options?: RequestOptions): Promise<InjectResponse> {
  return post<InjectResponse>("/api/autofix/inject", request, options);
}
