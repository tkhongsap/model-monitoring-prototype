/**
 * Development-only entry for the historical baked scenario views.
 * Production never resolves this module; vite.config.ts aliases @app-entry to
 * App.tsx unless CONTROL_TOWER_MODE=demo is explicitly selected.
 */
import { Switch, Route, Router as WouterRouter } from "wouter";
import { SimProvider } from "@/lib/sim";
import { LiveProvider } from "@/lib/live";
import { Masthead, NavTabs, Toasts } from "@/components/Chrome";
import { PlayerBar } from "@/components/PlayerBar";
import Home from "@/pages/home";
import Portfolio from "@/pages/portfolio";
import Pilot from "@/pages/pilot";
import Heatmap from "@/pages/heatmap";
import Gaps from "@/pages/gaps";
import Actions from "@/pages/actions";
import Board from "@/pages/board";
import UseCase from "@/pages/use-case";
import NotFound from "@/pages/not-found";

function Router() {
  return (
    <Switch>
      <Route path="/" component={Home} />
      <Route path="/portfolio" component={Portfolio} />
      <Route path="/pilot" component={Pilot} />
      <Route path="/heatmap" component={Heatmap} />
      <Route path="/gaps" component={Gaps} />
      <Route path="/actions" component={Actions} />
      <Route path="/board" component={Board} />
      <Route path="/use-case/:id" component={UseCase} />
      <Route component={NotFound} />
    </Switch>
  );
}

export default function AppDemo() {
  return (
    <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, "")}>
      <div className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <SimProvider>
          <LiveProvider>
            <Masthead />
            <NavTabs />
            <main className="mx-auto max-w-[1400px] px-4 pb-32 pt-5">
              <Router />
            </main>
            <Toasts />
            <PlayerBar />
          </LiveProvider>
        </SimProvider>
      </div>
    </WouterRouter>
  );
}
