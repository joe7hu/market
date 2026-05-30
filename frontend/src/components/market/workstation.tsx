import {
  Activity,
  AlertTriangle,
  BarChart3,
  BookOpenCheck,
  CalendarDays,
  Database,
  Eye,
  FileSearch,
  HeartPulse,
  Home,
  Landmark,
  Menu,
  Mic,
  RefreshCw,
  Search,
  Settings,
  UsersRound,
} from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { useMarketData } from "@/marketData";

type Tone = "good" | "warn" | "bad" | "info" | "muted";

type NavItem = {
  to: string;
  label: string;
  icon: typeof Home;
  end?: boolean;
  aliases?: string[];
};

const navItems: NavItem[] = [
  { to: "/today", label: "Today", icon: Home, aliases: ["/", "/dashboard"] },
  { to: "/feed", label: "Feed", icon: Activity },
  { to: "/watchlist", label: "Watchlist", icon: Eye },
  { to: "/portfolio", label: "Portfolio", icon: Landmark },
  { to: "/research-queue", label: "Research", icon: BookOpenCheck, aliases: ["/research", "/opportunities"] },
  { to: "/thesis-monitor", label: "Theses", icon: AlertTriangle },
  { to: "/superinvestors", label: "Superinvestors", icon: UsersRound, aliases: ["/filings"] },
  { to: "/calendar", label: "Calendar", icon: CalendarDays },
  { to: "/sources", label: "Sources", icon: Mic },
  { to: "/market", label: "Market", icon: BarChart3 },
  { to: "/health", label: "Health", icon: HeartPulse },
  { to: "/settings", label: "Settings", icon: Settings },
];

export function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const { model, loading, lastRefresh } = useMarketData();
  const [query, setQuery] = useState("");
  const [mobileOpen, setMobileOpen] = useState(false);

  const onSearch = (event: FormEvent) => {
    event.preventDefault();
    const symbol = query.trim().toUpperCase();
    if (symbol) {
      navigate(`/tickers/${encodeURIComponent(symbol)}`);
      setQuery("");
      setMobileOpen(false);
    }
  };

  const nav = <MainNav pathname={location.pathname} onNavigate={() => setMobileOpen(false)} />;

  return (
    <div className="min-h-screen bg-background text-foreground">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 border-r border-border bg-card lg:block">
        <ShellBrand />
        <ScrollArea className="h-[calc(100vh-85px)] px-3 py-4">{nav}</ScrollArea>
      </aside>
      <div className="lg:pl-64">
        <header className="sticky top-0 z-20 border-b border-border bg-background/95 backdrop-blur">
          <div className="flex min-h-14 items-center gap-3 px-3 sm:px-4 lg:px-6">
            <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
              <SheetTrigger asChild>
                <Button type="button" variant="outline" size="icon" className="lg:hidden" aria-label="Open navigation">
                  <Menu />
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-72 p-0">
                <SheetHeader className="border-b border-border px-4 py-4">
                  <SheetTitle>Market</SheetTitle>
                  <SheetDescription className="sr-only">Primary Market workstation navigation</SheetDescription>
                </SheetHeader>
                <ScrollArea className="h-[calc(100vh-73px)] px-3 py-4">{nav}</ScrollArea>
              </SheetContent>
            </Sheet>

            <form onSubmit={onSearch} className="relative min-w-0 flex-1 sm:max-w-md" role="search">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input className="h-9 pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ticker or symbol" aria-label="Search tickers" />
            </form>

            <div className="ml-auto hidden items-center gap-2 text-xs text-muted-foreground md:flex">
              <SourceHealthBadge />
              <Separator orientation="vertical" className="h-5" />
              <span className="flex items-center gap-1">
                <RefreshCw className={cn("size-3.5", loading && "animate-spin")} />
                {lastRefresh ? lastRefresh.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : model.latestHealthCheck || "Not loaded"}
              </span>
            </div>
          </div>
        </header>
        <main className="mx-auto w-full max-w-[1720px] px-3 py-4 sm:px-4 lg:px-6 lg:py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function ShellBrand() {
  return (
    <Link className="flex h-[85px] flex-col justify-center border-b border-border px-5 no-underline" to="/today">
      <span className="text-lg font-semibold tracking-normal text-foreground">Market</span>
      <span className="text-xs font-medium text-muted-foreground">Investment workstation</span>
    </Link>
  );
}

function MainNav({ pathname, onNavigate }: { pathname: string; onNavigate: () => void }) {
  return (
    <nav className="space-y-1" aria-label="Main navigation">
      {navItems.map((item) => {
        const active = pathname === item.to || item.aliases?.includes(pathname) || (item.to !== "/" && pathname.startsWith(`${item.to}/`));
        return (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            onClick={onNavigate}
            className={cn(
              "flex h-9 items-center gap-3 rounded-md px-3 text-sm font-medium text-muted-foreground no-underline transition-colors hover:bg-accent hover:text-accent-foreground",
              active && "bg-primary text-primary-foreground hover:bg-primary hover:text-primary-foreground",
            )}
          >
            <item.icon className="size-4" />
            <span>{item.label}</span>
          </NavLink>
        );
      })}
    </nav>
  );
}

export function PageHeader({ eyebrow, title, subtitle, actions }: { eyebrow?: string; title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <header className="mb-4 flex flex-col gap-3 border-b border-border pb-4 md:flex-row md:items-end md:justify-between">
      <div className="min-w-0">
        {eyebrow && <p className="mb-1 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">{eyebrow}</p>}
        <h1 className="text-2xl font-semibold tracking-normal text-foreground md:text-3xl">{title}</h1>
        {subtitle && <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">{subtitle}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function MetricTile({ label, value, caption, tone = "info" }: { label: string; value: ReactNode; caption?: string; tone?: Tone }) {
  return (
    <Card className={cn("min-w-0", toneBorder(tone))}>
      <CardHeader className="p-3 pb-1">
        <CardTitle className="truncate text-xs font-medium uppercase text-muted-foreground">{label}</CardTitle>
      </CardHeader>
      <CardContent className="p-3 pt-0">
        <div className="truncate text-2xl font-semibold">{value}</div>
        {caption && <p className="mt-1 truncate text-xs text-muted-foreground">{caption}</p>}
      </CardContent>
    </Card>
  );
}

export function StatusBadge({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  const variant = tone === "good" ? "success" : tone === "warn" ? "warning" : tone === "bad" ? "destructive" : tone === "info" ? "info" : "outline";
  return <Badge variant={variant}>{children}</Badge>;
}

export function DecisionCard({ title, status, reason, evidence, nextAction, symbols, tone = "info" }: { title: string; status?: ReactNode; reason?: ReactNode; evidence?: ReactNode; nextAction?: ReactNode; symbols?: string[]; tone?: Tone }) {
  return (
    <Card className={cn("overflow-hidden", toneBorder(tone))}>
      <CardHeader className="flex-row items-start justify-between gap-3 p-4 pb-2">
        <CardTitle className="text-base leading-6">{title}</CardTitle>
        {status && <div className="shrink-0">{status}</div>}
      </CardHeader>
      <CardContent className="space-y-3 p-4 pt-0 text-sm">
        {reason && <DecisionLine label="Reason">{reason}</DecisionLine>}
        {evidence && <DecisionLine label="Evidence">{evidence}</DecisionLine>}
        {nextAction && <DecisionLine label="Next">{nextAction}</DecisionLine>}
        {symbols?.length ? (
          <div className="flex flex-wrap gap-1.5">
            {symbols.map((symbol) => <StatusBadge key={symbol} tone="muted">{symbol}</StatusBadge>)}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function DecisionLine({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-1 sm:grid-cols-[88px_1fr]">
      <span className="text-xs font-semibold uppercase text-muted-foreground">{label}</span>
      <div className="min-w-0 leading-6 text-foreground">{children}</div>
    </div>
  );
}

export function EvidenceList({ items }: { items: ReactNode[] }) {
  if (!items.length) {
    return <span className="text-muted-foreground">No evidence linked</span>;
  }
  return (
    <ul className="space-y-1">
      {items.map((item, index) => <li key={index} className="leading-6">{item}</li>)}
    </ul>
  );
}

export function DataTableFrame({ title, children, action }: { title?: string; children: ReactNode; action?: ReactNode }) {
  return (
    <Card className="overflow-hidden">
      {(title || action) && (
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          {title && <h2 className="text-sm font-semibold">{title}</h2>}
          {action}
        </div>
      )}
      <div className="overflow-x-auto">{children}</div>
    </Card>
  );
}

export function Toolbar({ children }: { children: ReactNode }) {
  return <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card p-2">{children}</div>;
}

export function EmptyState({ title, detail, icon: Icon = Database }: { title: string; detail: string; icon?: typeof Database }) {
  return (
    <Card>
      <CardContent className="flex min-h-40 flex-col items-center justify-center p-6 text-center">
        <Icon className="mb-3 size-8 text-muted-foreground" />
        <h2 className="text-base font-semibold">{title}</h2>
        <p className="mt-1 max-w-md text-sm leading-6 text-muted-foreground">{detail}</p>
      </CardContent>
    </Card>
  );
}

export function SourceHealthBadge() {
  const { model } = useMarketData();
  const tone: Tone = model.sources.health === "live" ? "good" : "warn";
  return <StatusBadge tone={tone}>{model.sources.health === "live" ? "Sources loaded" : "Sources pending"}</StatusBadge>;
}

function toneBorder(tone: Tone) {
  return {
    good: "border-l-4 border-l-green-600",
    warn: "border-l-4 border-l-amber-500",
    bad: "border-l-4 border-l-red-600",
    info: "border-l-4 border-l-blue-600",
    muted: "border-l-4 border-l-border",
  }[tone];
}
