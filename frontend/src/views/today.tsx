import { CalendarClock, Minus, RefreshCw, TrendingDown, TrendingUp } from "lucide-react";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, MetricTile, PageHeader, StatusBadge } from "@/components/market/workstation";
import { ScopeStatusNotice } from "@/components/market/scopeStatus";
import { cn } from "@/lib/utils";
import type { TodayResponse } from "@/api/panel";
import type { components } from "@/generated/apiSchema";
import type { AppModel } from "@/model";
import type { JsonValue, PanelData, RowRecord, ScopeSnapshotStatus } from "@/types";
import { buildTodayViewModel, todayCategories, type TodayCategory } from "@/viewModels/today";
import { displayField, formatMoney, formatPct, listField, numberField, symbolList, textField, titleLabel, toneFromText, type Tone } from "./rowFormat";
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

type JsonObject = { [key: string]: JsonValue };
type TodayAction = NonNullable<TodayResponse["actions"]>[number];

export function actionQueueDisplay(items: TodayAction[]) {
  const isUnrankedMissingPlan = (item: TodayAction) => (
    item.source === "capital_action"
    && item.lifecycle_state === "blocked"
    && item.primary_blocker === "trade_plan_missing"
    && item.trade_plan == null
    && item.trade_rank == null
    && item.research_rank == null
    && !item.owned
  );
  return {
    items: items.filter((item) => !isUnrankedMissingPlan(item)),
    missingPlanCount: items.filter(isUnrankedMissingPlan).length,
  };
}

export function tradePlanForAction(item: TodayAction) {
  return item.source === "capital_action" ? item.trade_plan ?? null : undefined;
}

const SECTION_BY_KEY: Record<string, TodayCategory> = Object.fromEntries(todayCategories.map((category) => [category.key, category]));
export function TodayPage({ data, model, lastRefresh, actionQueue, actionQueueLoading, actionQueueError, loading, scopeStatus, onRefresh, onOpenTicker }: TodayPageProps) {
  const vm = useMemo(() => buildTodayViewModel(data, model), [data, model]);
  const riskExceptions = (data.portfolioRiskCards?.rows ?? vm.portfolioPulse).slice(0, 3);
  const hasBrief = vm.briefCount > 0;

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
          value={model.holdings.length ? `${formatMoney(vm.portfolioPnl)} (${formatPct(vm.portfolioPnlPct)})` : "No positions"}
          tone={vm.portfolioPnl >= 0 ? "good" : "bad"}
        />
        <MetricTile label="Decisions due" value={vm.decisionsDue} caption="candidates, risks, thesis reviews" tone={vm.decisionsDue ? "warn" : "good"} />
        <MetricTile label="Source updates" value={vm.sourceUpdates} caption="fresh signals on owned / watched" tone={vm.sourceUpdates ? "info" : "muted"} />
        <MetricTile
          label="Top exposure"
          value={vm.largestHolding ? `${vm.largestHolding.ticker} ${vm.largestHolding.weight.toFixed(1)}%` : "None"}
          caption={vm.largestHolding?.nextStep}
          tone={vm.largestHolding && vm.largestHolding.weight > 30 ? "warn" : "info"}
        />
      </div>

      <ActionQueue response={actionQueue} loading={actionQueueLoading} error={actionQueueError} onRefresh={onRefresh} onOpenTicker={onOpenTicker} />
      <CommandCenterPanels data={data} response={actionQueue} onOpenTicker={onOpenTicker} />
      <PreopenBrief row={vm.preopenBrief} />
      <EventScoutPanel truths={data.decisionTruth?.rows ?? []} packets={data.eventDecisionPackets?.rows ?? []} onOpenTicker={onOpenTicker} />

      {hasBrief ? (
        <>
          <div className="grid gap-6">
            <BriefSection section={{ ...SECTION_BY_KEY.portfolio_pulse, title: "Portfolio risk exceptions", subtitle: "The three highest-priority concentration, loss, or thesis-risk exceptions." }} rows={riskExceptions} onOpenTicker={onOpenTicker} columns />
            <CatalystSection section={{ ...SECTION_BY_KEY.catalysts, title: "Catalyst and macro veto", subtitle: "Near-term events and the current deterministic pre-open veto context." }} rows={vm.catalysts.slice(0, 3)} onOpenTicker={onOpenTicker} />
            <details className="rounded-md border border-border bg-card p-4">
              <summary className="cursor-pointer text-sm font-semibold">More daily context</summary>
              <div className="mt-5 grid gap-6">
                <HeroDecision row={vm.hero} onOpenTicker={onOpenTicker} />
                <BriefSection section={SECTION_BY_KEY.decide_now} rows={vm.decideNow} onOpenTicker={onOpenTicker} columns />
                <BriefSection section={SECTION_BY_KEY.whats_changed} rows={vm.whatsChanged} onOpenTicker={onOpenTicker} columns />
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

function CommandCenterPanels({ data, response, onOpenTicker }: { data: PanelData; response: TodayResponse | null; onOpenTicker: (symbol: string) => void }) {
  const capitalActions = (response?.book_actions ?? response?.actions ?? [])
    .filter((item) => (item.source === "capital_action" || item.source === "cash") && item.trade_rank != null)
    .slice(0, 3);
  const marketState = data.marketStateSnapshot?.rows?.[0];
  const risks = (data.portfolioRiskCards?.rows ?? []).slice(0, 3);
  const sourceRows = [...(data.sourceFreshness?.rows ?? []), ...(data.sourceHealth?.rows ?? [])];
  const blockers = sourceRows.filter((row) => /stale|fail|error|blocked|missing|degraded/i.test(textField(row, ["status", "freshness", "health", "effective_status"]))).slice(0, 4);
  return (
    <section className="mb-6 grid min-w-0 gap-4 xl:grid-cols-2" aria-label="Command Center context">
      <Card><CardHeader><CardTitle>Market-state delta</CardTitle></CardHeader><CardContent><dl className="grid gap-3 sm:grid-cols-3">{[["Stance", displayField(marketState, ["stance", "regime", "market_regime"]), "Current state"], ["Delta", displayField(marketState, ["delta", "change", "state_delta"]), "Since prior snapshot"], ["As of", displayField(marketState, ["as_of", "published_at", "available_at"]), "Point-in-time" ]].map(([label, value, detail]) => <div key={label}><dt className="text-xs uppercase text-muted-foreground">{label}</dt><dd className="mt-1 break-words font-semibold">{value}</dd><dd className="text-xs text-muted-foreground">{detail}</dd></div>)}</dl></CardContent></Card>
      <Card><CardHeader><CardTitle>Top three ranked capital actions</CardTitle></CardHeader><CardContent className="space-y-2">{capitalActions.length ? capitalActions.map((item) => <button type="button" key={item.projection_identity} className="flex w-full items-center justify-between gap-3 rounded-md border border-border p-3 text-left hover:bg-accent/40" onClick={() => item.ticker && onOpenTicker(item.ticker)}><span className="min-w-0"><span className="block font-medium">{item.ticker || item.title}</span><span className="block truncate text-xs text-muted-foreground">{item.next_action}</span></span><StatusBadge tone={item.lifecycle_state === "actionable" ? "good" : "warn"}>{item.trade_rank ? `#${item.trade_rank}` : item.action}</StatusBadge></button>) : <p className="text-sm text-muted-foreground">No ranked capital actions are currently available.</p>}</CardContent></Card>
      <Card><CardHeader><CardTitle>Active-position management</CardTitle></CardHeader><CardContent className="space-y-2">{risks.length ? risks.map((row, index) => <div key={textField(row, ["card_id", "title"], String(index))} className="rounded-md border border-border p-3"><div className="flex items-center justify-between gap-3"><span className="font-medium">{textField(row, ["title"], "Position review")}</span><StatusBadge tone={toneFromText(textField(row, ["severity", "risk_level"], "review"))}>{titleLabel(textField(row, ["severity", "risk_level"], "review"))}</StatusBadge></div><p className="mt-1 text-sm text-muted-foreground">{textField(row, ["next_step", "summary"], "Review the affected position.")}</p></div>) : <p className="text-sm text-muted-foreground">No active position exception is loaded.</p>}</CardContent></Card>
      <Card><CardHeader><CardTitle>Blocking source degradation</CardTitle></CardHeader><CardContent>{blockers.length ? <ul className="space-y-2 text-sm">{blockers.map((row, index) => <li key={`${textField(row, ["source_id", "provider", "source"], "source")}:${index}`} className="rounded-md border border-amber-200 bg-amber-50/40 p-3"><span className="font-medium">{textField(row, ["source_name", "source_id", "provider", "source"], "Source")}</span><span className="ml-2 text-muted-foreground">{displayField(row, ["failure_detail", "error", "detail", "status", "freshness"], "Needs review")}</span></li>)}</ul> : <p className="text-sm text-muted-foreground">No blocking source degradation is recorded.</p>}</CardContent></Card>
    </section>
  );
}

function ActionQueue({ response, loading, error, onRefresh, onOpenTicker }: { response: TodayResponse | null; loading: boolean; error: string | null; onRefresh: () => void; onOpenTicker: (symbol: string) => void }) {
  const queue = actionQueueDisplay(response?.actions ?? []);
  const items = queue.items;
  const unavailable = Boolean(response && !response.status.ready);
  const queueError = error ?? (unavailable ? response?.status.message ?? "Action Queue unavailable." : null);
  return (
    <section className="mb-6" aria-labelledby="action-queue-title">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <h2 id="action-queue-title" className="text-lg font-semibold">Action Queue</h2>
          <p className="text-xs text-muted-foreground">Current capital actions, Inbox transitions, portfolio risks, and decision-blocking research.</p>
        </div>
        {response && !unavailable ? <StatusBadge tone="info">{items.length} shown{queue.missingPlanCount ? ` · ${queue.missingPlanCount} missing plans` : ""}</StatusBadge> : null}
      </div>
      {queueError ? <div role="alert" className="mb-3 flex items-center justify-between gap-3 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"><span>{error && response ? `Showing the last Action Queue. ${error}` : `Action Queue unavailable: ${queueError}`}</span><Button type="button" size="sm" variant="outline" onClick={onRefresh}>Retry</Button></div> : null}
      {loading && !response ? <p role="status" className="rounded-md border border-border bg-card p-4 text-sm text-muted-foreground">Loading Action Queue…</p> : null}
      {queue.missingPlanCount ? <p role="status" className="mb-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">{queue.missingPlanCount} unranked ticker decisions remain CASH / NO TRADE because canonical trade plans are missing.</p> : null}
      {!loading && !queueError && !items.length && !queue.missingPlanCount ? <EmptyState title="Action Queue is clear" detail="No current actionable or transition items are available." /> : null}
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
        {plan !== undefined ? <CompactPlanSummary plan={plan} /> : (
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

type TradePlan = components["schemas"]["TradePlan"];

function CompactPlanSummary({ plan }: { plan: TradePlan | null }) {
  if (!plan) {
    return <div className="rounded-md border border-border p-3 text-sm"><p className="font-semibold">NO TRADE · CASH</p><p className="mt-1 text-muted-foreground">Unavailable: canonical trade plan is missing.</p></div>;
  }
  return (
    <div className="rounded-md border border-border p-3 text-sm">
      <p><span className="font-semibold">Action:</span> {plan.action} · <span className="font-semibold">Expression:</span> {plan.selected_expression_kind}</p>
      <p className="mt-2 text-muted-foreground"><span className="font-semibold text-foreground">Rationale:</span> {plan.rationale}</p>
      <p className="mt-2"><span className="font-semibold">Next:</span> {plan.next_action}</p>
    </div>
  );
}

function PreopenBrief({ row }: { row: RowRecord | null }) {
  if (!row) return null;
  const forecast = recordField(row, "qqq_forecast");
  const backtest = recordField(row, "backtest");
  const outcome = recordField(row, "qqq_outcome");
  const events = recordList(row, "key_events");
  const risks = listField(row, ["risks"]);
  const watchItems = listField(row, ["watch_items"]);
  const bias = String(forecast.bias ?? "neutral");
  const forecastStats = [
    moneyStat("Expected", forecast.expected_close),
    moneyStat("Support", forecast.support),
    moneyStat("Resistance", forecast.resistance),
    pctStat("Move", forecast.expected_return_pct),
    pctStat("Backtest MAE", backtest.mae_pct),
    pctStat("Range hit", backtest.range_hit_rate_pct),
  ].filter(Boolean) as string[];
  const outcomeStats = [
    moneyStat("Actual mark", outcome.actual_price),
    pctStat("Actual move", outcome.actual_return_pct),
    pctStat("Error", outcome.absolute_error_pct),
  ].filter(Boolean) as string[];

  return (
    <div className="mb-6 rounded-lg border border-border bg-card p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-xs font-semibold uppercase text-muted-foreground">Pre-open macro brief</p>
          <h2 className="mt-1 text-lg font-semibold leading-6">{textField(row, ["headline"], "Market open brief")}</h2>
        </div>
        <StatusBadge tone={bias === "bullish" ? "good" : bias === "bearish" ? "bad" : "info"}>{bias}</StatusBadge>
      </div>
      <p className="text-sm leading-6 text-muted-foreground">{textField(row, ["macro_regime"])}</p>
      <p className="mt-2 text-sm leading-6 text-foreground">{textField(row, ["narrative"])}</p>
      <div className="mt-3 grid gap-3 lg:grid-cols-[1.15fr_0.85fr]">
        <div className="rounded-md border border-border p-3">
          <p className="text-sm font-semibold">QQQ path</p>
          {forecastStats.length ? <StatRow stats={forecastStats} className="mt-2" /> : null}
          <p className="mt-2 text-sm leading-6 text-muted-foreground">{textField(row, ["qqq_path"])}</p>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">{textField(row, ["opening_scenario"])}</p>
          <div className="mt-3 border-t border-border pt-3">
            <p className="text-sm font-semibold">Forecast loop</p>
            <p className="mt-1 text-xs text-muted-foreground">{String(outcome.status ?? "pending")} · observed only when a point-in-time QQQ mark is available.</p>
            {outcomeStats.length ? <StatRow stats={outcomeStats} className="mt-2" /> : null}
            <p className="mt-1 text-xs text-muted-foreground">Range hit: {outcome.within_forecast_range === true ? "yes" : outcome.within_forecast_range === false ? "no" : "pending"}; direction: {outcome.direction_correct === true ? "correct" : outcome.direction_correct === false ? "wrong" : "pending"}.</p>
          </div>
        </div>
        <div className="rounded-md border border-border p-3">
          <p className="text-sm font-semibold">Key events</p>
          {events.length ? (
            <ul className="mt-2 space-y-1 text-sm text-muted-foreground">
              {events.slice(0, 4).map((event, index) => (
                <li key={String(event.id ?? index)} className="leading-5">
                  {String(event.event_date ?? "")} {String(event.event ?? "")}
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

function HeroDecision({ row, onOpenTicker }: { row: RowRecord | null; onOpenTicker: (symbol: string) => void }) {
  if (!row) return null;
  const symbol = symbolList(row)[0];
  const tone = cardTone(row);
  const sentiment = sentimentOf(row);
  const stats = listField(row, ["stats"]);
  const antithesis = textField(row, ["antithesis"]);
  return (
    <div className={cn("mb-6 rounded-lg border bg-card p-4", toneBorder(tone))}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase text-muted-foreground">Top priority</span>
        <ContextChip context={textField(row, ["context"])} sentiment={sentiment} tone={tone} />
      </div>
      <p className="mt-2 flex items-center gap-2 text-lg font-semibold leading-7 text-foreground">
        {sentiment !== "neutral" ? <SentimentMark sentiment={sentiment} /> : null}
        <span className="min-w-0">{textField(row, ["title"], "Decision item")}</span>
      </p>
      {stats.length ? <StatRow stats={stats} className="mt-1 text-sm" /> : null}
      {displayField(row, ["reason"], "") ? <p className="mt-2 text-sm leading-6 text-muted-foreground">{displayField(row, ["reason"], "")}</p> : null}
      {antithesis ? <p className="mt-1 text-sm leading-6 text-muted-foreground">Counter: {antithesis}</p> : null}
      {symbol ? (
        <div className="mt-3">
          <Button type="button" size="sm" onClick={() => onOpenTicker(symbol)}>
            Open {symbol}
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

function BriefSection({ section, rows, onOpenTicker, columns }: { section: TodayCategory; rows: RowRecord[]; onOpenTicker: (symbol: string) => void; columns?: boolean }) {
  return (
    <div className="min-w-0">
      <SectionHeader section={section} count={rows.length} />
      {rows.length ? (
        <div className={cn("grid gap-3", columns && "xl:grid-cols-2")}>
          {rows.map((row, index) => (
            <TodayBriefCard key={textField(row, ["item_id", "id"], `${section.key}-${index}`)} row={row} onOpenTicker={onOpenTicker} />
          ))}
        </div>
      ) : (
        <EmptyState title="Nothing here" detail={`No ${section.title.toLowerCase()} items right now.`} />
      )}
    </div>
  );
}

function CatalystSection({ section, rows, onOpenTicker }: { section: TodayCategory; rows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  return (
    <div className="min-w-0">
      <SectionHeader section={section} count={rows.length} />
      {rows.length ? (
        <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          {rows.map((row, index) => (
            <CatalystRow key={textField(row, ["item_id", "id"], `cal-${index}`)} row={row} onOpenTicker={onOpenTicker} />
          ))}
        </ul>
      ) : (
        <EmptyState title="No catalysts on your names" detail="Nothing scheduled in the next two weeks for names you own or watch." />
      )}
    </div>
  );
}

function CatalystRow({ row, onOpenTicker }: { row: RowRecord; onOpenTicker: (symbol: string) => void }) {
  const symbol = symbolList(row)[0];
  const days = numberField(row, ["days_until"], Number.NaN);
  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <span className={cn("flex w-20 shrink-0 items-center gap-1.5 text-xs font-semibold", Number.isFinite(days) && days <= 1 ? "text-amber-600" : "text-muted-foreground")}>
        <CalendarClock className="size-3.5" aria-hidden="true" />
        {dueLabel(days)}
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium leading-5">{textField(row, ["title"], "Scheduled event")}</p>
        <p className="truncate text-xs text-muted-foreground">{textField(row, ["context"])}</p>
      </div>
      {symbol ? (
        <Button type="button" size="sm" variant="ghost" className="h-7 shrink-0 text-xs" onClick={() => onOpenTicker(symbol)}>
          {symbol}
        </Button>
      ) : null}
    </li>
  );
}

function TodayBriefCard({ row, onOpenTicker }: { row: RowRecord; onOpenTicker: (symbol: string) => void }) {
  const symbols = symbolList(row);
  const tone = cardTone(row);
  const sentiment = sentimentOf(row);
  const stats = listField(row, ["stats"]);
  const reason = displayField(row, ["reason"], "");
  const antithesis = textField(row, ["antithesis"]);

  return (
    <Card className={cn("min-w-0 overflow-hidden", toneBorder(tone))}>
      <CardContent className="space-y-2 p-4">
        <div className="flex items-start justify-between gap-2">
          <h3 className="flex min-w-0 items-center gap-1.5 text-sm font-semibold leading-5">
            {sentiment !== "neutral" ? <SentimentMark sentiment={sentiment} /> : null}
            <span className="min-w-0">{textField(row, ["title", "symbol", "ticker"], "Decision item")}</span>
          </h3>
          <ContextChip context={textField(row, ["context"])} sentiment={sentiment} tone={tone} />
        </div>
        {stats.length ? <StatRow stats={stats} /> : null}
        {reason ? <p className="text-sm leading-6 text-muted-foreground">{reason}</p> : null}
        {antithesis ? <p className="text-sm leading-6 text-muted-foreground">Counter: {antithesis}</p> : null}
        {symbols.length ? (
          <div className="flex flex-wrap gap-1.5 pt-1">
            {symbols.slice(0, 6).map((symbol) => (
              <Button key={symbol} type="button" variant="outline" size="sm" className="h-6 px-2 text-xs" onClick={() => onOpenTicker(symbol)}>
                {symbol}
              </Button>
            ))}
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

function recordField(row: RowRecord, key: string): JsonObject {
  const value = row[key];
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
}

function recordList(row: RowRecord, key: string): JsonObject[] {
  const value = row[key];
  return Array.isArray(value) ? value.filter((item): item is JsonObject => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];
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

function sentimentOf(row: RowRecord): Sentiment {
  const value = textField(row, ["sentiment"]).toLowerCase();
  if (value === "bullish" || value === "good") return "bullish";
  if (value === "bearish" || value === "bad" || value === "sell") return "bearish";
  return "neutral";
}

function cardTone(row: RowRecord): Tone {
  return toneFromText(textField(row, ["severity", "status"], "info"));
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
