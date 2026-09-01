import { CalendarClock, Minus, RefreshCw, TrendingDown, TrendingUp } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, MetricTile, PageHeader, StatusBadge } from "@/components/market/workstation";
import { DataFieldStateNotice, missingFieldState } from "@/components/market/dataFieldState";
import { ScopeStatusNotice } from "@/components/market/scopeStatus";
import { cn } from "@/lib/utils";
import type { TodayResponse } from "@/api/panel";
import type { components } from "@/generated/apiSchema";
import type { AppModel } from "@/model";
import type { PanelData, ScopeSnapshotStatus } from "@/types";
import { expressionLabel } from "@/viewModels/expression";
import { formatMoney, formatPct, toneFromText, type Tone } from "./rowFormat";
import { EventScoutPanel } from "./EventScoutPanel";

type TodayPageProps = {
  data: PanelData;
  model: AppModel;
  lastRefresh: Date | null;
  actionQueue: TodayResponse | null;
  actionQueueLoading: boolean;
  actionQueueError: string | null;
  loading: boolean;
  scopeStatus?: ScopeSnapshotStatus;
  onRefresh: () => void;
  onOpenTicker: (symbol: string) => void;
};

type TodayAction = NonNullable<TodayResponse["actions"]>[number];
type TodayBriefItem = components["schemas"]["TodayBriefItemResponse"];
type TodayPreopenBrief = components["schemas"]["TodayPreopenBriefResponse"];

type TodayCategory = {
  key: string;
  title: string;
  subtitle: string;
  tone: Tone;
  dot: string;
};

const todayCategories: TodayCategory[] = [
  { key: "decide_now", title: "Decide now", subtitle: "Candidates, risks, and thesis reviews that want an action today", tone: "warn", dot: "bg-amber-500" },
  { key: "whats_changed", title: "What changed", subtitle: "Fresh source-backed signals on names you own or watch", tone: "info", dot: "bg-blue-600" },
  { key: "catalysts", title: "This week", subtitle: "Scheduled catalysts in the next two weeks", tone: "good", dot: "bg-violet-600" },
  { key: "portfolio_pulse", title: "Portfolio pulse", subtitle: "Biggest movers and concentration in your book", tone: "info", dot: "bg-emerald-600" },
];

export function tradePlanForAction(item: TodayAction) {
  return item.source === "capital_action" ? item.trade_plan ?? null : undefined;
}

const SECTION_BY_KEY: Record<string, TodayCategory> = Object.fromEntries(todayCategories.map((category) => [category.key, category]));
export function TodayPage({ data, model, lastRefresh, actionQueue, actionQueueLoading, actionQueueError, loading, scopeStatus, onRefresh, onOpenTicker }: TodayPageProps) {
  const briefItems = actionQueue?.brief_items ?? [];
  const riskExceptions = actionQueue?.portfolio_risk_items ?? [];
  const decideNow = briefItems.filter((item) => item.category === "decide_now");
  const whatsChanged = briefItems.filter((item) => item.category === "whats_changed");
  const catalysts = briefItems.filter((item) => item.category === "catalysts").slice().sort((a, b) => (a.days_until ?? Number.MAX_SAFE_INTEGER) - (b.days_until ?? Number.MAX_SAFE_INTEGER));
  const hero = decideNow[0] ?? whatsChanged[0] ?? null;
  const pricedHoldings = model.holdings.filter((holding) => holding.hasMarketValue);
  const largestHolding = pricedHoldings.slice().sort((a, b) => b.weight - a.weight)[0];
  const portfolioPnl = model.holdings.reduce((total, holding) => total + holding.unrealizedPnl, 0);
  const portfolioPnlPct = model.portfolioValue ? (portfolioPnl / model.portfolioValue) * 100 : 0;
  const hasBrief = briefItems.length > 0;

  return (
    <section>
      <PageHeader
        eyebrow="Daily decision brief"
        title="Command Center"
        subtitle="The current market stance, book risk, ranked capital actions, position management, critical events, and blockers in one decision surface."
        actions={
          <Button type="button" variant="outline" onClick={onRefresh}>
            <RefreshCw className={loading ? "animate-spin" : ""} />
            Refresh
          </Button>
        }
      />
      <ScopeStatusNotice status={scopeStatus} onRetry={onRefresh} />

      <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricTile
          label="Portfolio P&L"
          value={model.holdings.length ? `${formatMoney(portfolioPnl)} (${formatPct(portfolioPnlPct)})` : "No positions"}
          tone={portfolioPnl >= 0 ? "good" : "bad"}
        />
        <MetricTile label="Decisions due" value={decideNow.length} caption="candidates, risks, thesis reviews" tone={decideNow.length ? "warn" : "good"} />
        <MetricTile label="Source updates" value={whatsChanged.length} caption="fresh signals on owned / watched" tone={whatsChanged.length ? "info" : "muted"} />
        <MetricTile
          label="Top exposure"
          value={largestHolding ? `${largestHolding.ticker} ${largestHolding.weight.toFixed(1)}%` : "None"}
          caption={largestHolding?.nextStep}
          tone={largestHolding && largestHolding.weight > 30 ? "warn" : "info"}
        />
      </div>

      <ActionQueue response={actionQueue} loading={actionQueueLoading} error={actionQueueError} onRefresh={onRefresh} onOpenTicker={onOpenTicker} />
      <PreopenBrief brief={actionQueue?.preopen_brief} />
      <EventScoutPanel truths={data.decisionTruth?.rows ?? []} packets={data.eventDecisionPackets?.rows ?? []} onOpenTicker={onOpenTicker} />

      {hasBrief ? (
        <>
          <div className="grid gap-6">
            <BriefSection section={{ ...SECTION_BY_KEY.portfolio_pulse, title: "Portfolio risk exceptions", subtitle: "The three highest-priority concentration, loss, or thesis-risk exceptions." }} rows={riskExceptions.slice(0, 3)} onOpenTicker={onOpenTicker} columns />
            <CatalystSection section={{ ...SECTION_BY_KEY.catalysts, title: "Catalyst and macro veto", subtitle: "Near-term events and the current deterministic pre-open veto context." }} rows={catalysts.slice(0, 3)} onOpenTicker={onOpenTicker} />
            <details className="rounded-md border border-border bg-card p-4">
              <summary className="cursor-pointer text-sm font-semibold">More daily context</summary>
              <div className="mt-5 grid gap-6">
                <HeroDecision item={hero} onOpenTicker={onOpenTicker} />
                <BriefSection section={SECTION_BY_KEY.decide_now} rows={decideNow} onOpenTicker={onOpenTicker} columns />
                <BriefSection section={SECTION_BY_KEY.whats_changed} rows={whatsChanged} onOpenTicker={onOpenTicker} columns />
              </div>
            </details>
          </div>
        </>
      ) : (
        <EmptyState title="No daily brief loaded" detail="Refresh /today to load decisions, source changes, catalysts, and portfolio moves." />
      )}
    </section>
  );
}

function ActionQueue({ response, loading, error, onRefresh, onOpenTicker }: { response: TodayResponse | null; loading: boolean; error: string | null; onRefresh: () => void; onOpenTicker: (symbol: string) => void }) {
  const items = response?.actions ?? [];
  const missingPlanCount = response?.missing_plan_count ?? 0;
  const unavailable = Boolean(response && !response.status.ready);
  const queueError = error ?? (unavailable ? response?.status.message ?? "Action Queue unavailable." : null);
  return (
    <section className="mb-6" aria-labelledby="action-queue-title">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <h2 id="action-queue-title" className="text-lg font-semibold">Action Queue</h2>
          <p className="text-xs text-muted-foreground">Current capital actions, Inbox transitions, portfolio risks, and decision-blocking research.</p>
        </div>
        {response && !unavailable ? <StatusBadge tone="info">{items.length} shown{missingPlanCount ? ` · ${missingPlanCount} missing plans` : ""}</StatusBadge> : null}
      </div>
      {queueError ? <div role="alert" className="mb-3 flex items-center justify-between gap-3 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"><span>{error && response ? `Showing the last Action Queue. ${error}` : `Action Queue unavailable: ${queueError}`}</span><Button type="button" size="sm" variant="outline" onClick={onRefresh}>Retry</Button></div> : null}
      {loading && !response ? <p role="status" className="rounded-md border border-border bg-card p-4 text-sm text-muted-foreground">Loading Action Queue…</p> : null}
      {missingPlanCount ? <p role="status" className="mb-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">{missingPlanCount} unranked ticker decisions remain CASH / NO TRADE because canonical trade plans are missing.</p> : null}
      {!loading && !queueError && !items.length && !missingPlanCount ? <EmptyState title="Action Queue is clear" detail="No current actionable or transition items are available." /> : null}
      {!unavailable && items.length ? (
        <div className="grid gap-3 lg:grid-cols-3" role="list">
          {items.map((item) => <ActionQueueCard key={item.projection_identity} item={item} onOpenTicker={onOpenTicker} />)}
        </div>
      ) : null}
    </section>
  );
}

export function ActionQueueCard({ item, onOpenTicker }: { item: TodayAction; onOpenTicker: (symbol: string) => void }) {
  const plan = tradePlanForAction(item);
  const tone = plan !== undefined ? "info" : toneFromText(item.lifecycle_state === "actionable" ? item.action : item.lifecycle_state);
  const statusLabel = item.transition ?? item.action ?? item.lifecycle_state;
  const expiry = item.expires_at ? new Date(item.expires_at).toLocaleDateString() : null;
  const ticker = item.ticker;
  return (
    <Card role="listitem" className={cn("min-w-0", toneBorder(tone))}>
      <CardContent className="space-y-3 p-4">
        <div className="flex items-start justify-between gap-2">
          {ticker ? <button type="button" className="font-semibold hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => onOpenTicker(ticker)}>{ticker}</button> : <h3 className="font-semibold">{item.title}</h3>}
          {plan === undefined ? <StatusBadge tone={tone}>{statusLabel}</StatusBadge> : null}
        </div>
        {ticker ? <p className="text-sm font-medium">{item.title}</p> : null}
        {plan !== undefined ? <CompactPlanSummary plan={plan} fieldStates={item.field_states ?? []} /> : (
          <>
            {item.rationale ? <p className="line-clamp-3 text-sm text-muted-foreground">{item.rationale}</p> : null}
            {item.primary_blocker ? <p className="text-xs text-muted-foreground"><span className="font-semibold">Blocker:</span> {item.primary_blocker}</p> : null}
            <p className="text-sm"><span className="font-semibold">Next:</span> {item.next_action}</p>
            {expiry ? <p className="text-xs text-muted-foreground">Expires {expiry}</p> : null}
          </>
        )}
        {item.drill_down ? <a aria-label={`Open ${item.title} drill-down`} className="inline-flex min-h-9 items-center rounded-md border border-input px-3 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" href={item.drill_down}>Open drill-down</a> : null}
      </CardContent>
    </Card>
  );
}

type TradePlan = components["schemas"]["TodayTradePlanSummaryResponse"];

function CompactPlanSummary({ plan, fieldStates }: { plan: TradePlan | null; fieldStates: components["schemas"]["DataFieldStateV1"][] }) {
  if (!plan) {
    const state = fieldStates.find((candidate) => candidate.field === "trade_plan") ?? missingFieldState({
      field: "trade_plan", source: "trade_plan", reason: "trade_plan_missing",
      nextAction: "Refresh the ticker decision and publish its canonical TradePlan.",
    });
    return <div className="rounded-md border border-border p-3 text-sm"><p className="font-semibold">NO TRADE · CASH</p><div className="mt-2"><DataFieldStateNotice state={state} /></div></div>;
  }
  return (
    <div className="rounded-md border border-border p-3 text-sm">
      <p><span className="font-semibold">Action:</span> {plan.action} · <span className="font-semibold">Expression:</span> {expressionLabel(plan.selected_expression_kind)}</p>
      <p className="mt-2 text-muted-foreground"><span className="font-semibold text-foreground">Rationale:</span> {plan.rationale}</p>
      <p className="mt-2"><span className="font-semibold">Next:</span> {plan.next_action}</p>
    </div>
  );
}

function PreopenBrief({ brief }: { brief: TodayPreopenBrief | null | undefined }) {
  if (!brief) return null;
  const bias = brief.bias;
  const events = brief.key_events ?? [];
  const risks = brief.risks ?? [];
  const watchItems = brief.watch_items ?? [];
  const forecastStats = [
    moneyStat("Expected", brief.expected_close),
    moneyStat("Support", brief.support),
    moneyStat("Resistance", brief.resistance),
    pctStat("Move", brief.expected_return_pct),
    pctStat("Backtest MAE", brief.backtest_mae_pct),
    pctStat("Range hit", brief.range_hit_rate_pct),
  ].filter(Boolean) as string[];
  const outcomeStats = [
    moneyStat("Actual mark", brief.actual_price),
    pctStat("Actual move", brief.actual_return_pct),
    pctStat("Error", brief.absolute_error_pct),
  ].filter(Boolean) as string[];

  return (
    <div className="mb-6 rounded-lg border border-border bg-card p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-xs font-semibold uppercase text-muted-foreground">Pre-open macro brief</p>
          <h2 className="mt-1 text-lg font-semibold leading-6">{brief.headline}</h2>
        </div>
        <StatusBadge tone={bias === "bullish" ? "good" : bias === "bearish" ? "bad" : "info"}>{bias}</StatusBadge>
      </div>
      {brief.macro_regime ? <p className="text-sm leading-6 text-muted-foreground">{brief.macro_regime}</p> : null}
      {brief.narrative ? <p className="mt-2 text-sm leading-6 text-foreground">{brief.narrative}</p> : null}
      <div className="mt-3 grid gap-3 lg:grid-cols-[1.15fr_0.85fr]">
        <div className="rounded-md border border-border p-3">
          <p className="text-sm font-semibold">QQQ path</p>
          {forecastStats.length ? <StatRow stats={forecastStats} className="mt-2" /> : null}
          {brief.qqq_path ? <p className="mt-2 text-sm leading-6 text-muted-foreground">{brief.qqq_path}</p> : null}
          {brief.opening_scenario ? <p className="mt-1 text-sm leading-6 text-muted-foreground">{brief.opening_scenario}</p> : null}
          <div className="mt-3 border-t border-border pt-3">
            <p className="text-sm font-semibold">Forecast loop</p>
            <p className="mt-1 text-xs text-muted-foreground">{brief.outcome_status} · observed only when a point-in-time QQQ mark is available.</p>
            {outcomeStats.length ? <StatRow stats={outcomeStats} className="mt-2" /> : null}
            <p className="mt-1 text-xs text-muted-foreground">Range hit: {brief.within_forecast_range === true ? "yes" : brief.within_forecast_range === false ? "no" : "pending"}; direction: {brief.direction_correct === true ? "correct" : brief.direction_correct === false ? "wrong" : "pending"}.</p>
          </div>
        </div>
        <div className="rounded-md border border-border p-3">
          <p className="text-sm font-semibold">Key events</p>
          {events.length ? (
            <ul className="mt-2 space-y-1 text-sm text-muted-foreground">
              {events.slice(0, 4).map((event) => (
                <li key={event} className="leading-5">
                  {event}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-sm text-muted-foreground">No high-priority macro events loaded.</p>
          )}
        </div>
      </div>
      {watchItems.length || risks.length ? (
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <BulletList title="Watch" rows={watchItems} />
          <BulletList title="Risks" rows={risks} />
        </div>
      ) : null}
    </div>
  );
}

function HeroDecision({ item, onOpenTicker }: { item: TodayBriefItem | null; onOpenTicker: (symbol: string) => void }) {
  if (!item) return null;
  const tone = cardTone(item.severity);
  const sentiment = sentimentOf(item.sentiment);
  const stats = item.stats ?? [];
  return (
    <div className={cn("mb-6 rounded-lg border bg-card p-4", toneBorder(tone))}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase text-muted-foreground">Top priority</span>
        <ContextChip context={item.category} sentiment={sentiment} tone={tone} />
      </div>
      <p className="mt-2 flex items-center gap-2 text-lg font-semibold leading-7 text-foreground">
        {sentiment !== "neutral" ? <SentimentMark sentiment={sentiment} /> : null}
        <span className="min-w-0">{item.title}</span>
      </p>
      {stats.length ? <StatRow stats={stats} className="mt-1 text-sm" /> : null}
      {item.summary ? <p className="mt-2 text-sm leading-6 text-muted-foreground">{item.summary}</p> : null}
      {item.antithesis ? <p className="mt-1 text-sm leading-6 text-muted-foreground">Counter: {item.antithesis}</p> : null}
      {item.symbol ? (
        <div className="mt-3">
          <Button type="button" size="sm" onClick={() => onOpenTicker(item.symbol!)}>
            Open {item.symbol}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function SectionHeader({ section, count }: { section: TodayCategory; count: number }) {
  return (
    <div className="mb-3 flex items-center justify-between gap-3 border-b border-border pb-2">
      <div className="flex min-w-0 items-center gap-2">
        <span className={cn("size-2 shrink-0 rounded-full", section.dot)} />
        <div className="min-w-0">
          <h2 className="truncate text-lg font-semibold leading-6">{section.title}</h2>
          <p className="truncate text-xs text-muted-foreground">{section.subtitle}</p>
        </div>
      </div>
      <StatusBadge tone={count ? section.tone : "muted"}>{count}</StatusBadge>
    </div>
  );
}

function BriefSection({ section, rows, onOpenTicker, columns }: { section: TodayCategory; rows: TodayBriefItem[]; onOpenTicker: (symbol: string) => void; columns?: boolean }) {
  return (
    <div className="min-w-0">
      <SectionHeader section={section} count={rows.length} />
      {rows.length ? (
        <div className={cn("grid gap-3", columns && "xl:grid-cols-2")}>
          {rows.map((item) => (
            <TodayBriefCard key={item.stable_key} item={item} onOpenTicker={onOpenTicker} />
          ))}
        </div>
      ) : (
        <EmptyState title="Nothing here" detail={`No ${section.title.toLowerCase()} items right now.`} />
      )}
    </div>
  );
}

function CatalystSection({ section, rows, onOpenTicker }: { section: TodayCategory; rows: TodayBriefItem[]; onOpenTicker: (symbol: string) => void }) {
  return (
    <div className="min-w-0">
      <SectionHeader section={section} count={rows.length} />
      {rows.length ? (
        <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          {rows.map((item) => (
            <CatalystRow key={item.stable_key} item={item} onOpenTicker={onOpenTicker} />
          ))}
        </ul>
      ) : (
        <EmptyState title="No catalysts on your names" detail="Nothing scheduled in the next two weeks for names you own or watch." />
      )}
    </div>
  );
}

function CatalystRow({ item, onOpenTicker }: { item: TodayBriefItem; onOpenTicker: (symbol: string) => void }) {
  const days = item.days_until ?? Number.NaN;
  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <span className={cn("flex w-20 shrink-0 items-center gap-1.5 text-xs font-semibold", Number.isFinite(days) && days <= 1 ? "text-amber-600" : "text-muted-foreground")}>
        <CalendarClock className="size-3.5" aria-hidden="true" />
        {dueLabel(days)}
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium leading-5">{item.title}</p>
        {item.summary ? <p className="truncate text-xs text-muted-foreground">{item.summary}</p> : null}
      </div>
      {item.symbol ? (
        <Button type="button" size="sm" variant="ghost" className="h-7 shrink-0 text-xs" onClick={() => onOpenTicker(item.symbol!)}>
          {item.symbol}
        </Button>
      ) : null}
    </li>
  );
}

function TodayBriefCard({ item, onOpenTicker }: { item: TodayBriefItem; onOpenTicker: (symbol: string) => void }) {
  const tone = cardTone(item.severity);
  const sentiment = sentimentOf(item.sentiment);
  const stats = item.stats ?? [];

  return (
    <Card className={cn("min-w-0 overflow-hidden", toneBorder(tone))}>
      <CardContent className="space-y-2 p-4">
        <div className="flex items-start justify-between gap-2">
          <h3 className="flex min-w-0 items-center gap-1.5 text-sm font-semibold leading-5">
            {sentiment !== "neutral" ? <SentimentMark sentiment={sentiment} /> : null}
            <span className="min-w-0">{item.title}</span>
          </h3>
          <ContextChip context={item.category} sentiment={sentiment} tone={tone} />
        </div>
        {stats.length ? <StatRow stats={stats} /> : null}
        {item.summary ? <p className="text-sm leading-6 text-muted-foreground">{item.summary}</p> : null}
        {item.antithesis ? <p className="text-sm leading-6 text-muted-foreground">Counter: {item.antithesis}</p> : null}
        {item.symbol ? (
          <div className="flex flex-wrap gap-1.5 pt-1">
            <Button type="button" variant="outline" size="sm" className="h-6 px-2 text-xs" onClick={() => onOpenTicker(item.symbol!)}>
              {item.symbol}
            </Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function StatRow({ stats, className }: { stats: string[]; className?: string }) {
  return (
    <div className={cn("flex flex-wrap items-center gap-x-2 gap-y-1 text-xs font-medium tabular-nums text-foreground/80", className)}>
      {stats.map((stat, index) => (
        <span key={index} className="flex items-center gap-2">
          {index > 0 ? <span className="text-muted-foreground/50" aria-hidden="true">·</span> : null}
          {stat}
        </span>
      ))}
    </div>
  );
}

function BulletList({ title, rows }: { title: string; rows: string[] }) {
  if (!rows.length) return null;
  return (
    <div className="rounded-md border border-border p-3">
      <p className="text-sm font-semibold">{title}</p>
      <ul className="mt-2 space-y-1 text-sm text-muted-foreground">
        {rows.slice(0, 4).map((row, index) => (
          <li key={index} className="leading-5">{row}</li>
        ))}
      </ul>
    </div>
  );
}

function moneyStat(label: string, value: unknown): string | null {
  const parsed = typeof value === "number" ? value : typeof value === "string" ? Number(value) : Number.NaN;
  return Number.isFinite(parsed) ? `${label} ${formatMoney(parsed)}` : null;
}

function pctStat(label: string, value: unknown): string | null {
  const parsed = typeof value === "number" ? value : typeof value === "string" ? Number(value) : Number.NaN;
  return Number.isFinite(parsed) ? `${label} ${formatPct(parsed)}` : null;
}

function ContextChip({ context, sentiment, tone }: { context: string; sentiment: Sentiment; tone: Tone }) {
  if (!context) return sentiment !== "neutral" ? <SentimentMark sentiment={sentiment} /> : null;
  const owned = context.toLowerCase().startsWith("owned");
  return <StatusBadge tone={owned ? tone : "muted"}>{context}</StatusBadge>;
}

type Sentiment = "bullish" | "bearish" | "neutral";

function SentimentMark({ sentiment }: { sentiment: Sentiment }) {
  if (sentiment === "neutral") return <Minus className="size-4 text-muted-foreground" aria-label="Neutral" />;
  const bullish = sentiment === "bullish";
  const Icon = bullish ? TrendingUp : TrendingDown;
  return <Icon className={cn("size-4 shrink-0", bullish ? "text-emerald-600" : "text-red-600")} aria-label={bullish ? "Bullish" : "Bearish"} />;
}

function sentimentOf(value: string): Sentiment {
  value = value.toLowerCase();
  if (value === "bullish" || value === "good") return "bullish";
  if (value === "bearish" || value === "bad" || value === "sell") return "bearish";
  return "neutral";
}

function cardTone(value: string): Tone {
  return toneFromText(value);
}

function toneBorder(tone: Tone): string {
  if (tone === "bad") return "border-red-200";
  if (tone === "warn") return "border-amber-200";
  if (tone === "good") return "border-emerald-200";
  return "border-border";
}

function dueLabel(days: number): string {
  if (!Number.isFinite(days)) return "Scheduled";
  if (days <= 0) return "Today";
  if (days === 1) return "Tomorrow";
  return `${days}d`;
}
