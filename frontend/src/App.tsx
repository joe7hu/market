import {
  CalendarDays,
  ChevronRight,
  ClipboardList,
  FileSearch,
  HeartPulse,
  Home,
  Layers3,
  Settings,
  Sparkles,
  Sun,
  UserRound,
} from "lucide-react";
import { NavLink, Outlet, Route, Routes } from "react-router-dom";
import { MarketDataProvider } from "./marketData";
import { CalendarRoute } from "./pages/CalendarRoute";
import { DashboardRoute } from "./pages/DashboardRoute";
import { FilingsRoute } from "./pages/FilingsRoute";
import { HealthRoute } from "./pages/HealthRoute";
import { NotFoundRoute } from "./pages/NotFoundRoute";
import { OpportunitiesRoute } from "./pages/OpportunitiesRoute";
import { PortfolioRoute } from "./pages/PortfolioRoute";
import { ResearchRoute } from "./pages/ResearchRoute";
import { SettingsRoute } from "./pages/SettingsRoute";
import { TickerRoute } from "./pages/TickerRoute";

const navItems = [
  { to: "/", label: "Dashboard", icon: <Home size={15} />, end: true },
  { to: "/opportunities", label: "Opportunities", icon: <Sparkles size={15} /> },
  { to: "/portfolio", label: "Portfolio", icon: <Layers3 size={15} /> },
  { to: "/research", label: "Research", icon: <FileSearch size={15} /> },
  { to: "/filings", label: "Trader Filings", icon: <ClipboardList size={15} /> },
  { to: "/calendar", label: "Calendar", icon: <CalendarDays size={15} /> },
  { to: "/health", label: "Health", icon: <HeartPulse size={15} /> },
  { to: "/settings", label: "Settings", icon: <Settings size={15} /> },
];

export function App() {
  return (
    <MarketDataProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<DashboardRoute />} />
          <Route path="opportunities" element={<OpportunitiesRoute />} />
          <Route path="portfolio" element={<PortfolioRoute />} />
          <Route path="research" element={<ResearchRoute />} />
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
  return (
    <div className="terminal-shell">
      <aside className="sidebar">
        <NavLink className="brand" to="/">
          <span className="brand-mark">M</span>
          <span>
            <strong>market</strong>
            <small>Decision Desk</small>
          </span>
        </NavLink>
        <nav className="side-nav" aria-label="Main navigation">
          {navItems.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end}>
              {item.icon}
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <button type="button">
            <Sun size={15} />
            <span>Light</span>
          </button>
          <button type="button">
            <UserRound size={15} />
            <span>Joe Hu</span>
            <ChevronRight size={13} />
          </button>
        </div>
      </aside>
      <main className="desk-main">
        <Outlet />
      </main>
    </div>
  );
}
