import { Navigate, NavLink, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { MarketDataProvider, useMarketData } from "./marketData";
import { CalendarRoute } from "./pages/CalendarRoute";
import { FilingsRoute } from "./pages/FilingsRoute";
import { HealthRoute } from "./pages/HealthRoute";
import { NotFoundRoute } from "./pages/NotFoundRoute";
import { PortfolioRoute } from "./pages/PortfolioRoute";
import { ResearchRoute } from "./pages/ResearchRoute";
import { SettingsRoute } from "./pages/SettingsRoute";
import { ThesisMonitorRoute } from "./pages/ThesisMonitorRoute";
import { TickerRoute } from "./pages/TickerRoute";
import { TodayRoute } from "./pages/TodayRoute";

type NavItem = {
  to: string;
  label: string;
  end?: boolean;
  aliases?: string[];
};

const navItems: NavItem[] = [
  { to: "/today", label: "Today", aliases: ["/", "/dashboard"] },
  { to: "/portfolio", label: "Portfolio Risk" },
  { to: "/research-queue", label: "Research Queue", aliases: ["/research", "/opportunities"] },
  { to: "/thesis-monitor", label: "Thesis Monitor" },
  { to: "/filings", label: "Filings" },
  { to: "/calendar", label: "Calendar" },
  { to: "/health", label: "Health" },
  { to: "/settings", label: "Settings" },
];

const fallbackTapeItems = [
  { symbol: "INDEX_SPX", price: "5,137.08", change: 0.8 },
  { symbol: "INDEX_NDX", price: "18,302.91", change: -1.14 },
  { symbol: "CRYP_BTC", price: "62,410.00", change: 2.1 },
  { symbol: "FX_EURUSD", price: "1.0837", change: 0.32 },
  { symbol: "COMM_OIL", price: "79.97", change: -0.42 },
];

export function App() {
  return (
    <MarketDataProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/today" replace />} />
          <Route path="today" element={<TodayRoute />} />
          <Route path="dashboard" element={<Navigate to="/today" replace />} />
          <Route path="opportunities" element={<Navigate to="/research-queue" replace />} />
          <Route path="portfolio" element={<PortfolioRoute />} />
          <Route path="research" element={<Navigate to="/research-queue" replace />} />
          <Route path="research-queue" element={<ResearchRoute />} />
          <Route path="thesis-monitor" element={<ThesisMonitorRoute />} />
          <Route path="filings" element={<FilingsRoute />} />
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
  const { model, lastRefresh, loading } = useMarketData();
  const location = useLocation();
  const active = [...navItems].reverse().find((item) => location.pathname === item.to || item.aliases?.includes(location.pathname) || (!item.end && location.pathname.startsWith(item.to)));
  const loadedSources = Object.values(model.sources).filter((state) => state === "live").length;
  const tapeItems = [
    ...model.watchlist.map((item) => ({ symbol: item.symbol, price: item.price, change: item.change })),
    ...fallbackTapeItems,
  ].slice(0, 6);
  return (
    <div className="market-terminal">
      <header className="ticker-tape" aria-label="Market tape">
        {tapeItems.map((item, index) => (
          <span key={`${item.symbol}-${index}`} className={item.change < 0 ? "negative" : "positive"}>
            <b>{item.symbol}</b>
            <strong>{item.price}</strong>
            <em>{formatTapeChange(item.change)}</em>
          </span>
        ))}
      </header>
      <div className="terminal-shell">
        <aside className="sidebar">
          <NavLink className="brand" to="/">
            <strong>TER<br />MNL.</strong>
          </NavLink>
          <nav className="side-nav" aria-label="Main navigation">
            {navItems.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => isActive || item.aliases?.includes(location.pathname) ? "active" : ""}>
                <span>{item.label}</span>
              </NavLink>
            ))}
          </nav>
          <div className="market-clocks" aria-label="Exchange clocks">
            <span><b>NY</b><time>{zoneTime("America/New_York")}</time></span>
            <span><b>LDN</b><time>{zoneTime("Europe/London")}</time></span>
            <span><b>TOK</b><time>{zoneTime("Asia/Tokyo")}</time></span>
          </div>
          <div className="sidebar-footer">
            <span>SYS.OP.{loading ? "SYNC" : "NORMAL"}</span>
            <span>LATENCY: {lastRefresh ? "12MS" : "IDLE"}</span>
            <span>CONN: {loadedSources ? "SECURE_WS" : "LOCAL_API"}</span>
            <span>VIEW: {active?.label.toUpperCase() ?? "TICKER"}</span>
          </div>
        </aside>
        <main className="desk-main">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function formatTapeChange(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function zoneTime(timeZone: string): string {
  const parts = new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone,
    timeZoneName: "short",
  }).formatToParts(new Date());
  const hour = parts.find((part) => part.type === "hour")?.value ?? "--";
  const minute = parts.find((part) => part.type === "minute")?.value ?? "--";
  const zone = parts.find((part) => part.type === "timeZoneName")?.value ?? "";
  return `${hour}:${minute} ${zone}`;
}
