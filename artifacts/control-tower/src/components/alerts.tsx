/** Read-only alert views (spec C.4). The worker opens and resolves alerts on
 * health transitions; the browser only lists them. No acknowledge workflow. */
import { HealthChip, Section, StatCard, Td, Th } from "@/components/ui";
import { formatAge, LiveAlert, LiveAlertList, useLiveResource } from "@/lib/live";
import { useRouter } from "@/lib/nav";

function epochToIso(seconds?: number | null): string | null {
  return seconds == null ? null : new Date(seconds * 1000).toISOString();
}

function transitionText(alert: LiveAlert): string {
  return `${alert.from_health ?? "—"} → ${alert.to_health}`;
}

function deliveryText(alert: LiveAlert): string {
  const phase = alert.resolved_at != null ? "resolve" : "open";
  const status = phase === "resolve" ? alert.resolve_delivery_status : alert.open_delivery_status;
  const error = phase === "resolve" ? alert.resolve_delivery_error : alert.open_delivery_error;
  if (status === "delivered") return "webhook delivered";
  if (status === "error") return `webhook retrying${error ? ` (${error})` : ""}`;
  if (status === "skipped") return "webhook not configured";
  if (status === "pending") return "webhook pending";
  return status;
}

/** Home page strip: open alerts by severity and the newest five. */
export function AlertsStrip() {
  const router = useRouter();
  const resource = useLiveResource<LiveAlertList>("/api/live/alerts?open=true&limit=100");
  const open = resource.data?.rows ?? [];
  const red = open.filter((a) => a.to_health === "Red").length;
  const amber = open.filter((a) => a.to_health === "Amber").length;
  const newest = open.slice(0, 5);
  return (
    <Section title="Alerts — open health transitions">
      {resource.error && (
        <div className="mb-2 rounded border border-red-300 bg-red-50 p-2 text-xs font-semibold text-red-800" role="alert">
          Alert list refresh failed: {resource.error}.
        </div>
      )}
      <div className="grid gap-3 md:grid-cols-[repeat(2,minmax(0,160px))_1fr]">
        <StatCard value={red} label="Open Red" accent="#E60012" />
        <StatCard value={amber} label="Open Amber" accent="#B8860B" />
        <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
          {newest.length === 0 ? (
            <div className="text-sm text-slate-400">
              {resource.loading ? "Loading persisted alerts…" : "No open alerts. Every Red or Green→Amber transition opens one here and resolves when the lane returns to Green."}
            </div>
          ) : (
            <ul className="flex flex-col gap-1">
              {newest.map((alert) => (
                <li key={alert.alert_id}>
                  <button
                    onClick={() => router.push(`/use-case/${alert.source_id}`)}
                    className="flex w-full flex-wrap items-center gap-2 rounded px-1 py-0.5 text-left text-xs hover:bg-slate-50"
                  >
                    <HealthChip small health={alert.to_health} />
                    <span className="font-mono text-slate-500">{alert.source_id}</span>
                    <span className="font-semibold text-slate-800">{alert.lane}</span>
                    <span className="text-slate-500">{transitionText(alert)}</span>
                    <span className="font-mono text-slate-400">tick {alert.tick ?? "—"}</span>
                    <span className="ml-auto text-slate-400">{formatAge(epochToIso(alert.opened_at))}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Section>
  );
}

/** Use-case page panel: open alerts and the last 20 resolved ones. */
export function AlertsPanel({ uc }: { uc: string }) {
  const resource = useLiveResource<LiveAlertList>(uc ? `/api/live/alerts?uc=${encodeURIComponent(uc)}&limit=100` : "");
  const rows = resource.data?.rows ?? [];
  const open = rows.filter((a) => a.resolved_at == null);
  const resolved = rows.filter((a) => a.resolved_at != null).slice(0, 20);
  return (
    <div className="mb-4 rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
      <h2 className="mb-2 text-xs font-bold uppercase tracking-wider text-slate-600">
        Alerts <span className="ml-1 font-mono text-slate-400">{open.length} open · {resolved.length} resolved</span>
      </h2>
      {resource.error && (
        <div className="mb-2 rounded border border-red-300 bg-red-50 p-2 text-xs font-semibold text-red-800" role="alert">
          Alert list refresh failed: {resource.error}.
        </div>
      )}
      {rows.length === 0 ? (
        <div className="text-sm text-slate-400">
          {resource.loading ? "Loading persisted alerts…" : "No alerts recorded for this use case."}
        </div>
      ) : (
        <table className="w-full">
          <thead><tr><Th>State</Th><Th>Lane</Th><Th>Transition</Th><Th>Tick</Th><Th>Opened</Th><Th>Resolved</Th><Th>Delivery</Th></tr></thead>
          <tbody>
            {[...open, ...resolved].map((alert) => (
              <tr key={alert.alert_id} className={alert.resolved_at == null ? "" : "text-slate-400"}>
                <Td>{alert.resolved_at == null ? <HealthChip small health={alert.to_health} /> : <HealthChip small health="Green" title="resolved" />}</Td>
                <Td className="font-semibold">{alert.lane}</Td>
                <Td className="text-xs">{transitionText(alert)}</Td>
                <Td className="font-mono text-xs">{alert.tick ?? "—"}</Td>
                <Td className="text-xs">{formatAge(epochToIso(alert.opened_at))}</Td>
                <Td className="text-xs">{alert.resolved_at == null ? "open" : `${formatAge(epochToIso(alert.resolved_at))} (tick ${alert.resolved_tick ?? "—"})`}</Td>
                <Td className="text-xs">{deliveryText(alert)}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
