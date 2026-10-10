"use client";

import { useCallback, useEffect, useId, useRef, useState, type ChangeEvent } from "react";
import {
  ArrowLeft,
  ChevronDown,
  CircleCheck,
  CircleMinus,
  CircleX,
  Eye,
  FileCode2,
  FileUp,
  LoaderCircle,
  RefreshCw,
  Rocket,
  ScanSearch,
  TriangleAlert,
  Upload,
  X,
  Zap,
  type LucideIcon,
} from "lucide-react";

import {
  DEFAULT_API_BASE,
  injectFixes,
  inspectFrontend,
  PreflightApiError,
  type CheckStatus,
  type GeneratedFile,
  type InjectResponse,
  type PreflightCheck,
  type PreflightReport,
  type PreflightStatus,
} from "@/lib/preflight";

export interface FrontendHealthModalProps {
  open: boolean;
  /** Workspace-relative path of the ingested repo, e.g. "acme/web-app". */
  repoPath: string;
  onClose: () => void;
  /** Called when the user continues to the container build. Only offered when nothing is failing. */
  onProceed?: (report: PreflightReport) => void;
  /** Skip the initial scan when the caller already has a report (e.g. from the deploy pipeline). */
  initialReport?: PreflightReport;
  apiBaseUrl?: string;
}

type View = "summary" | "manual";
type Busy = null | "scan" | "autofix" | "preview" | "manual";

const MAX_UPLOAD_BYTES = 64 * 1024;

const CHECK_STYLES: Record<CheckStatus, { icon: LucideIcon; tone: string; label: string }> = {
  fail: { icon: CircleX, tone: "text-red-400", label: "Missing" },
  warn: { icon: TriangleAlert, tone: "text-amber-400", label: "Warning" },
  pass: { icon: CircleCheck, tone: "text-emerald-400", label: "OK" },
  skipped: { icon: CircleMinus, tone: "text-zinc-500", label: "N/A" },
};

const STATUS_BADGES: Record<PreflightStatus, { label: string; className: string }> = {
  ready: { label: "Ready to deploy", className: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300" },
  ready_with_warnings: { label: "Ready with warnings", className: "border-amber-500/30 bg-amber-500/10 text-amber-300" },
  needs_config: { label: "Config needed", className: "border-amber-500/30 bg-amber-500/10 text-amber-300" },
  blocked: { label: "Blocked", className: "border-red-500/30 bg-red-500/10 text-red-300" },
};

const STATUS_ORDER: Record<CheckStatus, number> = { fail: 0, warn: 1, pass: 2, skipped: 3 };

function describeError(err: unknown): string {
  if (err instanceof PreflightApiError) return err.message;
  if (err instanceof Error) return err.message;
  return "Something went wrong.";
}

function scoreTone(score: number): string {
  if (score >= 90) return "text-emerald-400";
  if (score >= 60) return "text-amber-400";
  return "text-red-400";
}

export default function FrontendHealthModal({
  open,
  repoPath,
  onClose,
  onProceed,
  initialReport,
  apiBaseUrl = DEFAULT_API_BASE,
}: FrontendHealthModalProps) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);

  const [report, setReport] = useState<PreflightReport | null>(initialReport ?? null);
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("summary");
  const [preview, setPreview] = useState<GeneratedFile[] | null>(null);
  const [result, setResult] = useState<Pick<InjectResponse, "written" | "skipped" | "notes"> | null>(null);
  const [overwrite, setOverwrite] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const scan = useCallback(
    async (signal?: AbortSignal) => {
      setBusy("scan");
      setError(null);
      try {
        setReport(await inspectFrontend(repoPath, { baseUrl: apiBaseUrl, signal }));
      } catch (err) {
        if (!signal?.aborted) setError(describeError(err));
      } finally {
        if (!signal?.aborted) setBusy(null);
      }
    },
    [repoPath, apiBaseUrl],
  );

  // Reset and (re)scan every time the modal opens.
  useEffect(() => {
    if (!open) return;
    setView("summary");
    setPreview(null);
    setResult(null);
    setDrafts({});
    setError(null);
    if (initialReport) {
      setReport(initialReport);
      return;
    }
    setReport(null);
    const controller = new AbortController();
    void scan(controller.signal);
    return () => controller.abort();
  }, [open, initialReport, scan]);

  // Escape to close, lock background scroll, move focus into the dialog.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && busy === null) onClose();
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", onKey);
    panelRef.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKey);
    };
  }, [open, busy, onClose]);

  const runAutofix = async (dryRun: boolean) => {
    if (!report) return;
    setBusy(dryRun ? "preview" : "autofix");
    setError(null);
    try {
      const res = await injectFixes(
        { repo_path: repoPath, mode: "auto", items: report.auto_fixable_items, overwrite, dry_run: dryRun },
        { baseUrl: apiBaseUrl },
      );
      if (dryRun) {
        setPreview(res.files);
      } else {
        setReport(res.report);
        setResult({ written: res.written, skipped: res.skipped, notes: res.notes });
        setPreview(null);
      }
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(null);
    }
  };

  const submitManual = async () => {
    const files = Object.entries(drafts)
      .filter(([, content]) => content.trim().length > 0)
      .map(([path, content]) => ({ path, content }));
    if (files.length === 0) {
      setError("Paste or upload at least one file.");
      return;
    }
    setBusy("manual");
    setError(null);
    try {
      const res = await injectFixes({ repo_path: repoPath, mode: "manual", files, overwrite }, { baseUrl: apiBaseUrl });
      setReport(res.report);
      setResult({ written: res.written, skipped: res.skipped, notes: res.notes });
      setDrafts({});
      setView("summary");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(null);
    }
  };

  if (!open) return null;

  const canProceed = report !== null && (report.status === "ready" || report.status === "ready_with_warnings");
  const sortedChecks = report ? [...report.checks].sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status]) : [];
  const hasFixes = report !== null && (report.auto_fixable_items.length > 0 || report.manual_items.length > 0);
  const issueCount = report ? report.checks.filter((c) => c.status === "fail" || c.status === "warn").length : 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        aria-hidden="true"
        onClick={() => busy === null && onClose()}
      />

      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="relative flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-950 text-zinc-100 shadow-2xl shadow-black/50 outline-none"
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-4 border-b border-zinc-800 px-6 py-5">
          <div className="flex items-start gap-3">
            <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-2">
              <ScanSearch className="size-5 text-sky-400" aria-hidden="true" />
            </div>
            <div>
              <h2 id={titleId} className="text-lg font-semibold text-zinc-50">
                Pre-flight Diagnostics
              </h2>
              <p className="mt-0.5 font-mono text-xs text-zinc-500">{repoPath}</p>
            </div>
          </div>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => void scan()}
              disabled={busy !== null}
              className="rounded-lg p-2 text-zinc-400 transition hover:bg-zinc-900 hover:text-zinc-100 disabled:opacity-40"
              aria-label="Re-run scan"
            >
              <RefreshCw className={`size-4 ${busy === "scan" ? "animate-spin" : ""}`} aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={onClose}
              disabled={busy !== null && busy !== "scan"}
              className="rounded-lg p-2 text-zinc-400 transition hover:bg-zinc-900 hover:text-zinc-100 disabled:opacity-40"
              aria-label="Close"
            >
              <X className="size-4" aria-hidden="true" />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {error && (
            <div role="alert" className="mb-4 flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
              <CircleX className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              <span>{error}</span>
            </div>
          )}

          {!report && busy === "scan" && <ScanSkeleton />}

          {report && view === "summary" && (
            <div className="space-y-5">
              <Overview report={report} />

              {result && <InjectResult result={result} />}

              <section aria-label="Checks">
                <div className="mb-2 flex items-center justify-between">
                  <h3 className="text-sm font-medium text-zinc-300">Checks</h3>
                  <span className="text-xs text-zinc-500">
                    {issueCount === 0 ? "No issues" : `${issueCount} issue${issueCount === 1 ? "" : "s"}`}
                  </span>
                </div>
                <ul className="divide-y divide-zinc-800/80 overflow-hidden rounded-xl border border-zinc-800 bg-zinc-900/40">
                  {sortedChecks.map((check) => (
                    <CheckRow key={check.id} check={check} />
                  ))}
                </ul>
              </section>

              {preview && <PreviewFiles files={preview} onClose={() => setPreview(null)} />}
            </div>
          )}

          {report && view === "manual" && (
            <ManualUpload
              report={report}
              drafts={drafts}
              onChange={(path, content) => setDrafts((d) => ({ ...d, [path]: content }))}
              onError={setError}
            />
          )}
        </div>

        {/* Footer */}
        {report && (
          <div className="flex flex-col gap-3 border-t border-zinc-800 bg-zinc-950/80 px-6 py-4 sm:flex-row sm:items-center sm:justify-between">
            {hasFixes || view === "manual" ? (
              <label className="flex items-center gap-2 text-xs text-zinc-400">
                <input
                  type="checkbox"
                  checked={overwrite}
                  onChange={(e) => setOverwrite(e.target.checked)}
                  className="size-3.5 rounded border-zinc-700 bg-zinc-900 accent-sky-500"
                />
                Overwrite existing files
              </label>
            ) : (
              <span />
            )}

            {view === "summary" ? (
              <div className="flex flex-wrap items-center justify-end gap-2">
                {canProceed && onProceed ? (
                  <button
                    type="button"
                    onClick={() => onProceed(report)}
                    className="inline-flex items-center gap-2 rounded-lg bg-emerald-500 px-4 py-2 text-sm font-medium text-emerald-950 transition hover:bg-emerald-400"
                  >
                    <Rocket className="size-4" aria-hidden="true" />
                    Continue to build
                  </button>
                ) : null}

                {report.manual_items.length > 0 && (
                  <button
                    type="button"
                    onClick={() => {
                      setError(null);
                      setView("manual");
                    }}
                    disabled={busy !== null}
                    className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 px-4 py-2 text-sm font-medium text-zinc-200 transition hover:border-zinc-600 hover:bg-zinc-900 disabled:opacity-50"
                  >
                    <Upload className="size-4" aria-hidden="true" />
                    Provide config manually
                  </button>
                )}

                {report.auto_fixable_items.length > 0 && (
                  <>
                    <button
                      type="button"
                      onClick={() => void runAutofix(true)}
                      disabled={busy !== null}
                      className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-zinc-400 transition hover:bg-zinc-900 hover:text-zinc-200 disabled:opacity-50"
                    >
                      {busy === "preview" ? (
                        <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />
                      ) : (
                        <Eye className="size-4" aria-hidden="true" />
                      )}
                      Preview
                    </button>
                    <button
                      type="button"
                      onClick={() => void runAutofix(false)}
                      disabled={busy !== null}
                      className="inline-flex items-center gap-2 rounded-lg bg-sky-500 px-4 py-2 text-sm font-medium text-sky-950 shadow-lg shadow-sky-500/20 transition hover:bg-sky-400 disabled:opacity-60"
                    >
                      {busy === "autofix" ? (
                        <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />
                      ) : (
                        <Zap className="size-4" aria-hidden="true" />
                      )}
                      {busy === "autofix" ? "Generating…" : `Auto-generate & inject (${report.auto_fixable_items.length})`}
                    </button>
                  </>
                )}
              </div>
            ) : (
              <div className="flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setView("summary")}
                  disabled={busy !== null}
                  className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-zinc-400 transition hover:bg-zinc-900 hover:text-zinc-200"
                >
                  <ArrowLeft className="size-4" aria-hidden="true" />
                  Back
                </button>
                <button
                  type="button"
                  onClick={() => void submitManual()}
                  disabled={busy !== null}
                  className="inline-flex items-center gap-2 rounded-lg bg-sky-500 px-4 py-2 text-sm font-medium text-sky-950 transition hover:bg-sky-400 disabled:opacity-60"
                >
                  {busy === "manual" ? (
                    <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />
                  ) : (
                    <Upload className="size-4" aria-hidden="true" />
                  )}
                  Save & re-scan
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function Overview({ report }: { report: PreflightReport }) {
  const badge = STATUS_BADGES[report.status];
  const facts: [string, string][] = [
    ["Package manager", report.package_manager + (report.lockfile ? "" : " (no lockfile)")],
    ["Output", report.runtime === "node" ? `Node server :${report.container_port}` : `${report.output_dir ?? "?"}/ → nginx :${report.container_port}`],
    ["Node", `v${report.node_version}`],
    ["App root", report.app_root],
  ];

  return (
    <section className="grid gap-4 sm:grid-cols-[auto_1fr]">
      <div className="flex items-center gap-5 rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 sm:flex-col sm:justify-center">
        <ScoreRing score={report.health_score} />
        <div className="w-full min-w-40">
          <div className="flex h-2 overflow-hidden rounded-full bg-zinc-800" aria-hidden="true">
            <div className="bg-emerald-500 transition-all duration-700" style={{ width: `${report.health_score}%` }} />
            <div className="bg-amber-500/70 transition-all duration-700" style={{ width: `${report.config_needed}%` }} />
          </div>
          <div className="mt-1.5 flex flex-wrap justify-between gap-x-3 text-xs">
            <span className="text-emerald-400">{report.health_score}% ready</span>
            <span className="text-amber-400">{report.config_needed}% config needed</span>
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="text-xs uppercase tracking-wide text-zinc-500">Detected framework</p>
            <p className="mt-0.5 text-lg font-semibold text-zinc-50">{report.framework_label}</p>
          </div>
          <span className={`rounded-full border px-2.5 py-1 text-xs font-medium ${badge.className}`}>{badge.label}</span>
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
          {facts.map(([label, value]) => (
            <div key={label}>
              <dt className="text-xs text-zinc-500">{label}</dt>
              <dd className="mt-0.5 truncate font-mono text-zinc-200" title={value}>
                {value}
              </dd>
            </div>
          ))}
        </dl>
        {report.status === "blocked" && (
          <p className="mt-4 rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2 text-xs text-red-300">
            Some issues cannot be generated automatically. Fix them in the repository, then re-run the scan.
          </p>
        )}
      </div>
    </section>
  );
}

function ScoreRing({ score }: { score: number }) {
  const radius = 52;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - Math.min(Math.max(score, 0), 100) / 100);
  return (
    <div className="relative size-32 shrink-0" role="img" aria-label={`Readiness score ${score} percent`}>
      <svg viewBox="0 0 120 120" className="size-full -rotate-90">
        <circle cx="60" cy="60" r={radius} fill="none" strokeWidth="10" className="stroke-zinc-800" />
        <circle
          cx="60"
          cy="60"
          r={radius}
          fill="none"
          strokeWidth="10"
          strokeLinecap="round"
          stroke="currentColor"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          className={`${scoreTone(score)} transition-[stroke-dashoffset] duration-700 ease-out`}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-3xl font-semibold tabular-nums text-zinc-50">{score}%</span>
        <span className="text-xs text-zinc-500">ready</span>
      </div>
    </div>
  );
}

function CheckRow({ check }: { check: PreflightCheck }) {
  const [expanded, setExpanded] = useState(false);
  const style = CHECK_STYLES[check.status];
  const Icon = style.icon;
  const isIssue = check.status === "fail" || check.status === "warn";

  return (
    <li>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-start gap-3 px-4 py-3 text-left transition hover:bg-zinc-900/60"
      >
        <Icon className={`mt-0.5 size-4 shrink-0 ${style.tone}`} aria-label={style.label} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`text-sm font-medium ${check.status === "skipped" ? "text-zinc-500" : "text-zinc-100"}`}>
              {check.title}
            </span>
            {check.file && isIssue && (
              <code className="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[11px] text-zinc-300">{check.file}</code>
            )}
            {isIssue &&
              (check.auto_fixable ? (
                <span className="inline-flex items-center gap-1 rounded-full bg-sky-500/10 px-2 py-0.5 text-[11px] font-medium text-sky-300">
                  <Zap className="size-3" aria-hidden="true" />
                  AI fixable
                </span>
              ) : (
                <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-[11px] font-medium text-zinc-400">Manual</span>
              ))}
          </div>
          <p className="mt-0.5 text-sm text-zinc-400">{check.message}</p>
        </div>
        <ChevronDown className={`mt-0.5 size-4 shrink-0 text-zinc-600 transition ${expanded ? "rotate-180" : ""}`} aria-hidden="true" />
      </button>
      {expanded && (
        <div className="space-y-2 px-11 pb-3 text-xs text-zinc-400">
          <p>
            <span className="font-medium text-zinc-300">Why it matters: </span>
            {check.why}
          </p>
          {check.details.length > 0 && (
            <ul className="flex flex-wrap gap-1.5">
              {check.details.map((d) => (
                <li key={d} className="rounded bg-zinc-800/80 px-1.5 py-0.5 font-mono text-[11px] text-zinc-300">
                  {d}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

function InjectResult({ result }: { result: Pick<InjectResponse, "written" | "skipped" | "notes"> }) {
  return (
    <div className="space-y-2 rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4 text-sm">
      {result.written.length > 0 ? (
        <p className="flex flex-wrap items-center gap-1.5 text-emerald-300">
          <CircleCheck className="size-4" aria-hidden="true" />
          Injected:
          {result.written.map((w) => (
            <code key={w.path} className="rounded bg-emerald-500/10 px-1.5 py-0.5 font-mono text-xs">
              {w.path}
            </code>
          ))}
        </p>
      ) : (
        <p className="text-zinc-400">No files were written.</p>
      )}
      {result.skipped.map((s) => (
        <p key={s.item} className="flex items-start gap-1.5 text-xs text-amber-300">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          <span>
            <code className="font-mono">{s.item}</code>: {s.reason}
          </span>
        </p>
      ))}
      {result.notes.map((n) => (
        <p key={n} className="text-xs text-zinc-400">
          {n}
        </p>
      ))}
    </div>
  );
}

function PreviewFiles({ files, onClose }: { files: GeneratedFile[]; onClose: () => void }) {
  const [active, setActive] = useState(files[0]?.path ?? "");
  const current = files.find((f) => f.path === active) ?? files[0];
  if (!current) return null;

  return (
    <section aria-label="Generated file preview" className="overflow-hidden rounded-xl border border-zinc-800">
      <div className="flex items-center justify-between border-b border-zinc-800 bg-zinc-900/60 pr-2">
        <div className="flex overflow-x-auto" role="tablist">
          {files.map((f) => (
            <button
              key={f.path}
              type="button"
              role="tab"
              aria-selected={f.path === current.path}
              onClick={() => setActive(f.path)}
              className={`flex items-center gap-1.5 whitespace-nowrap border-r border-zinc-800 px-3 py-2 font-mono text-xs transition ${
                f.path === current.path ? "bg-zinc-950 text-zinc-100" : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              <FileCode2 className="size-3.5" aria-hidden="true" />
              {f.path}
            </button>
          ))}
        </div>
        <button type="button" onClick={onClose} className="rounded p-1 text-zinc-500 hover:text-zinc-200" aria-label="Close preview">
          <X className="size-3.5" aria-hidden="true" />
        </button>
      </div>
      {current.description && <p className="border-b border-zinc-800 px-4 py-2 text-xs text-zinc-500">{current.description}</p>}
      <pre className="max-h-72 overflow-auto bg-zinc-950 p-4 font-mono text-xs leading-relaxed text-zinc-300">
        <code>{current.content}</code>
      </pre>
    </section>
  );
}

function ManualUpload({
  report,
  drafts,
  onChange,
  onError,
}: {
  report: PreflightReport;
  drafts: Record<string, string>;
  onChange: (path: string, content: string) => void;
  onError: (message: string | null) => void;
}) {
  const reasons = new Map(report.missing_files.map((m) => [m.file, m]));

  const loadFile = (path: string) => async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (file.size > MAX_UPLOAD_BYTES) {
      onError(`${file.name} is larger than 64 KB.`);
      return;
    }
    onError(null);
    onChange(path, await file.text());
  };

  return (
    <div className="space-y-4">
      <p className="text-sm text-zinc-400">
        Paste or upload your own versions. They are written to <code className="font-mono text-zinc-300">{report.app_root}/</code>{" "}
        and the repo is re-scanned.
      </p>
      {report.manual_items.map((path) => {
        const info = reasons.get(path);
        const inputId = `manual-${path.replace(/[^a-z0-9]/gi, "-")}`;
        return (
          <div key={path} className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <label htmlFor={inputId} className="font-mono text-sm text-zinc-100">
                  {path}
                </label>
                {info && <p className="mt-0.5 text-xs text-zinc-500">{info.message}</p>}
              </div>
              <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-zinc-700 px-2.5 py-1.5 text-xs text-zinc-300 transition hover:bg-zinc-800">
                <FileUp className="size-3.5" aria-hidden="true" />
                Load file
                <input type="file" className="sr-only" onChange={loadFile(path)} />
              </label>
            </div>
            <textarea
              id={inputId}
              value={drafts[path] ?? ""}
              onChange={(e) => onChange(path, e.target.value)}
              rows={6}
              spellCheck={false}
              placeholder={`Contents of ${path}`}
              className="mt-3 w-full resize-y rounded-lg border border-zinc-800 bg-zinc-950 p-3 font-mono text-xs text-zinc-200 placeholder:text-zinc-600 focus:border-sky-500/50 focus:outline-none focus:ring-2 focus:ring-sky-500/20"
            />
          </div>
        );
      })}
    </div>
  );
}

function ScanSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-live="polite">
      <div className="flex items-center gap-2 text-sm text-zinc-400">
        <LoaderCircle className="size-4 animate-spin text-sky-400" aria-hidden="true" />
        Scanning repository…
      </div>
      <div className="grid gap-4 sm:grid-cols-[auto_1fr]">
        <div className="h-40 w-48 animate-pulse rounded-xl bg-zinc-900" />
        <div className="h-40 animate-pulse rounded-xl bg-zinc-900" />
      </div>
      {Array.from({ length: 5 }, (_, i) => (
        <div key={i} className="h-12 animate-pulse rounded-lg bg-zinc-900" />
      ))}
    </div>
  );
}
