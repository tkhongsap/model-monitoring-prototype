import { useRouter } from "@/lib/nav";
import { HealthChip, Legend, ScrollBox, Td, Th, TierBadge } from "@/components/ui";
import { formatLag, rowSyncState, shortId, stateLabel, stateTone, useLive } from "@/lib/live";

const LANES = ["Quality", "Safety & security", "Reliability", "Drift & degradation", "Feedback & action loop"];

export default function Heatmap() {
  const router = useRouter();
  const { rows, summary, connection, effectiveState, error } = useLive();
  if (!summary) return (
    <div className={`rounded-lg border p-8 text-center text-sm ${error ? "border-red-300 bg-red-50 text-red-800" : "border-slate-200 bg-white text-slate-500"}`}>
      {error ?? "Connecting to persisted monitor observations…"}
    </div>
  );

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-bold">Monitoring Lane Heatmap</h1>
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold ${stateTone(effectiveState)}`}>{stateLabel(effectiveState)}</span>
        {error && <span className="text-xs font-semibold text-red-700">{error}</span>}
      </div>
      <ScrollBox>
        <table className="w-full">
          <thead>
            <tr>
              <Th>Use case / evidence</Th><Th>Sync</Th><Th>Risk</Th>
              {LANES.map((lane) => <Th key={lane}>{lane.replace(" & action loop", "/action")}</Th>)}
              <Th>Overall</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const state = connection === "error" ? "error" : rowSyncState(row);
              const digest = row.content_sha256 ?? row.window_digest;
              return (
                <tr key={row.registry_id} className={`hover:bg-slate-50 ${state === "stale" || state === "error" ? "bg-amber-50/40" : ""}`}>
                  <Td>
                    <button className="text-left font-semibold text-slate-800 hover:text-[#E60012]" onClick={() => router.push(`/use-case/${row.registry_id}`)}>
                      {row.use_case_name}
                      <span className="ml-2 font-mono text-[10px] text-slate-400">{row.registry_id}</span>
                    </button>
                    <div className="mt-0.5 font-mono text-[9px] text-slate-400">
                      batch {shortId(row.batch_id, 9)} · window {shortId(row.window_id, 9)} · digest {shortId(digest, 9)} · model {row.model_version ?? "—"} · n={row.record_count ?? "—"} · lag {formatLag(row.source_lag_ms)}
                    </div>
                  </Td>
                  <Td>
                    <span className={`whitespace-nowrap rounded-full border px-2 py-0.5 text-[9px] font-bold ${stateTone(state)}`}>{stateLabel(state)}</span>
                  </Td>
                  <Td><TierBadge tier={row.risk_tier} /></Td>
                  {LANES.map((lane) => (
                    <Td key={lane} className="text-center">
                      <button onClick={() => router.push(`/use-case/${row.registry_id}#${encodeURIComponent(lane)}`)}>
                        <HealthChip
                          health={row.lanes?.[lane] ?? "Unknown"}
                          striped={lane === "Feedback & action loop" && !!row.feedback_unknown_reason}
                          title={row.feedback_unknown_reason && lane === "Feedback & action loop"
                            ? row.feedback_unknown_reason
                            : `${lane} — ${row.tooltips?.[lane]?.metric ?? "not instrumented"}`}
                        />
                      </button>
                    </Td>
                  ))}
                  <Td className="text-center"><HealthChip health={row.overall ?? "Unknown"} /></Td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </ScrollBox>
      <Legend />
      <p className="mt-2 text-[11px] text-slate-400">Unknown means evidence was missing, insufficient, or not instrumented; it is never promoted to Green.</p>
    </div>
  );
}
