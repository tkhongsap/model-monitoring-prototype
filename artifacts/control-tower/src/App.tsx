import { Switch, Route, Router as WouterRouter } from "wouter";
import { lazy, Suspense } from "react";
import { LiveProvider } from "@/lib/live";
import { LiveMasthead, LiveNavTabs } from "@/components/ChromeLive";
import { PlayerBar } from "@/components/PlayerBar";
import Home from "@/pages/home";
import Heatmap from "@/pages/heatmap";
import NotFound from "@/pages/not-found";

const UseCase = lazy(() => import("@/pages/use-case"));

function Router() {
  return (
    <Switch>
      <Route path="/" component={Home} />
      <Route path="/heatmap" component={Heatmap} />
      <Route path="/use-case/:id" component={UseCase} />
      <Route component={NotFound} />
    </Switch>
  );
}

function App() {
  return (
    <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, "")}>
      <div className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <LiveProvider>
          <LiveMasthead />
          <LiveNavTabs />
          <main className="mx-auto max-w-[1400px] px-4 pb-32 pt-5">
            <Suspense fallback={<div className="rounded-lg border border-slate-200 bg-white p-8 text-sm text-slate-500">Loading observation detail…</div>}>
              <Router />
            </Suspense>
          </main>
          <PlayerBar />
        </LiveProvider>
      </div>
    </WouterRouter>
  );
}

export default App;
