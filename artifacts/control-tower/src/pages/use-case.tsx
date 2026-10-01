/** Use-case drill-down (PRD D.8): every signal vs its band, the real engine
 * artifacts behind each grade, and the action history. */
import { useParams, useRouter } from "@/lib/nav";
import { useState } from "react";
import {
  CartesianGrid, Legend as RLegend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { AlertsPanel } from "@/components/alerts";
import { CadenceBadge, GreyChip, HealthChip, ScrollBox, Td, Th, TierBadge } from "@/components/ui";
import {
  formatAge,
  formatLag,
  provenanceText,
  shortId,
  stateLabel,
  stateTone,
  SyncSnapshot,
  syncStateOf,
  useLive,
  useLiveResource,
} from "@/lib/live";

export default function UseCase() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { connection, effectiveState } = useLive();
  const resource = useLiveResource<any>(id ? `/api/live/use-case/${id}` : "");
  const uc = resource.data;
  const [tab, setTab] = useState(0);
  if (!uc) return (
    <div className={`rounded-lg border p-8 ${resource.error ? "border-red-300 bg-red-50 text-red-800" : "border-slate-200 bg-white text-slate-400"}`}>
      {resource.error ?? "Loading persisted observation…"}
    </div>
  );
  const isML = uc.lane_kind === "ml";
  const tabs = isML
    ? ["Drift (Evidently)", "Performance (NannyML)", "Explainability (LIME / SHAP)"]
    : ["Judge scores"];
  const deep = (uc.signals || []).length > 0;
  const sync = uc as SyncSnapshot;
  const syncState = resource.error || connection === "error"
    ? "error"
    : syncStateOf(sync.sync_state) ?? syncStateOf(sync.state) ?? effectiveState;
  const digest = sync.content_sha256 ?? sync.window_digest;

  return (
    <div>
      <button className="mb-2 text-xs font-semibold text-slate-400 hover:text-[#E60012]" onClick={() => router.push("/heatmap")}>
        Heatmap › {uc.use_case_name}
      </button>
      {resource.error && (
        <div className="mb-3 rounded border border-red-300 bg-red-50 p-2 text-sm font-semibold text-red-800" role="alert">
          Detail refresh failed: {resource.error}. Showing the last successfully fetched observation from {formatAge(resource.lastSuccessfulFetchAt)}.
        </div>
      )}
      {/* header */}
      <div className="mb-4 rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-extrabold">{uc.use_case_name}</h1>
          <span className="font-mono text-xs text-slate-400">{uc.registry_id} · source: {uc.source_record_id}</span>
          <span className="rounded bg-slate-800 px-2 py-0.5 text-[10px] font-bold text-white">{uc.status}</span>
          <TierBadge tier={uc.risk_tier} />
          <HealthChip health={uc.current_health} />
          <GreyChip text={`telemetry: ${stateLabel(syncState).toLowerCase()}`} />
          <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold ${stateTone(syncState)}`}>{stateLabel(syncState)}</span>
          <CadenceBadge tier={uc.risk_tier} />
        </div>
        <div className="mt-2 grid gap-x-6 gap-y-0.5 text-xs text-slate-600 md:grid-cols-2">
          <div><b>Owners (roles):</b> business — {uc.business_owner} · technical — {uc.technical_owner} · monitoring — {uc.monitoring_owner}</div>
          <div><b>System owner:</b> {uc.system_owner}</div>
          <div><b>Platform:</b> {uc.platform_or_app} · <b>Model:</b> {uc.model_or_route}</div>
          <div><b>Data:</b> <span title={uc.data_sources}>{String(uc.data_sources).slice(0, 60)}…</span> · reviewed {uc.last_reviewed} · next {uc.next_review}</div>
        </div>
      </div>

      <div className="mb-4 rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
        <h2 className="mb-2 text-xs font-bold uppercase tracking-wider text-slate-600">Observation evidence</h2>
        <dl className="grid gap-x-4 gap-y-1 text-xs sm:grid-cols-[auto_1fr_auto_1fr]">
          <dt className="font-semibold text-slate-500">Window ID</dt><dd className="truncate font-mono" title={sync.window_id ?? undefined}>{shortId(sync.window_id, 18)}</dd>
          <dt className="font-semibold text-slate-500">POC batch</dt><dd className="truncate font-mono" title={sync.batch_id ?? undefined}>{shortId(sync.batch_id, 18)}</dd>
          <dt className="font-semibold text-slate-500">Content digest</dt><dd className="truncate font-mono" title={digest ?? undefined}>{shortId(digest, 18)}</dd>
          <dt className="font-semibold text-slate-500">Observation ID</dt><dd className="truncate font-mono" title={sync.observation_id ?? undefined}>{shortId(sync.observation_id, 18)}</dd>
          <dt className="font-semibold text-slate-500">Model version</dt><dd className="font-mono">{sync.model_version ?? uc.model_version ?? "—"}</dd>
          <dt className="font-semibold text-slate-500">Producer → observed</dt><dd className="font-mono">{sync.source_tick ?? sync.producer_tick ?? "—"} → {sync.observed_tick ?? uc.tick ?? "—"}</dd>
          <dt className="font-semibold text-slate-500">Records</dt><dd className="font-mono">{sync.record_count ?? "—"}</dd>
          <dt className="font-semibold text-slate-500">Provenance</dt><dd>{provenanceText(sync)}</dd>
          <dt className="font-semibold text-slate-500">Last successful pull</dt><dd>{formatAge(sync.last_success_at)}</dd>
          <dt className="font-semibold text-slate-500">Source → monitor lag</dt><dd className="font-mono">{formatLag(sync.source_lag_ms)}</dd>
        </dl>
      </div>

      <AlertsPanel uc={uc.registry_id ?? id ?? ""} />

      {/* signals */}
      <ScrollBox>
        <table className="w-full">
          <thead><tr><Th>Signal</Th><Th>Lane</Th><Th>Value</Th><Th>Band (Green / Red)</Th><Th>Health</Th><Th>Trend</Th></tr></thead>
          <tbody>
            {(uc.signals || []).map((s: any) => (
              <tr key={s.key} id={encodeURIComponent(s.lane)}>
                <Td className="font-semibold">{s.label}
                  {s.provenance === "Sheet-3 (inherited)" && <span className="ml-1 rounded bg-slate-800 px-1 py-0.5 text-[8px] font-bold text-white">SHEET-3</span>}
                </Td>
                <Td className="text-xs text-slate-500">{s.lane}</Td>
                <Td className="font-mono">{fmt(s.value, s)}{s.pending_reason && <span className="ml-1 text-[10px] italic text-slate-400">({s.pending_reason})</span>}</Td>
                <Td className="text-xs text-slate-500">{band(s)}</Td>
                <Td><HealthChip health={s.pending_reason ? "Unknown" : s.health} striped={!!s.pending_reason} title={s.pending_reason || undefined} /></Td>
                <Td><Spark history={s.history} spec={s} /></Td>
              </tr>
            ))}
            {!deep && (
              <tr><td colSpan={6} className="p-4 text-center text-sm text-slate-400">
                No measured signals were reported for this observation. Evidence note: {uc.evidence_note || "—"}
              </td></tr>
            )}
          </tbody>
        </table>
      </ScrollBox>

      {/* tabs */}
      {deep && (
        <div className="mt-5">
          <div className="flex gap-1 border-b border-slate-200">
            {tabs.map((t, i) => (
              <button key={t} onClick={() => setTab(i)}
                className={`px-3 py-2 text-sm font-semibold ${tab === i ? "border-b-2 border-[#E60012] text-[#E60012]" : "text-slate-500"}`}>
                {t}
              </button>
            ))}
          </div>
          <div className="mt-3">
            {isML ? <MLTabs uc={uc} tab={tab} windowNumber={uc.observed_tick ?? uc.tick ?? 0} /> : <LLMTabs uc={uc} />}
          </div>
        </div>
      )}

      {/* NBA offer mix */}
      {uc.offer_mix && <OfferMixPanel uc={uc} />}

    </div>
  );
}

function fmt(v: any, s: any) {
  if (v === null || v === undefined) return "—";
  if (s.unit === "fraction" || s.key === "pii_exposure_rate") return `${(v * 100).toFixed(1)}%`;
  if (s.key === "data_drift_share") return v.toFixed(3);
  if (s.unit === "seconds") return `${v.toFixed(1)}s`;
  return v.toFixed(3);
}

function band(s: any) {
  const g = s.direction === "lower_is_better" ? `≤ ${disp(s.green_bar, s)}` : `≥ ${disp(s.green_bar, s)}`;
  const r = s.direction === "lower_is_better" ? `≥ ${disp(s.red_bar, s)}` : `< ${disp(s.red_bar, s)}`;
  return `Green ${g} · Red ${r}`;
}
function disp(v: number, s: any) {
  if (s.unit === "fraction") return `${(v * 100).toFixed(0)}%`;
  if (s.unit === "seconds") return `${v}s`;
  return String(v);
}

function Spark({ history, spec }: { history: any[]; spec: any }) {
  const pts = (history || []).filter((h) => h.value !== null);
  if (pts.length < 2) return <span className="text-[10px] italic text-slate-300">accumulating…</span>;
  const vals = pts.map((p) => p.value);
  const min = Math.min(...vals, spec.red_bar ?? Infinity) * 0.98;
  const max = Math.max(...vals, spec.red_bar ?? -Infinity) * 1.02;
  const W = 90, H = 24;
  const x = (i: number) => (i / (pts.length - 1)) * W;
  const y = (v: number) => H - ((v - min) / (max - min || 1)) * H;
  const color = { Green: "#00A66C", Amber: "#FFB000", Red: "#E60012", Unknown: "#8A8F98" }[pts[pts.length - 1].health as string] || "#64748b";
  return (
    <svg width={W} height={H} className="overflow-visible">
      {spec.red_bar != null && (
        <line x1={0} x2={W} y1={y(spec.red_bar)} y2={y(spec.red_bar)} stroke="#E60012" strokeDasharray="3 2" strokeWidth={0.8} />
      )}
      <polyline fill="none" stroke={color} strokeWidth={1.6}
        points={pts.map((p, i) => `${x(i)},${y(p.value)}`).join(" ")} />
    </svg>
  );
}

function ArtifactFrame({ id, height = 620 }: { id?: string; height?: number }) {
  if (!id) return <div className="rounded border border-slate-200 bg-slate-50 p-6 text-sm text-slate-400">Artifact not available for this tick.</div>;
  return <iframe src={`/api/live/artifacts/${id}`} className="w-full rounded border border-slate-200 bg-white" style={{ height }} />;
}

function MLTabs({ uc, tab, windowNumber }: { uc: any; tab: number; windowNumber: number }) {
  const est = uc.signals?.find((s: any) => s.key === "estimated_roc_auc");
  const real = uc.signals?.find((s: any) => s.key === "realized_roc_auc");
  const chart = (est?.history || []).map((h: any) => ({
    tick: h.tick, estimated: h.value,
    realized: (real?.history || []).find((r: any) => r.tick === h.tick)?.value ?? null,
  }));
  if (tab === 0) return (
    <div>
      <p className="mb-2 text-sm"><b>Drifted features ({(uc.drifted_features || []).length}):</b>{" "}
        {(uc.drifted_features || []).join(", ") || "none reported"}</p>
      <ArtifactFrame id={uc.artifacts?.evidently_html} />
      <p className="mt-1 text-[11px] text-slate-400">Full Evidently report for observed window {windowNumber}</p>
    </div>
  );
  if (tab === 1) return chart.length < 2 ? (
    <div className="rounded border border-slate-200 bg-slate-50 p-6 text-sm italic text-slate-400">
      Performance history accumulating… (needs at least 2 observed windows)
    </div>
  ) : (
    <div>
      <div className="h-72 w-full rounded border border-slate-200 bg-white p-2">
        <ResponsiveContainer>
          <LineChart data={chart}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
            <XAxis dataKey="tick" fontSize={11} label={{ value: "observed window", position: "insideBottom", offset: -2, fontSize: 10 }} />
            <YAxis domain={[0.65, 0.95]} fontSize={11} />
            <Tooltip />
            <RLegend />
            <ReferenceLine y={0.72} stroke="#E60012" strokeDasharray="4 3" label={{ value: "Red bar 0.72", fontSize: 10, fill: "#E60012" }} />
            <ReferenceLine y={0.80} stroke="#00A66C" strokeDasharray="4 3" label={{ value: "Green bar 0.80", fontSize: 10, fill: "#00A66C" }} />
            {uc.reference_auc && <ReferenceLine y={uc.reference_auc} stroke="#94a3b8" strokeDasharray="2 4" label={{ value: `reference ${uc.reference_auc}`, fontSize: 10, fill: "#94a3b8" }} />}
            <Line type="monotone" dataKey="estimated" stroke="#0f172a" strokeWidth={2} dot={false} name="Estimated (NannyML, label-free)" />
            <Line type="monotone" dataKey="realized" stroke="#E60012" strokeWidth={2} strokeDasharray="6 3" dot={{ r: 2 }} name="Realized (delayed labels)" connectNulls={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1 text-[11px] text-slate-500">
        NannyML estimates production performance <b>before</b> ground-truth labels arrive — the early warning the
        Drift/Quality lanes rely on. Under covariate shift the estimate is <b>early but conservative</b>: it sags
        label-free while the realized line (3-tick label lag) confirms the damage is worse. Model v{uc.model_version}.
      </p>
    </div>
  );
  return (
    <div>
      {uc.lime_instance?.index !== undefined && (
        <p className="mb-2 text-sm">
          LIME explanation for the highest-risk instance (row {uc.lime_instance.index}
          {uc.lime_instance.churn_probability != null && <> · predicted probability {(uc.lime_instance.churn_probability * 100).toFixed(0)}%</>}).
        </p>
      )}
      <div className="grid gap-3 lg:grid-cols-2">
        <div><ArtifactFrame id={uc.artifacts?.lime_html} height={420} /></div>
        <div>
          {uc.artifacts?.shap_png
            // eslint-disable-next-line @next/next/no-img-element
            ? <img src={`/api/live/artifacts/${uc.artifacts.shap_png}`} alt="SHAP global importance" className="w-full rounded border border-slate-200 bg-white" />
            : <div className="rounded border border-slate-200 bg-slate-50 p-6 text-sm text-slate-400">SHAP not available.</div>}
          <p className="mt-1 text-[11px] text-slate-500">SHAP global feature importance (model v{uc.model_version}).</p>
        </div>
      </div>
      <p className="mt-2 rounded border border-amber-200 bg-amber-50 p-2 text-[11px] text-amber-800">
        LIME explanations are local and can be unstable (fidelity-vs-simplicity trade-off). SHAP is the more
        consistent, game-theoretic counterpart — and what the enterprise path (Azure ML Responsible AI dashboard) uses.
      </p>
    </div>
  );
}

function LLMTabs({ uc }: { uc: any }) {
  return (
    <div>
      <div className="mb-2 rounded border border-slate-300 bg-slate-100 p-2 text-[11px] font-bold text-slate-600">
        {uc.judge
          ? `JUDGE — ${uc.judge}. Each sampled turn is scored for groundedness, relevance, hallucination and PII.`
          : "Judge evidence unavailable — affected quality and safety signals remain Unknown."}
      </div>
      <ScrollBox>
        <table className="w-full">
          <thead><tr><Th>Trace</Th><Th>Ground.</Th><Th>Relev.</Th><Th>Halluc.</Th><Th>PII</Th><Th>Latency</Th></tr></thead>
          <tbody>
            {(uc.judge_sample || []).map((r: any, i: number) => (
              <tr key={i} className={r.hallucination ? "bg-red-50" : ""}>
                <Td className="max-w-[260px] truncate font-mono text-xs" title={r.trace_id}>{r.trace_id}</Td>
                <Td className="font-mono">{r.groundedness}</Td>
                <Td className="font-mono">{r.relevance}</Td>
                <Td>{r.hallucination ? <span className="font-bold text-[#E60012]">YES</span> : "no"}</Td>
                <Td>{r.pii ? "YES" : "no"}</Td>
                <Td className="font-mono">{r.latency_s}s</Td>
              </tr>
            ))}
          </tbody>
        </table>
      </ScrollBox>
    </div>
  );
}

function OfferMixPanel({ uc }: { uc: any }) {
  const offers = Object.keys(uc.offer_mix || {});
  const acc = (uc.signals || []).find((s: any) => typeof s.key === "string" && s.key.includes("acceptance"));
  return (
    <div className="mt-6">
      <h2 className="mb-2 text-sm font-bold uppercase tracking-wider text-slate-600">Offer mix vs baseline</h2>
      <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
        {offers.length === 0 && <div className="text-sm text-slate-400">No offer mix reported for this window.</div>}
        {offers.map((o) => {
          const cur = uc.offer_mix[o] ?? 0;
          const base = uc.baseline_offer_mix?.[o] ?? 0;
          return (
            <div key={o} className="mb-2 text-xs">
              <div className="mb-0.5 flex justify-between">
                <span className="font-semibold">{o}</span>
                <span className="font-mono text-slate-500">{(cur * 100).toFixed(0)}% <span className="text-slate-300">vs {(base * 100).toFixed(0)}% base</span></span>
              </div>
              <div className="relative h-2 w-full rounded bg-slate-100">
                <div className="absolute inset-y-0 left-0 rounded bg-[#E60012]" style={{ width: `${Math.min(100, cur * 100)}%` }} />
                <div className="absolute inset-y-0 w-0.5 bg-slate-600" style={{ left: `${Math.min(100, base * 100)}%` }} title={`baseline ${(base * 100).toFixed(0)}%`} />
              </div>
            </div>
          );
        })}
        {acc && (
          <p className="mt-2 text-xs text-slate-600">
            <b>Acceptance rate:</b> {acc.value != null ? `${(acc.value * 100).toFixed(1)}%` : "—"}
            {acc.pending_reason && <span className="ml-1 italic text-slate-400">({acc.pending_reason})</span>}
            {uc.acceptance_pending_reason && <span className="ml-1 italic text-slate-400">({uc.acceptance_pending_reason})</span>}
          </p>
        )}
      </div>
    </div>
  );
}
