/** Persistent read-only sync bar. Telemetry ingestion is backend-owned. */
import {
  formatAge,
  formatLag,
  rowSyncState,
  shortId,
  stateLabel,
  stateTone,
  SyncSnapshot,
  useLive,
} from "@/lib/live";

export function PlayerBar() {
  const { summary, rows, connection, effectiveState, loading, error, lastSuccessfulFetchAt, refresh } = useLive();
  const summarySync = summary?.sync_state && typeof summary.sync_state === "object"
    ? summary.sync_state as SyncSnapshot
    : null;
  const backlog = summary?.backlog ?? summarySync?.backlog ?? rows.reduce((total, row) => total + (row.backlog ?? 0), 0);
  const sourceSuccesses = rows
    .map((row) => row.last_success_at)
    .filter((value): value is string => !!value)
    .sort((left, right) => Date.parse(right) - Date.parse(left));
  const lastSuccess = summarySync?.last_success_at ?? sourceSuccesses[0] ?? lastSuccessfulFetchAt;

  return (
    <footer className={`fixed bottom-0 left-0 right-0 z-40 border-t px-4 py-2 text-white ${
      effectiveState === "error" ? "border-red-500 bg-red-950" :
      effectiveState === "stale" ? "border-amber-500 bg-amber-950" : "border-slate-700 bg-slate-900"
    }`}>
      <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-2 text-xs">
        <button
          className="rounded bg-slate-700 px-2 py-1 font-semibold hover:bg-slate-600 disabled:opacity-40"
          onClick={() => void refresh()}
          disabled={loading}
          title="Refresh the persisted monitor view; this does not trigger model traffic"
        >
          {loading ? "Refreshing…" : "Refresh status"}
        </button>
        <span className={`rounded-full border px-2 py-0.5 font-bold ${stateTone(effectiveState)}`}>
          {stateLabel(effectiveState)}
        </span>
        <span className="text-slate-300">latest source success {formatAge(lastSuccess)}</span>
        <span className="rounded bg-slate-800 px-2 py-0.5 font-mono">backlog {backlog}</span>
        {error && <span className="font-semibold text-red-200">{error}</span>}

        <span className="ml-auto flex flex-wrap items-center justify-end gap-1.5">
          {rows.length === 0 && !loading && <span className="text-slate-400">no persisted observations</span>}
          {rows.map((row) => {
            const state = connection === "error" ? "error" : rowSyncState(row);
            const digest = row.content_sha256 ?? row.window_digest;
            return (
              <span
                key={row.registry_id}
                className={`rounded border px-2 py-0.5 ${stateTone(state)}`}
                title={`window ${row.window_id ?? "none"} · digest ${digest ?? "none"} · source lag ${formatLag(row.source_lag_ms)}`}
              >
                {row.registry_id.replace("AICT-", "")} · {stateLabel(state)} · w:{shortId(row.window_id, 8)} · lag:{formatLag(row.source_lag_ms)}
              </span>
            );
          })}
        </span>
      </div>
    </footer>
  );
}
