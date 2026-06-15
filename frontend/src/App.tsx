import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/market/workstation";
import { MarketDataProvider } from "./marketData";
import { AgentRoute } from "./pages/AgentRoute";
import { CalendarRoute } from "./pages/CalendarRoute";
import { FeedRoute } from "./pages/FeedRoute";
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

export function App() {
  return (
    <MarketDataProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/today" replace />} />
          <Route path="feed" element={<FeedRoute />} />
          <Route path="today" element={<TodayRoute />} />
          <Route path="dashboard" element={<Navigate to="/today" replace />} />
          <Route path="watchlist" element={<WatchlistRoute />} />
          <Route path="sources" element={<SourcesRoute />} />
          <Route path="superinvestors" element={<SuperinvestorsRoute />} />
          <Route path="market" element={<MarketRoute />} />
          <Route path="opportunities" element={<Navigate to="/watchlist" replace />} />
          <Route path="portfolio" element={<PortfolioRoute />} />
          <Route path="research" element={<Navigate to="/watchlist" replace />} />
          <Route path="research-queue" element={<Navigate to="/watchlist" replace />} />
          <Route path="options-radar" element={<OptionsRadarRoute />} />
          <Route path="thesis-monitor" element={<ThesisMonitorRoute />} />
          <Route path="filings" element={<Navigate to="/superinvestors" replace />} />
          <Route path="calendar" element={<CalendarRoute />} />
          <Route path="agent" element={<AgentRoute />} />
          <Route path="health" element={<HealthRoute />} />
          <Route path="settings" element={<SettingsRoute />} />
          <Route path="tickers/:symbol" element={<TickerRoute />} />
          <Route path="*" element={<NotFoundRoute />} />
        </Route>
      </Routes>
    </MarketDataProvider>
  );
}
