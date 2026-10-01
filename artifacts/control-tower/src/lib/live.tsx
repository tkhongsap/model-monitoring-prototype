/** Strict-live API context. The backend worker owns telemetry polling; the
 * browser only reads persisted observations and never advances a model cursor. */
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

export type Health = "Green" | "Amber" | "Red" | "Unknown";
export type SyncStateName = "connecting" | "catching_up" | "at_tail" | "idle" | "stale" | "error";
export type ConnectionState = "connecting" | "ok" | "error";

export interface SyncSnapshot {
  state?: SyncStateName;
  sync_state?: SyncStateName | SyncSnapshot;
  last_success_at?: string | null;
  backlog?: number | null;
  source_tick?: number | null;
  producer_tick?: number | null;
  observed_tick?: number | null;
  observation_id?: string | null;
  window_id?: string | null;
  batch_id?: string | null;
  content_sha256?: string | null;
  window_digest?: string | null;
  model_version?: string | null;
  provenance_counts?: Record<string, number> | null;
  provenance?: string | Record<string, number> | null;
  record_count?: number | null;
  source_lag_ms?: number | null;
  errors?: Record<string, unknown> | string | null;
}

export interface LiveRow extends SyncSnapshot {
  registry_id: string;
  use_case_name: string;
  business_unit?: string;
  platform_or_app?: string;
  status?: string;
  risk_tier?: string;
  current_health?: Health;
  overall?: Health;
  lanes?: Record<string, Health>;
  tooltips?: Record<string, { metric?: string }>;
  feedback_unknown_reason?: string | null;
  telemetry_status?: string;
  lane_kind?: "ml" | "llm";
  tick?: number | null;
  mode?: string;
  waiting?: boolean;
  stale?: boolean;
}

/** One persisted health-transition alert (GET /api/live/alerts). Derived
 * metadata only: the worker opens and resolves rows; the browser reads. */
export interface LiveAlert {
  alert_id: string;
  source_id: string;
  lane: string;
  from_health: Health | null;
  to_health: Health;
  tick: number | null;
  observation_id: string | null;
  opened_at: number;
  resolved_at: number | null;
  resolved_tick: number | null;
  open_delivery_status: "pending" | "delivered" | "error" | "skipped" | string;
  open_delivery_error: string | null;
  open_delivered_at: number | null;
  resolve_delivery_status: "n/a" | "pending" | "delivered" | "error" | "skipped" | string;
  resolve_delivery_error: string | null;
  resolve_delivered_at: number | null;
}

export interface LiveAlertList {
  contract_version: string;
  redacted: boolean;
  rows: LiveAlert[];
}

export interface LiveSummary {
  use_case_count: number;
  as_of: Record<string, number | null>;
  overall_counts: Record<string, number>;
  lane_counts: Record<string, Record<string, number>>;
  open_alerts?: Record<string, number>;
  rows: LiveRow[];
  sync_state?: SyncStateName | SyncSnapshot;
  state?: SyncStateName;
  backlog?: number;
  sync_sources?: SyncSnapshot[];
  generated_at?: string;
  build?: { git_sha?: string; version?: string };
}

interface LiveCtx {
  rows: LiveRow[];
  summary: LiveSummary | null;
  loading: boolean;
  connection: ConnectionState;
  effectiveState: SyncStateName;
  error: string | null;
  lastSuccessfulFetchAt: string | null;
  revision: number;
  refresh: () => Promise<void>;
}

const Ctx = createContext<LiveCtx>({
  rows: [],
  summary: null,
  loading: true,
  connection: "connecting",
  effectiveState: "connecting",
  error: null,
  lastSuccessfulFetchAt: null,
  revision: 0,
  refresh: async () => {},
});

const STATE_WEIGHT: Record<SyncStateName, number> = {
  at_tail: 0,
  idle: 1,
  connecting: 2,
  catching_up: 3,
  stale: 4,
  error: 5,
};

export function syncStateOf(value: unknown): SyncStateName | null {
  if (typeof value === "string" && value in STATE_WEIGHT) return value as SyncStateName;
  if (value && typeof value === "object") {
    const candidate = value as SyncSnapshot;
    return syncStateOf(candidate.state) ?? (candidate.sync_state === value ? null : syncStateOf(candidate.sync_state));
  }
  return null;
}

export function rowSyncState(row: LiveRow): SyncStateName {
  const canonical = syncStateOf(row.sync_state) ?? syncStateOf(row.state);
  if (canonical) return canonical;
  if (row.stale) return "stale";
  if (row.waiting) return "connecting";
  return "idle";
}

function worstState(states: SyncStateName[]): SyncStateName {
  return states.reduce((worst, state) => STATE_WEIGHT[state] > STATE_WEIGHT[worst] ? state : worst, "at_tail");
}

export function stateLabel(state: SyncStateName): string {
  return ({
    connecting: "CONNECTING",
    catching_up: "CATCHING UP",
    at_tail: "AT TAIL",
    idle: "IDLE",
    stale: "STALE",
    error: "ERROR",
  })[state];
}

export function stateTone(state: SyncStateName): string {
  if (state === "error") return "border-red-300 bg-red-600 text-white";
  if (state === "stale") return "border-amber-300 bg-amber-600 text-white";
  if (state === "at_tail") return "border-emerald-300 bg-emerald-600 text-white";
  if (state === "catching_up") return "border-sky-300 bg-sky-600 text-white";
  return "border-slate-300 bg-slate-600 text-white";
}

export function formatAge(value?: string | null): string {
  if (!value) return "never";
  const milliseconds = Date.now() - Date.parse(value);
  if (!Number.isFinite(milliseconds)) return "unknown";
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

export function formatLag(milliseconds?: number | null): string {
  if (milliseconds == null || !Number.isFinite(milliseconds)) return "—";
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(1)}s`;
  return `${Math.round(milliseconds / 60_000)}m`;
}

export function shortId(value?: string | null, length = 12): string {
  if (!value) return "—";
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

export function provenanceText(row: SyncSnapshot): string {
  const value = row.provenance_counts ?? row.provenance;
  if (!value) return "not reported";
  if (typeof value === "string") return value;
  const entries = Object.entries(value).filter(([, count]) => count > 0);
  return entries.length ? entries.map(([kind, count]) => `${kind}: ${count}`).join(" · ") : "not reported";
}

export function LiveProvider({ children }: { children: React.ReactNode }) {
  const [summary, setSummary] = useState<LiveSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [lastSuccessfulFetchAt, setLastSuccessfulFetchAt] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch("/api/live/portfolio", {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`monitor API returned HTTP ${response.status}`);
      const payload = await response.json() as LiveSummary;
      if (!payload || !Array.isArray(payload.rows)) throw new Error("monitor API returned an invalid live portfolio");
      setSummary(payload);
      setConnection("ok");
      setError(null);
      setLastSuccessfulFetchAt(new Date().toISOString());
      setRevision((value) => value + 1);
    } catch (caught) {
      const message = caught instanceof Error && caught.name === "AbortError"
        ? "monitor API request timed out"
        : caught instanceof Error ? caught.message : "monitor API request failed";
      setConnection("error");
      setError(message);
    } finally {
      window.clearTimeout(timeout);
      setLoading(false);
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const rows = summary?.rows ?? [];
  const effectiveState = useMemo<SyncStateName>(() => {
    if (connection === "error") return "error";
    if (!summary) return "connecting";
    const summaryState = syncStateOf(summary.sync_state);
    const rowStates = rows.map(rowSyncState);
    return worstState(summaryState ? [summaryState, ...rowStates] : rowStates.length ? rowStates : ["idle"]);
  }, [connection, rows, summary]);

  return (
    <Ctx.Provider value={{
      rows,
      summary,
      loading,
      connection,
      effectiveState,
      error,
      lastSuccessfulFetchAt,
      revision,
      refresh,
    }}>
      {children}
    </Ctx.Provider>
  );
}

export const useLive = () => useContext(Ctx);

export interface LiveResource<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  lastSuccessfulFetchAt: string | null;
}

/** Fetch detail data after every successful portfolio refresh. Previous data is
 * retained for context, but an error is surfaced explicitly beside it. */
export function useLiveResource<T = unknown>(path: string): LiveResource<T> {
  const { revision } = useLive();
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastSuccessfulFetchAt, setLastSuccessfulFetchAt] = useState<string | null>(null);

  useEffect(() => {
    if (!path) {
      setLoading(false);
      return;
    }
    let alive = true;
    const controller = new AbortController();
    const run = async () => {
      try {
        const response = await fetch(path, { cache: "no-store", signal: controller.signal });
        if (!response.ok) throw new Error(`detail API returned HTTP ${response.status}`);
        const payload = await response.json() as T;
        if (!alive) return;
        setData(payload);
        setError(null);
        setLastSuccessfulFetchAt(new Date().toISOString());
      } catch (caught) {
        if (!alive || (caught instanceof Error && caught.name === "AbortError")) return;
        setError(caught instanceof Error ? caught.message : "detail API request failed");
      } finally {
        if (alive) setLoading(false);
      }
    };
    void run();
    return () => {
      alive = false;
      controller.abort();
    };
  }, [path, revision]);

  return { data, loading, error, lastSuccessfulFetchAt };
}

export function useLiveApi<T = unknown>(path: string): T | null {
  return useLiveResource<T>(path).data;
}
