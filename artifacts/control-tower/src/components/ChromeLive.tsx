import Link, { usePathname } from "@/lib/nav";
import { formatAge, shortId, stateLabel, stateTone, useLive, useLiveResource } from "@/lib/live";

const TABS = [
  { href: "/", label: "At A Glance" },
  { href: "/heatmap", label: "Heatmap" },
];

export function LiveMasthead() {
  const { effectiveState, lastSuccessfulFetchAt, error } = useLive();
  const version = useLiveResource<{
    monitor_git_sha?: string;
    build_sha?: string;
    contract_version?: string;
    producer?: { git_sha?: string | null; status?: string };
  }>("/api/version");
  const state = stateLabel(effectiveState);

  return (
    <header
      className="flex flex-wrap items-center gap-3 px-4 py-2.5 text-white"
      style={{ background: "linear-gradient(120deg, #101010 0%, #1a1a1a 55%, #E60012 130%)" }}
    >
      <span className="rounded-full bg-white px-2.5 py-0.5 text-sm font-extrabold lowercase tracking-tight text-[#E60012]">true</span>
      <span className="text-sm font-bold">AI Use Case Observability Control Tower</span>
      <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold tracking-wider ${stateTone(effectiveState)}`}>
        {state}
      </span>
      <span className="ml-auto text-right text-[10px] text-slate-200">
        {error ? error : `Monitor API last reached ${formatAge(lastSuccessfulFetchAt)}`}
      </span>
      <span className="font-mono text-[10px] text-slate-300"
        title={`monitor ${version.data?.monitor_git_sha ?? version.data?.build_sha ?? "unknown"} · producer ${version.data?.producer?.git_sha ?? "unknown"}`}>
        monitor {shortId(version.data?.monitor_git_sha ?? version.data?.build_sha, 10)} · producer {shortId(version.data?.producer?.git_sha, 10)} · contract {version.data?.contract_version ?? "—"}
      </span>
      <span className="rounded-full bg-[#E60012] px-2.5 py-0.5 text-[10px] font-extrabold tracking-wide">
        CPG Confidential · Provenance-labelled telemetry
      </span>
    </header>
  );
}

export function LiveNavTabs() {
  const path = usePathname();
  return (
    <nav className="flex gap-1 border-b border-slate-200 bg-white px-3">
      {TABS.map((tab) => {
        const active = tab.href === "/" ? path === "/" : path.startsWith(tab.href);
        return (
          <Link
            key={tab.href}
            href={tab.href}
            className={`px-3 py-2 text-sm font-semibold ${active
              ? "border-b-2 border-[#E60012] text-[#E60012]"
              : "text-slate-600 hover:text-slate-900"}`}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
