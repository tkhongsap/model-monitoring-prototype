import { useRouter } from "@/lib/nav";
import { AlertsStrip } from "@/components/alerts";
import { HealthChip, Section, StatCard, TierBadge } from "@/components/ui";
import {
  formatAge,
  formatLag,
  provenanceText,
  rowSyncState,
  shortId,
  stateLabel,
  stateTone,
  useLive,
} from "@/lib/live";

const LANES = ["Quality", "Safety & security", "Reliability", "Drift & degradation", "Feedback & action loop"];

function field(value: unknown): string {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

export default function AtAGlance() {
  const router = useRouter();
  const { summary, rows, connection, effectiveState, error, lastSuccessfulFetchAt } = useLive();
  if (!summary) return <Empty error={error} />;
  const counts = summary.overall_counts ?? {};
  const count = summary.use_case_count ?? rows.length;

  return (
    <div>
      {(effectiveState === "error" || effectiveState === "stale") && (
        <div className={`mb-4 rounded-lg border p-3 text-sm ${
          effectiveState === "error" ? "border-red-300 bg-red-50 text-red-900" : "border-amber-300 bg-amber-50 text-amber-900"
        }`} role="alert">
          <b>{stateLabel(effectiveState)}:</b>{" "}
          {error ?? "One or more telemetry sources have stopped delivering current observations."}
          {" "}The values below are retained for context and are not presented as current. Monitor API last reached {formatAge(lastSuccessfulFetchAt)}.
        </div>
      )}

      <p className="mb-3 text-sm font-semibold text-slate-600">
        Persisted model observations — each window carries source provenance and a content digest
      </p>
      <Section title="Portfolio — observed health">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          <StatCard value={count} label="Observed AI use cases" />
          <StatCard value={counts.Red ?? 0} label="Red" accent="#E60012" />
          <StatCard value={counts.Amber ?? 0} label="Amber" accent="#B8860B" />
          <StatCard value={counts.Green ?? 0} label="Green" accent="#00A66C" />
          <StatCard value={counts.Unknown ?? 0} label="Unknown" accent="#6B7280" />
        </div>
      </Section>

      <AlertsStrip />

      <Section title="Source → monitor pipeline">
        <div className="grid gap-3 xl:grid-cols-3">
          {rows.map((row) => {
            const state = connection === "error" ? "error" : rowSyncState(row);
            const sourceTick = row.source_tick ?? row.producer_tick;
            const digest = row.content_sha256 ?? row.window_digest;
            return (
              <article key={row.registry_id} className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
                <div className="flex items-start gap-2">
                  <div>
                    <h2 className="font-bold text-slate-900">{row.use_case_name}</h2>
                    <p className="font-mono text-[10px] text-slate-400">{row.registry_id}</p>
                  </div>
                  <span className={`ml-auto rounded-full border px-2 py-0.5 text-[10px] font-bold ${stateTone(state)}`}>
                    {stateLabel(state)}
                  </span>
                </div>

                <div className="mt-3 grid grid-cols-[1fr_auto_1fr] items-center gap-2 text-center text-xs">
                  <div className="rounded bg-slate-50 p-2">
                    <div className="font-semibold">Producer</div>
                    <div className="font-mono text-slate-600">tick {field(sourceTick)}</div>
                  </div>
                  <div aria-hidden="true" className="text-lg text-slate-400">→</div>
                  <div className="rounded bg-slate-50 p-2">
                    <div className="font-semibold">Monitor</div>
                    <div className="font-mono text-slate-600">tick {field(row.observed_tick ?? row.tick)}</div>
                  </div>
                </div>

                <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-[11px]">
                  <dt className="font-semibold text-slate-500">Window</dt><dd className="truncate font-mono" title={row.window_id ?? undefined}>{shortId(row.window_id)}</dd>
                  <dt className="font-semibold text-slate-500">POC batch</dt><dd className="truncate font-mono" title={row.batch_id ?? undefined}>{shortId(row.batch_id)}</dd>
                  <dt className="font-semibold text-slate-500">Digest</dt><dd className="truncate font-mono" title={digest ?? undefined}>{shortId(digest)}</dd>
                  <dt className="font-semibold text-slate-500">Observation</dt><dd className="truncate font-mono" title={row.observation_id ?? undefined}>{shortId(row.observation_id)}</dd>
                  <dt className="font-semibold text-slate-500">Model</dt><dd className="font-mono">{field(row.model_version)}</dd>
                  <dt className="font-semibold text-slate-500">Records</dt><dd className="font-mono">{field(row.record_count)}</dd>
                  <dt className="font-semibold text-slate-500">Provenance</dt><dd>{provenanceText(row)}</dd>
                  <dt className="font-semibold text-slate-500">Last success</dt><dd>{formatAge(row.last_success_at)}</dd>
                  <dt className="font-semibold text-slate-500">Backlog</dt><dd className="font-mono">{field(row.backlog)}</dd>
                  <dt className="font-semibold text-slate-500">Source lag</dt><dd className="font-mono">{formatLag(row.source_lag_ms)}</dd>
                </dl>
              </article>
            );
          })}
        </div>
      </Section>

      <Section title="Observed models">
        <div className="flex flex-col gap-2">
          {rows.map((row) => (
            <button
              key={row.registry_id}
              onClick={() => router.push(`/use-case/${row.registry_id}`)}
              className="flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-white p-3 text-left shadow-sm hover:border-[#E60012] hover:shadow"
            >
              <div className="min-w-[220px]">
                <div className="font-semibold text-slate-800">{row.use_case_name}</div>
                <div className="font-mono text-[10px] text-slate-400">{row.registry_id} · {row.business_unit}</div>
              </div>
              <TierBadge tier={row.risk_tier} />
              <HealthChip health={row.overall ?? "Unknown"} />
              <span className="ml-auto flex flex-wrap items-center gap-1">
                {LANES.map((lane) => (
                  <HealthChip key={lane} small health={row.lanes?.[lane] ?? "Unknown"} title={`${lane} — ${row.tooltips?.[lane]?.metric ?? "not instrumented"}`} />
                ))}
              </span>
            </button>
          ))}
        </div>
      </Section>

      <p className="text-[11px] text-slate-400">
        Health is derived only from successfully pulled observations. Missing or uninstrumented evidence remains Unknown.
      </p>
    </div>
  );
}

function Empty({ error }: { error: string | null }) {
  return (
    <div className={`rounded-lg border p-10 text-center ${error ? "border-red-300 bg-red-50 text-red-800" : "border-slate-200 bg-white text-slate-500"}`}>
      <div className="font-bold">{error ? "Monitor connection failed" : "Connecting to the monitor"}</div>
      <div className="mt-1 text-sm">{error ?? "Waiting for the persisted live portfolio."}</div>
    </div>
  );
}
