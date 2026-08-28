import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/market/workstation";
import { RouteErrorBoundary } from "@/components/RouteErrorBoundary";
import { MarketDataProvider } from "./marketData";
import { AgentRoute } from "./pages/AgentRoute";
import { CalendarRoute } from "./pages/CalendarRoute";
import { HealthRoute } from "./pages/HealthRoute";
import { MarketRoute } from "./pages/MarketRoute";
import { NotFoundRoute } from "./pages/NotFoundRoute";
import { OptionsRadarRoute } from "./pages/OptionsRadarRoute";
import { PortfolioRoute } from "./pages/PortfolioRoute";
import { SettingsRoute } from "./pages/SettingsRoute";
import { SourcesRoute } from "./pages/SourcesRoute";
import { SuperinvestorsRoute } from "./pages/SuperinvestorsRoute";
import { ThesisMonitorRoute } from "./pages/ThesisMonitorRoute";
import { TickerRoute } from "./pages/TickerRoute";
import { TodayRoute } from "./pages/TodayRoute";
import { WatchlistRoute } from "./pages/WatchlistRoute";

const OptionsChainRoute = lazy(async () => ({ default: (await import("./pages/OptionsChainRoute")).OptionsChainRoute }));

export function App() {
  return (
    <MarketDataProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/today" replace />} />
          <Route path="feed" element={<Navigate to="/today" replace />} />
          <Route path="today" element={<TodayRoute />} />
          <Route path="dashboard" element={<Navigate to="/today" replace />} />
          <Route path="watchlist" element={<WatchlistRoute />} />
          <Route path="sources" element={<SourcesRoute />} />
          <Route path="superinvestors" element={<SuperinvestorsRoute />} />
          <Route path="market" element={<MarketRoute />} />
          <Route path="opportunities" element={<Navigate to="/watchlist" replace />} />
          <Route path="portfolio" element={<PortfolioRoute />} />
          <Route path="research" element={<Navigate to="/sources" replace />} />
          <Route path="research-queue" element={<Navigate to="/sources" replace />} />
          <Route path="options-radar" element={<OptionsRadarRoute />} />
          <Route path="options-chain" element={<Suspense fallback={<p className="text-sm text-muted-foreground">Loading option-chain workstation…</p>}><OptionsChainRoute /></Suspense>} />
          <Route path="inbox" element={<Navigate to="/today" replace />} />
          <Route path="thesis-monitor" element={<ThesisMonitorRoute />} />
          <Route path="theses" element={<ThesisMonitorRoute />} />
          <Route path="filings" element={<Navigate to="/superinvestors" replace />} />
          <Route path="calendar" element={<CalendarRoute />} />
          <Route path="agent" element={<AgentRoute />} />
          <Route path="health" element={<RouteErrorBoundary route="/health" failedApis={["/api/panel-snapshot?scope=health", "/api/options/history/health"]}><HealthRoute /></RouteErrorBoundary>} />
          <Route path="settings" element={<SettingsRoute />} />
          <Route path="tickers/:symbol" element={<TickerRoute />} />
          <Route path="*" element={<NotFoundRoute />} />
        </Route>
      </Routes>
    </MarketDataProvider>
  );
}
