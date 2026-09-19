import {
  Database,
  Eye,
  HeartPulse,
  Home,
  Landmark,
  Menu,
  MessageCircle,
  Mic,
  RefreshCw,
  Search,
} from "lucide-react";
import { useEffect, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
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
import { loadContextualAssistantPacket, type ContextualAssistantPacket } from "@/api/panel";
import { BuildMismatchBanner } from "@/components/market/BuildIdentity";
import type { Tone } from "@/ui/tone";

type NavItem = {
  to: string;
  label: string;
  icon: typeof Home;
  end?: boolean;
  aliases?: string[];
};

export const navItems: NavItem[] = [
  { to: "/today", label: "Today", icon: Home, aliases: ["/", "/dashboard"] },
  { to: "/market", label: "Market", icon: Database },
  { to: "/opportunities", label: "Opportunities", icon: Eye, aliases: ["/watchlist"] },
  { to: "/portfolio", label: "Real portfolio", icon: Landmark, end: true },
  { to: "/portfolio/paper", label: "Paper trading", icon: Landmark },
  { to: "/research", label: "Research", icon: Mic, aliases: ["/sources", "/research-queue"] },
];

export function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const { model, loading, lastRefresh } = useMarketData();
  const [query, setQuery] = useState("");
  const [mobileOpen, setMobileOpen] = useState(false);
  const widePage = location.pathname.startsWith("/watchlist") || location.pathname.startsWith("/opportunities") || location.pathname.startsWith("/options-radar") || location.pathname.startsWith("/options-chain");

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
    <div className="min-h-screen overflow-x-hidden bg-background text-foreground">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 border-r border-border bg-card lg:block">
        <ShellBrand />
        <ScrollArea className="h-[calc(100vh-85px)] px-3 py-4">{nav}</ScrollArea>
      </aside>
      <div className="min-w-0 lg:pl-64">
        <header className="sticky top-0 z-20 border-b border-border bg-background/95 backdrop-blur">
          <div className="flex min-h-16 items-center gap-3 px-3 sm:px-4 lg:px-6">
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
              <Input className="pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ticker or symbol" aria-label="Search tickers" />
            </form>

            <ContextualAgentDrawer />

            {location.pathname.startsWith("/health") ? (
              <div className="ml-auto hidden items-center gap-2 text-xs text-muted-foreground md:flex">
                <SourceHealthBadge />
                <Separator orientation="vertical" className="h-5" />
                <span className="flex items-center gap-1">
                  <RefreshCw className={cn("size-3.5", loading && "animate-spin")} />
                  {lastRefresh ? lastRefresh.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : model.latestHealthCheck || "Not loaded"}
                </span>
              </div>
            ) : null}
          </div>
          <BuildMismatchBanner />
        </header>
        <main className={cn("mx-auto min-w-0 overflow-x-hidden px-3 py-4 sm:px-4 lg:py-6", widePage ? "max-w-none lg:px-3" : "max-w-[1720px] lg:px-6")}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function ContextualAgentDrawer() {
  const location = useLocation();
  const { model } = useMarketData();
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState("");
  const [query, setQuery] = useState("");
  const [topic, setTopic] = useState("decision");
  const [packet, setPacket] = useState<ContextualAssistantPacket | null>(null);
  const [packetError, setPacketError] = useState<string | null>(null);
  const tickerMatch = location.pathname.match(/^\/tickers\/([^/]+)/i);
  const ticker = tickerMatch ? decodeURIComponent(tickerMatch[1]).toUpperCase() : selected;
  useEffect(() => {
    setPacket(null); setPacketError(null);
    if (!open || !ticker) return;
    const controller = new AbortController();
    void loadContextualAssistantPacket(ticker, controller.signal).then(value => {
      if (!controller.signal.aborted && value.ticker === ticker) setPacket(value);
    }).catch(error => { if (!controller.signal.aborted) setPacketError(error instanceof Error ? error.message : "Packet unavailable."); });
    return () => controller.abort();
  }, [ticker, open]);
  const answer = packet?.explanations?.find(item => item.topic === topic);
  return <Sheet open={open} onOpenChange={setOpen}>
    <SheetTrigger asChild><Button type="button" variant="outline" size="icon" aria-label="Open contextual agent drawer" title="Explain the current decision"><MessageCircle /></Button></SheetTrigger>
    <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-lg">
      <SheetHeader><SheetTitle>Decision evidence</SheetTitle><SheetDescription>Explain the frozen decision and its missing inputs. These are recorded facts, not a new trade recommendation.</SheetDescription></SheetHeader>
      <div className="space-y-4 py-5 text-sm">
        {!tickerMatch ? <><form className="flex gap-2" onSubmit={event => { event.preventDefault(); const value = query.trim().toUpperCase(); if (/^[A-Z0-9.^=-]{1,16}$/.test(value)) { setSelected(value); setTopic("decision"); } }}><Input aria-label="Ticker to explain" placeholder="Ticker to explain" value={query} onChange={e => setQuery(e.target.value)} /><Button type="submit">Open</Button></form>{model.holdings.length ? <div className="flex flex-wrap gap-2">{model.holdings.slice(0, 8).map(holding => <button key={holding.ticker} className="rounded border px-2 py-1 text-xs hover:bg-accent" onClick={() => { setSelected(holding.ticker); setQuery(holding.ticker); }}>{holding.ticker}</button>)}</div> : null}</> : null}
        {packetError ? <p role="alert" className="text-destructive">{packetError}</p> : null}
        {!packet && ticker && !packetError ? <p role="status">Loading {ticker}'s recorded decision…</p> : null}
        {!ticker ? <p className="text-muted-foreground">Choose a ticker from this page or enter a symbol to inspect its decision.</p> : null}
        {packet ? <>
          <div className="rounded-lg border bg-muted/30 p-3"><strong>{packet.ticker}</strong><p className="mt-1 text-xs text-muted-foreground">Decision as of {packet.as_of ? new Date(packet.as_of).toLocaleString() : "not recorded"} · revision {packet.decision_revision ?? "unavailable"}</p></div>
          <div role="tablist" aria-label="Decision questions" className="flex flex-wrap gap-2">{packet.explanations?.map(item => <button key={item.topic} role="tab" aria-selected={topic === item.topic} onClick={() => setTopic(item.topic)} className={cn("rounded-md border px-3 py-2 text-xs", topic === item.topic && "bg-primary text-primary-foreground")}>{item.question}</button>)}</div>
          {answer ? <div role="tabpanel" className="rounded-lg border p-4"><p className="whitespace-pre-wrap leading-6">{answer.answer}</p><p className="mt-3 break-all text-xs text-muted-foreground">{answer.citation_ids.map(id => `[${id}]`).join(" ") || "No usable decision citation in this packet."}</p></div> : <p>The server has not supplied an explanation for this packet.</p>}
          {packet.missing_evidence.length ? <p className="rounded border border-amber-300 p-3 text-amber-800 dark:text-amber-200">Missing: {packet.missing_evidence.join(", ")}. The explanation cannot substitute for this evidence.</p> : null}
          <details><summary className="cursor-pointer text-xs font-medium">Packet and sources</summary><p className="mt-2 break-all text-xs">{packet.packet_id}</p><ul className="mt-2 space-y-1 text-xs text-muted-foreground">{packet.citations.map(citation => <li key={citation.id}>{citation.label}{citation.available ? "" : " · not available"}</li>)}</ul></details>
          <Button asChild variant="outline" className="w-full"><Link onClick={() => setOpen(false)} to={`/tickers/${encodeURIComponent(packet.ticker)}`}>Open complete decision and trade plan</Link></Button>
        </> : null}
        <Button asChild className="w-full"><Link onClick={() => setOpen(false)} to={`/agent?context=${encodeURIComponent(location.pathname)}${packet ? `&ticker=${encodeURIComponent(packet.ticker)}&packet_id=${encodeURIComponent(packet.packet_id)}` : ""}`}>Request additional research</Link></Button>
        <p className="text-xs text-muted-foreground">Advisory only. Research requests are reviewed separately; this drawer cannot place orders or change risk limits.</p>
      </div>
    </SheetContent>
  </Sheet>;
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
        const active = pathname === item.to || item.aliases?.includes(pathname) || (!item.end && item.to !== "/" && pathname.startsWith(`${item.to}/`));
        return (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            onClick={onNavigate}
            className={cn(
              "flex h-11 items-center gap-3 rounded-md px-3 text-sm font-medium text-muted-foreground no-underline transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              active && "bg-primary text-primary-foreground hover:bg-primary hover:text-primary-foreground",
            )}
          >
            <item.icon className="size-4" />
            <span>{item.label}</span>
          </NavLink>
        );
      })}
      <div className="mt-4 space-y-1 border-t border-border pt-3" aria-label="Operations">
        <Link to="/agent" onClick={onNavigate} className="flex h-11 items-center gap-3 rounded-md px-3 text-sm text-muted-foreground hover:bg-accent"><MessageCircle className="size-4" />Agent controls</Link>
        <Link to="/health" onClick={onNavigate} className="flex h-11 items-center gap-3 rounded-md px-3 text-sm text-muted-foreground hover:bg-accent"><HeartPulse className="size-4" />System health</Link>
      </div>
    </nav>
  );
}

export function PageHeader({ eyebrow, title, subtitle, actions }: { eyebrow?: string; title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <header className="mb-4 flex flex-col gap-3 border-b border-border pb-4 md:flex-row md:items-end md:justify-between">
      <div className="min-w-0">
        {eyebrow && <p className="mb-1 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">{eyebrow}</p>}
        <h1 className="text-2xl font-semibold tracking-normal text-foreground text-balance md:text-3xl">{title}</h1>
        {subtitle && <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">{subtitle}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function MetricTile({ label, value, caption, tone = "info" }: { label: string; value: ReactNode; caption?: string; tone?: Tone }) {
  const longValue = typeof value === "string" && value.length > 26;
  return (
    <Card className={cn("min-w-0", toneSurface(tone))}>
      <div className="p-4 pb-1">
        <div className="flex items-center gap-2 truncate text-xs font-medium uppercase text-muted-foreground">
          <span className={cn("size-1.5 shrink-0 rounded-full", toneDot(tone))} />
          {label}
        </div>
      </div>
      <CardContent className="p-4 pt-0">
        <div className={cn("line-clamp-2 min-h-[1.75rem] break-words font-semibold leading-tight", longValue ? "text-lg xl:text-xl" : "text-xl xl:text-2xl")}>{value}</div>
        {caption && <p className="mt-1 line-clamp-2 break-words text-xs leading-5 text-muted-foreground">{caption}</p>}
      </CardContent>
    </Card>
  );
}

export function StatusBadge({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  const variant = tone === "good" ? "success" : tone === "warn" ? "warning" : tone === "bad" ? "destructive" : tone === "info" ? "info" : "outline";
  return <Badge variant={variant}>{children}</Badge>;
}

export type DecisionCardProps = {
  title: ReactNode;
  status?: ReactNode;
  reason?: ReactNode;
  evidence?: ReactNode;
  nextAction?: ReactNode;
  symbols?: string[];
  tone?: Tone;
};

export function DecisionCard({ title, status, reason, evidence, nextAction, symbols, tone = "info" }: DecisionCardProps) {
  return (
    <Card className={cn("overflow-hidden", toneSurface(tone))}>
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

export function ClickableDecisionCard({ enabled, onOpen, ...cardProps }: DecisionCardProps & { enabled: boolean; onOpen: () => void }) {
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!enabled) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpen();
    }
  };

  return (
    <div
      role={enabled ? "button" : undefined}
      tabIndex={enabled ? 0 : -1}
      aria-disabled={enabled ? undefined : true}
      className={cn("block w-full text-left transition-transform focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2", enabled ? "cursor-pointer hover:-translate-y-px" : "cursor-default")}
      onClick={() => {
        if (enabled) onOpen();
      }}
      onKeyDown={onKeyDown}
    >
      <DecisionCard {...cardProps} />
    </div>
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

export function DataTableFrame({ title, children, action }: { title?: ReactNode; children: ReactNode; action?: ReactNode }) {
  return (
    <Card className="overflow-hidden">
      {(title || action) && (
        <div className="flex flex-col gap-3 border-b border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          {title && <h2 className="shrink-0 text-lg font-semibold">{title}</h2>}
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
  const { model, loading, lastRefresh } = useMarketData();
  const sourceStates = Object.values(model.sources);
  const available = sourceStates.filter((state) => state === "live").length;
  const tone = criticalDataCoverageTone(sourceStates, loading);
  return <StatusBadge tone={tone}>{loading ? "Checking critical data" : `Critical data: ${available}/${sourceStates.length} available`}</StatusBadge>;
}

export function criticalDataCoverageTone(sourceStates: string[], loading: boolean): Tone {
  if (loading) return "info";
  return sourceStates.length > 0 && sourceStates.every((state) => state === "live") ? "good" : "warn";
}

function toneSurface(tone: Tone) {
  return {
    good: "border-border border-l-green-500 bg-green-50/15",
    warn: "border-border border-l-amber-500 bg-amber-50/25",
    bad: "border-border border-l-red-500 bg-red-50/25",
    info: "border-border bg-card",
    muted: "border-border bg-card",
  }[tone];
}

function toneDot(tone: Tone) {
  return {
    good: "bg-green-600",
    warn: "bg-amber-500",
    bad: "bg-red-600",
    info: "bg-blue-600",
    muted: "bg-muted-foreground",
  }[tone];
}
