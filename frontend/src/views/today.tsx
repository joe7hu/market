import { useState } from "react";
import { setDecisionInboxState } from "@/api/options";
import { InboxStateControls, InboxUsefulnessControls, type InboxStateChange } from "./decisionInbox";
import { CalendarClock, Minus, RefreshCw, TrendingDown, TrendingUp } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, PageHeader, StatusBadge } from "@/components/market/workstation";
import { DataFieldStateNotice, decisionReason, missingFieldState } from "@/components/market/dataFieldState";
import { ScopeStatusNotice } from "@/components/market/scopeStatus";
import { cn } from "@/lib/utils";
import type { TodayResponse } from "@/api/panel";
import type { components } from "@/generated/apiSchema";
import type { AppModel } from "@/model";
import type { PanelData, ScopeSnapshotStatus } from "@/types";
import { expressionLabel } from "@/viewModels/expression";
import { formatMoney, formatPct, toneFromText, type Tone } from "@/shared/rowFormat";
import { EventScoutPanel } from "./EventScoutPanel";
import { statusLabel as semanticStatusLabel } from "@/presentation/labels";

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
type TodayBriefCategory = components["schemas"]["TodayBriefCategoryResponse"];
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
  const categoryStates = Object.fromEntries((actionQueue?.brief_categories ?? []).map((category) => [category.category, category]));
  const decideNow = briefItems.filter((item) => item.category === "decide_now");
  const whatsChanged = briefItems.filter((item) => item.category === "whats_changed");
  const catalysts = briefItems.filter((item) => item.category === "catalysts").slice().sort((a, b) => (a.days_until ?? Number.MAX_SAFE_INTEGER) - (b.days_until ?? Number.MAX_SAFE_INTEGER));
  const portfolioPulse = briefItems.filter((item) => item.category === "portfolio_pulse");
  const hasBrief = Boolean(actionQueue);

  return (
    <section>
      <PageHeader
        eyebrow="Daily decision brief"
        title="Command Center"
        subtitle="Your holding risks, current research and next decisions."
        actions={
          <Button type="button" variant="outline" onClick={onRefresh}>
            <RefreshCw className={loading ? "animate-spin" : ""} />
            Refresh
          </Button>
        }
      />
      <ScopeStatusNotice status={scopeStatus} onRetry={onRefresh} />

      {hasBrief ? <div className="grid gap-8">
        <section aria-labelledby="today-do-now"><h2 id="today-do-now" className="mb-3 text-xl font-semibold">Do now</h2><ActionQueue response={actionQueue} loading={actionQueueLoading} error={actionQueueError} onRefresh={onRefresh} onOpenTicker={onOpenTicker} /><BriefSection section={SECTION_BY_KEY.decide_now} rows={decideNow} category={categoryStates.decide_now} onOpenTicker={onOpenTicker} columns /></section>
        <section aria-labelledby="today-changed"><h2 id="today-changed" className="mb-3 text-xl font-semibold">What changed</h2><PreopenBrief brief={actionQueue?.preopen_brief} /><div className="grid gap-6"><BriefSection section={SECTION_BY_KEY.whats_changed} rows={whatsChanged} category={categoryStates.whats_changed} onOpenTicker={onOpenTicker} columns /><CatalystSection section={{ ...SECTION_BY_KEY.catalysts, title: "Catalysts", subtitle: "Near-term events that can change a decision." }} rows={catalysts} category={categoryStates.catalysts} onOpenTicker={onOpenTicker} /></div></section>
        <section aria-labelledby="today-system"><div className="mb-3 flex items-end justify-between gap-3"><div><h2 id="today-system" className="text-xl font-semibold">System</h2><p className="text-sm text-muted-foreground">Only the conditions that change today’s trust or action.</p></div><a className="text-sm font-medium text-primary hover:underline" href="/health">Open system health →</a></div><PortfolioPerformanceSummary summary={data.portfolioSummaryDto} /><div className="grid gap-6"><BriefSection section={{ ...SECTION_BY_KEY.portfolio_pulse, title: "Portfolio risk", subtitle: "Concentration, loss, and thesis exceptions." }} rows={riskExceptions.slice(0, 3)} onOpenTicker={onOpenTicker} columns /><BriefSection section={SECTION_BY_KEY.portfolio_pulse} rows={portfolioPulse} category={categoryStates.portfolio_pulse} onOpenTicker={onOpenTicker} columns /></div><details className="mt-4 rounded-md border border-border p-4"><summary className="cursor-pointer text-sm font-semibold">Event research</summary><EventScoutPanel truths={data.decisionTruth?.rows ?? []} packets={data.eventDecisionPackets?.rows ?? []} onOpenTicker={onOpenTicker} /></details></section>
      </div> : <EmptyState title="No daily brief loaded" detail="Refresh Today to load decisions, source changes, catalysts, and portfolio risks." />}
    </section>
  );
}

function PortfolioPerformanceSummary({ summary }: { summary: PanelData["portfolioSummaryDto"] }) {
  if (!summary || summary.availability !== "complete" || summary.total_pnl == null || summary.total_pnl_pct == null) return null;
  return (
    <Card className="mb-6 border-blue-200 bg-blue-50/30">
      <CardContent className="flex flex-wrap items-center justify-between gap-4 p-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">Portfolio snapshot</p>
          <p className="mt-1 text-sm font-medium">Return on invested capital</p>
        </div>
        <p className={cn("text-2xl font-semibold tabular-nums", summary.total_pnl >= 0 ? "text-emerald-700" : "text-red-700")}>
          {formatMoney(summary.total_pnl)} ({formatPct(summary.total_pnl_pct)})
        </p>
      </CardContent>
    </Card>
  );
}

function ActionQueue({ response, loading, error, onRefresh, onOpenTicker }: { response: TodayResponse | null; loading: boolean; error: string | null; onRefresh: () => void; onOpenTicker: (symbol: string) => void }) {
  const [showAll, setShowAll] = useState(false);
  const sorted = (response?.actions ?? []).slice().sort((a, b) => Number(b.source === "portfolio_risk") - Number(a.source === "portfolio_risk"));
  const items = showAll ? sorted : sorted.slice(0, 10);
  const missingPlanCount = response?.missing_plan_count ?? 0;
  const unavailable = Boolean(response && !response.status.ready);
  const queueError = error ?? (unavailable ? response?.status.message ?? "Action Queue unavailable." : null);
  return (
    <section className="mb-6" aria-labelledby="action-queue-title">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <h2 id="action-queue-title" className="text-lg font-semibold">Action Queue</h2>
          <p className="text-xs text-muted-foreground">Current holding risks and decisions. Open a ticker to review its evidence.</p>
        </div>
        {response && !unavailable ? <StatusBadge tone="info">{items.length} shown{missingPlanCount ? ` · ${missingPlanCount} missing plans` : ""}</StatusBadge> : null}
      </div>
      {queueError ? <div role="alert" className="mb-3 flex items-center justify-between gap-3 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"><span>{error && response ? `Showing the last Action Queue. ${error}` : `Action Queue unavailable: ${queueError}`}</span><Button type="button" size="sm" variant="outline" onClick={onRefresh}>Retry</Button></div> : null}
      {loading && !response ? <p role="status" className="rounded-md border border-border bg-card p-4 text-sm text-muted-foreground">Loading Action Queue…</p> : null}
      {missingPlanCount ? <p role="status" className="mb-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">{missingPlanCount} unranked ticker decisions remain CASH / NO TRADE because canonical trade plans are missing.</p> : null}
      {!loading && !queueError && !items.length && !missingPlanCount ? <EmptyState title="Action Queue is clear" detail="No current actionable or transition items are available." /> : null}
      {!unavailable && items.length ? (
        <div className="grid gap-3 lg:grid-cols-3" role="list">
          {items.map((item) => <ActionQueueCard key={item.projection_identity} item={item} onOpenTicker={onOpenTicker} onRefresh={onRefresh} />)}
          {sorted.length > 10 ? <Button variant="outline" onClick={() => setShowAll(!showAll)}>{showAll ? "Show top ten" : `Show all ${sorted.length} current items`}</Button> : null}
        </div>
      ) : null}
    </section>
  );
}

export function ActionQueueCard({ item, onOpenTicker, onRefresh }: { item: TodayAction; onOpenTicker: (symbol: string) => void; onRefresh?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function updateState(itemId: string, body: InboxStateChange) {
    setBusy(true);
    setError(null);
    try {
      await setDecisionInboxState(itemId, body);
      onRefresh?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Action could not be saved.");
    } finally {
      setBusy(false);
    }
  }
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
          {plan === undefined ? <StatusBadge tone={tone}>{decisionReason(statusLabel)}</StatusBadge> : null}
        </div>
        {ticker ? <p className="text-sm font-medium">{item.title.replace(/capital action$/i, "trade assessment")}</p> : null}
        {plan !== undefined ? <CompactPlanSummary plan={plan} fieldStates={item.field_states ?? []} /> : (
          <>
            {item.rationale ? <p className="line-clamp-3 text-sm text-muted-foreground">{decisionReason(item.rationale)}</p> : null}
            {item.primary_blocker ? <p className="text-xs text-muted-foreground"><span className="font-semibold">Blocker:</span> {decisionReason(item.primary_blocker)}</p> : null}
            <p className="text-sm"><span className="font-semibold">Next:</span> {decisionReason(item.next_action)}</p>
            {expiry ? <p className="text-xs text-muted-foreground">Expires {expiry}</p> : null}
          </>
        )}
        {item.inbox_item_id ? <InboxStateControls itemId={item.inbox_item_id} busy={busy} onState={updateState} /> : null}
        {item.inbox_item_id ? <InboxUsefulnessControls itemId={item.inbox_item_id} useful={item.useful} /> : null}
        {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
        {item.drill_down ? <a aria-label={`Open ${item.title} drill-down`} className="inline-flex min-h-9 items-center rounded-md border border-input px-3 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" href={item.drill_down}>Review evidence</a> : null}
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
    return <div className="rounded-md border border-border p-3 text-sm"><p className="font-semibold">No new trade</p><div className="mt-2"><DataFieldStateNotice state={state} /></div></div>;
  }
  return (
    <div className="rounded-md border border-border p-3 text-sm">
      {plan.selected_expression_kind?.toUpperCase() === "CASH" ? <p className="font-semibold">No new trade</p> : <p><span className="font-semibold">Action:</span> {plan.action} · <span className="font-semibold">Expression:</span> {expressionLabel(plan.selected_expression_kind)}</p>}
      <p className="mt-2 text-muted-foreground"><span className="font-semibold text-foreground">Rationale:</span> {decisionReason(plan.rationale)}</p>
      <p className="mt-2"><span className="font-semibold">Next:</span> {decisionReason(plan.next_action)}</p>
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
            <p className="mt-1 text-xs text-muted-foreground">{semanticStatusLabel(brief.outcome_status, "Pending")} · observed only when a point-in-time QQQ mark is available.</p>
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

function SectionHeader({ section, count, category }: { section: TodayCategory; count: number; category?: TodayBriefCategory }) {
  return (
    <div className="mb-3 flex items-center justify-between gap-3 border-b border-border pb-2">
      <div className="flex min-w-0 items-center gap-2">
        <span className={cn("size-2 shrink-0 rounded-full", section.dot)} />
        <div className="min-w-0">
          <h2 className="truncate text-lg font-semibold leading-6">{section.title}</h2>
          <p className="truncate text-xs text-muted-foreground">{section.subtitle}</p>
        </div>
      </div>
      <StatusBadge tone={count ? section.tone : "muted"}>{category?.total_count == null ? `${count} shown` : `${count} of ${category.total_count} shown`}</StatusBadge>
    </div>
  );
}

function BriefSection({ section, rows, category, onOpenTicker, columns }: { section: TodayCategory; rows: TodayBriefItem[]; category?: TodayBriefCategory; onOpenTicker: (symbol: string) => void; columns?: boolean }) {
  return (
    <div className="min-w-0">
      <SectionHeader section={section} count={rows.length} category={category} />
      {category?.coverage_message ? <p className="mb-3 text-xs text-muted-foreground">{category.coverage_message}</p> : null}
      {rows.length ? (
        <div className={cn("grid gap-3", columns && "xl:grid-cols-2")}>
          {rows.map((item) => (
            <TodayBriefCard key={item.stable_key} item={item} onOpenTicker={onOpenTicker} />
          ))}
        </div>
      ) : (
        <EmptyState title="No published items" detail={category?.coverage_status === "complete" ? `No ${section.title.toLowerCase()} items were found in complete coverage.` : `No ${section.title.toLowerCase()} items are loaded. Coverage is ${semanticStatusLabel(category?.coverage_status, "unknown")}.`} />
      )}
    </div>
  );
}

function CatalystSection({ section, rows, category, onOpenTicker }: { section: TodayCategory; rows: TodayBriefItem[]; category?: TodayBriefCategory; onOpenTicker: (symbol: string) => void }) {
  return (
    <div className="min-w-0">
      <SectionHeader section={section} count={rows.length} category={category} />
      {category?.coverage_message ? <p className="mb-3 text-xs text-muted-foreground">{category.coverage_message}</p> : null}
      {rows.length ? (
        <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          {rows.map((item) => (
            <CatalystRow key={item.stable_key} item={item} onOpenTicker={onOpenTicker} />
          ))}
        </ul>
      ) : (
        <EmptyState title={category?.coverage_status === "complete" ? "No catalysts found" : "Catalyst coverage is unavailable"} detail={category?.coverage_status === "complete" ? "No events were found in the next two weeks in complete coverage." : "No events are loaded. The available data cannot establish that nothing is scheduled."} />
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
        {item.next_action ? <p className="text-sm font-medium">Next: {decisionReason(item.next_action)}</p> : null}
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
  return <StatusBadge tone={owned ? tone : "muted"}>{semanticStatusLabel(context)}</StatusBadge>;
}

type Sentiment = "bullish" | "bearish" | "neutral";

function SentimentMark({ sentiment }: { sentiment: Sentiment }) {
  if (sentiment === "neutral") return <Minus className="size-4 text-muted-foreground" aria-label="Neutral" />;
  const bullish = sentiment === "bullish";
  const Icon = bullish ? TrendingUp : TrendingDown;
  return <Icon className={cn("size-4 shrink-0", bullish ? "text-emerald-600" : "text-red-600")} aria-label={bullish ? "Bullish" : "Bearish"} />;
}

function sentimentOf(value: string | null | undefined): Sentiment {
  value = typeof value === "string" ? value.toLowerCase() : "";
  if (value === "bullish" || value === "good") return "bullish";
  if (value === "bearish" || value === "bad" || value === "sell") return "bearish";
  return "neutral";
}

function cardTone(value: string | null | undefined): Tone {
  return typeof value === "string" ? toneFromText(value) : "muted";
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
