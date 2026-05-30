import { Navigate, NavLink, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { MarketDataProvider } from "./marketData";
import { CalendarRoute } from "./pages/CalendarRoute";
import { FeedRoute } from "./pages/FeedRoute";
import { HealthRoute } from "./pages/HealthRoute";
import { MarketRoute } from "./pages/MarketRoute";
import { NotFoundRoute } from "./pages/NotFoundRoute";
import { PortfolioRoute } from "./pages/PortfolioRoute";
import { ResearchRoute } from "./pages/ResearchRoute";
import { SettingsRoute } from "./pages/SettingsRoute";
import { SourcesRoute } from "./pages/SourcesRoute";
import { SuperinvestorsRoute } from "./pages/SuperinvestorsRoute";
import { ThesisMonitorRoute } from "./pages/ThesisMonitorRoute";
import { TickerRoute } from "./pages/TickerRoute";
import { WatchlistRoute } from "./pages/WatchlistRoute";

type NavItem = {
  to: string;
  label: string;
  end?: boolean;
  aliases?: string[];
};

const navItems: NavItem[] = [
  { to: "/feed", label: "Feed", aliases: ["/", "/dashboard", "/today"] },
  { to: "/watchlist", label: "Watchlist" },
  { to: "/sources", label: "Sources" },
  { to: "/superinvestors", label: "Superinvestors", aliases: ["/filings"] },
  { to: "/market", label: "Market" },
];

export function App() {
  return (
    <MarketDataProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/feed" replace />} />
          <Route path="feed" element={<FeedRoute />} />
          <Route path="today" element={<Navigate to="/feed" replace />} />
          <Route path="dashboard" element={<Navigate to="/feed" replace />} />
          <Route path="watchlist" element={<WatchlistRoute />} />
          <Route path="sources" element={<SourcesRoute />} />
          <Route path="superinvestors" element={<SuperinvestorsRoute />} />
          <Route path="market" element={<MarketRoute />} />
          <Route path="opportunities" element={<Navigate to="/research-queue" replace />} />
          <Route path="portfolio" element={<PortfolioRoute />} />
          <Route path="research" element={<Navigate to="/research-queue" replace />} />
          <Route path="research-queue" element={<ResearchRoute />} />
          <Route path="thesis-monitor" element={<ThesisMonitorRoute />} />
          <Route path="filings" element={<Navigate to="/superinvestors" replace />} />
          <Route path="calendar" element={<CalendarRoute />} />
          <Route path="health" element={<HealthRoute />} />
          <Route path="settings" element={<SettingsRoute />} />
          <Route path="tickers/:symbol" element={<TickerRoute />} />
          <Route path="*" element={<NotFoundRoute />} />
        </Route>
      </Routes>
    </MarketDataProvider>
  );
}

function AppShell() {
  const location = useLocation();
  return (
    <div className="market-terminal">
      <div className="terminal-shell">
        <aside className="sidebar">
          <NavLink className="brand" to="/">
            <strong>Market</strong>
            <small>Portfolio Intelligence</small>
          </NavLink>
          <nav className="side-nav" aria-label="Main navigation">
            {navItems.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => isActive || item.aliases?.includes(location.pathname) ? "active" : ""}>
                <span>{item.label}</span>
              </NavLink>
            ))}
          </nav>
        </aside>
        <main className="desk-main">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
