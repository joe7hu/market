import { useState, type FormEvent } from "react";
import { Activity, Eye, LifeBuoy, Mic, Rss, Search, Sun, UsersRound } from "lucide-react";
import { Navigate, NavLink, Outlet, Route, Routes, useLocation, useNavigate } from "react-router-dom";
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
  icon: typeof Rss;
  end?: boolean;
  aliases?: string[];
};

const navItems: NavItem[] = [
  { to: "/feed", label: "Feed", icon: Rss, aliases: ["/", "/dashboard", "/today"] },
  { to: "/watchlist", label: "Watchlist", icon: Eye },
  { to: "/superinvestors", label: "Superinvestors", icon: UsersRound, aliases: ["/filings"] },
  { to: "/sources", label: "Sources", icon: Mic },
  { to: "/market", label: "Market Valuation", icon: Activity },
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
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const onSearch = (event: FormEvent) => {
    event.preventDefault();
    const symbol = query.trim().toUpperCase();
    if (symbol) {
      navigate(`/tickers/${symbol}`);
      setQuery("");
    }
  };
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
                <item.icon size={16} />
                <span>{item.label}</span>
              </NavLink>
            ))}
          </nav>
          <div className="sidebar-utility">
            <span><LifeBuoy size={15} /> Contact support</span>
            <span><Sun size={15} /> Light Mode</span>
          </div>
        </aside>
        <main className="desk-main">
          <header className="market-topbar">
            <form onSubmit={onSearch} className="ticker-search" role="search">
              <Search size={16} />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search tickers..." aria-label="Search tickers" />
              <kbd>⌘K</kbd>
            </form>
            <button type="button" className="signin-pill">Sign in</button>
          </header>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
