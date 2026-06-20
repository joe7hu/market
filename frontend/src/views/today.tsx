import { CalendarClock, Minus, RefreshCw, TrendingDown, TrendingUp } from "lucide-react";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, MetricTile, PageHeader, StatusBadge } from "@/components/market/workstation";
import { cn } from "@/lib/utils";
import type { AppModel } from "@/model";
import type { JsonValue, PanelData, RowRecord } from "@/types";
import { buildTodayViewModel, todayCategories, type TodayCategory } from "@/viewModels/today";
import { displayField, formatMoney, formatPct, listField, numberField, symbolList, textField, toneFromText, type Tone } from "./rowFormat";

type TodayPageProps = {
  data: PanelData;
  model: AppModel;
  lastRefresh: Date | null;
  loading: boolean;
  onRefresh: () => void;
  onOpenTicker: (symbol: string) => void;
};

type JsonObject = { [key: string]: JsonValue };

const SECTION_BY_KEY: Record<string, TodayCategory> = Object.fromEntries(todayCategories.map((category) => [category.key, category]));

export function TodayPage({ data, model, lastRefresh, loading, onRefresh, onOpenTicker }: TodayPageProps) {
  const vm = useMemo(() => buildTodayViewModel(data, model), [data, model]);
  const hasBrief = vm.briefCount > 0;

  return (
    <section>
      <PageHeader
        eyebrow="Daily decision brief"
        title="Today"
        subtitle="What needs a decision, what changed in your sources, what's coming on your names, and how your book is moving."
        actions={
          <Button type="button" variant="outline" onClick={onRefresh}>
            <RefreshCw className={loading ? "animate-spin" : ""} />
            Refresh
          </Button>
        }
      />

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

      <PreopenBrief row={vm.preopenBrief} />

      {hasBrief ? (
        <>
          <HeroDecision row={vm.hero} onOpenTicker={onOpenTicker} />

          <div className="grid gap-6">
            <BriefSection section={SECTION_BY_KEY.decide_now} rows={vm.decideNow} onOpenTicker={onOpenTicker} columns />
            <BriefSection section={SECTION_BY_KEY.whats_changed} rows={vm.whatsChanged} onOpenTicker={onOpenTicker} columns />
            <CatalystSection section={SECTION_BY_KEY.catalysts} rows={vm.catalysts} onOpenTicker={onOpenTicker} />
            <BriefSection section={SECTION_BY_KEY.portfolio_pulse} rows={vm.portfolioPulse} onOpenTicker={onOpenTicker} columns />
          </div>
        </>
      ) : (
        <EmptyState title="No daily brief loaded" detail="Refresh /today to load decisions, source changes, catalysts, and portfolio moves." />
      )}
    </section>
  );
}

function PreopenBrief({ row }: { row: RowRecord | null }) {
  if (!row) return null;
  const forecast = recordField(row, "qqq_forecast");
  const backtest = recordField(row, "backtest");
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
