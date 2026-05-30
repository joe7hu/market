import { ArrowRight, RefreshCw } from "lucide-react";
import { useMemo, useState, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { DataTableFrame, DecisionCard, EmptyState, EvidenceList, MetricTile, PageHeader, StatusBadge } from "@/components/market/workstation";
import { cn } from "@/lib/utils";
import type { AppModel } from "@/model";
import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { displayField, formatMoney, formatPct, listField, numberField, symbolList, textField, titleLabel, toneFromText, type Tone } from "./rowFormat";

type TodayPageProps = {
  data: PanelData;
  model: AppModel;
  lastRefresh: Date | null;
  loading: boolean;
  onRefresh: () => void;
  onOpenTicker: (symbol: string) => void;
};

const categories: Array<{ key: string; title: string; shortTitle: string; tone: Tone; dot: string }> = [
  { key: "top_portfolio_changes", title: "Portfolio Changes", shortTitle: "Portfolio", tone: "info", dot: "bg-blue-600" },
  { key: "top_risks", title: "Risks", shortTitle: "Risk", tone: "warn", dot: "bg-amber-500" },
  { key: "top_opportunities", title: "Opportunities / Research", shortTitle: "Research", tone: "good", dot: "bg-green-600" },
  { key: "blocked_stale_items", title: "Blocked / Stale", shortTitle: "Blocked", tone: "bad", dot: "bg-red-600" },
];

export function TodayPage({ data, model, lastRefresh, loading, onRefresh, onOpenTicker }: TodayPageProps) {
  const [activeCategory, setActiveCategory] = useState("all");
  const briefRows = rows(data.dailyBrief);
  const categoryCounts = useMemo(() => new Map(categories.map((category) => [category.key, briefRows.filter((row) => textField(row, ["category"]) === category.key).length])), [briefRows]);
  const visibleCategories = activeCategory === "all" ? categories : categories.filter((category) => category.key === activeCategory);
  const visibleBriefRows = activeCategory === "all" ? briefRows : briefRows.filter((row) => textField(row, ["category"]) === activeCategory);
  const pricedHoldings = model.holdings.filter((holding) => holding.hasMarketValue);
  const largestHolding = pricedHoldings.slice().sort((a, b) => b.weight - a.weight)[0];
  const portfolioPnl = model.holdings.reduce((total, holding) => total + holding.unrealizedPnl, 0);
  const portfolioPnlPct = model.portfolioValue ? (portfolioPnl / model.portfolioValue) * 100 : 0;
  const needsReview = model.thesisMonitorRows.filter((row) => textField(row, ["needs_review"]).toLowerCase() === "yes" || textField(row, ["needs_review"]).toLowerCase() === "true").length;
  const blocked = model.decisionReadinessRows.filter((row) => textField(row, ["status"]) !== "ready").length;
  const topAction = briefRows[0] ? textField(briefRows[0], ["next_action", "nextAction"], "Review the top decision brief item.") : "Load the daily brief before changing sizing.";

  return (
    <section>
      <PageHeader
        eyebrow="Daily decision brief"
        title={briefRows[0] ? textField(briefRows[0], ["title"], "Today") : "Today"}
        subtitle="What changed, what matters, what is blocked, and the next review action."
        actions={
          <Button type="button" variant="outline" onClick={onRefresh}>
            <RefreshCw className={loading ? "animate-spin" : ""} />
            Refresh
          </Button>
        }
      />

      <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricTile label="Portfolio P&L" value={model.holdings.length ? `${formatMoney(portfolioPnl)} (${formatPct(portfolioPnlPct)})` : "No positions"} tone={portfolioPnl >= 0 ? "good" : "bad"} />
        <MetricTile label="Top Exposure" value={largestHolding ? `${largestHolding.ticker} ${largestHolding.weight.toFixed(1)}%` : "None"} caption={largestHolding?.nextStep} tone={largestHolding && largestHolding.weight > 30 ? "warn" : "info"} />
        <MetricTile label="Needs Review" value={needsReview} caption="stale thesis or contradiction flag" tone={needsReview ? "warn" : "good"} />
        <MetricTile label="Blocked Readiness" value={blocked} caption="not-ready decision rows" tone={blocked ? "warn" : "good"} />
      </div>

      <NextActionPanel action={topAction} lastRefresh={lastRefresh} blocked={blocked} />

      <div className="mb-4 flex flex-wrap items-center gap-2 border-b border-border pb-3">
        <CategoryButton active={activeCategory === "all"} label="All" count={briefRows.length} onClick={() => setActiveCategory("all")} />
        {categories.map((category) => (
          <CategoryButton
            key={category.key}
            active={activeCategory === category.key}
            label={category.shortTitle}
            count={categoryCounts.get(category.key) ?? 0}
            dotClassName={category.dot}
            onClick={() => setActiveCategory(category.key)}
          />
        ))}
      </div>

      {briefRows.length ? (
        <div className={cn("grid gap-4", visibleCategories.length > 1 ? "xl:grid-cols-2" : "xl:max-w-3xl")}>
          {visibleCategories.map((category) => {
            const categoryRows = briefRows.filter((row) => textField(row, ["category"]) === category.key).slice(0, 6);
            return (
              <Card key={category.key} className={cn("min-w-0 overflow-hidden", category.tone === "warn" && "border-amber-200", category.tone === "bad" && "border-red-200", category.tone === "good" && "border-green-200")}>
                <CardHeader className="border-b border-border p-4">
                  <h2 className="flex items-center justify-between gap-3 text-lg font-semibold">
                    <span className="flex min-w-0 items-center gap-2">
                      <span className={cn("size-2 shrink-0 rounded-full", category.dot)} />
                      <span className="truncate">{category.title}</span>
                    </span>
                    <StatusBadge tone={category.tone}>
                      <span aria-hidden="true">{categoryRows.length}</span>
                      <span className="sr-only"> {categoryRows.length} items</span>
                    </StatusBadge>
                  </h2>
                </CardHeader>
                <CardContent className="space-y-3 p-4">
                  {categoryRows.length ? categoryRows.map((row, index) => <TodayBriefCard key={textField(row, ["item_id", "id"], `${category.key}-${index}`)} row={row} onOpenTicker={onOpenTicker} />) : <EmptyState title="No ranked items" detail="The backend daily_brief read model returned no rows for this category." />}
                </CardContent>
              </Card>
            );
          })}
        </div>
      ) : (
        <EmptyState title="No daily brief loaded" detail="Refresh /today or run the Market refresh job to populate top portfolio changes, risks, opportunities, and blocked/stale items." />
      )}

      <div className="mt-4">
        <DataTableFrame title="Daily Brief Audit Trail">
          <table className="w-full min-w-[720px] text-sm">
            <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2">Section</th>
                <th className="px-3 py-2">Title</th>
                <th className="px-3 py-2">Reason</th>
                <th className="px-3 py-2">Next</th>
              </tr>
            </thead>
            <tbody>
              {visibleBriefRows.slice(0, 16).map((row, index) => (
                <tr key={index} className="border-b border-border transition-colors hover:bg-accent/40">
                  <td className="px-3 py-2 text-muted-foreground">{titleLabel(textField(row, ["category"], "item"))}</td>
                  <td className="px-3 py-2 font-medium">{displayField(row, ["title"])}</td>
                  <td className="px-3 py-2">{displayField(row, ["reason"])}</td>
                  <td className="px-3 py-2">{displayField(row, ["next_action", "nextAction"])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </DataTableFrame>
      </div>
    </section>
  );
}

function NextActionPanel({ action, lastRefresh, blocked }: { action: string; lastRefresh: Date | null; blocked: number }) {
  return (
    <div className="mb-4 rounded-md border border-border bg-card px-4 py-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase text-muted-foreground">Primary next action</p>
          <p className="mt-1 text-base font-semibold leading-7 text-foreground">{action}</p>
          <p className="mt-1 text-sm text-muted-foreground">
            {lastRefresh ? `Refreshed ${lastRefresh.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "Waiting for local data"}
            {blocked ? ` · ${blocked} readiness blockers` : " · decision readiness clear"}
          </p>
        </div>
        <ArrowRight className="hidden size-5 text-muted-foreground sm:block" aria-hidden="true" />
      </div>
    </div>
  );
}

function CategoryButton({ active, label, count, dotClassName, onClick }: { active: boolean; label: string; count: number; dotClassName?: string; onClick: () => void }) {
  return (
    <Button
      type="button"
      variant={active ? "default" : "outline"}
      size="sm"
      className={cn("gap-2", !active && "bg-card")}
      onClick={onClick}
    >
      {dotClassName ? <span className={cn("size-2 rounded-full", dotClassName)} /> : null}
      <span>{label}</span>
      <span className={cn("rounded-sm px-1.5 py-0.5 text-[11px] leading-none", active ? "bg-primary-foreground/20" : "bg-muted text-muted-foreground")} aria-hidden="true">{count}</span>
      <span className="sr-only"> {count} items</span>
    </Button>
  );
}

function TodayBriefCard({ row, onOpenTicker }: { row: RowRecord; onOpenTicker: (symbol: string) => void }) {
  const symbols = symbolList(row);
  const blocker = textField(row, ["blocker"], "None");
  const severity = textField(row, ["severity", "status"], "info");
  const tone: Tone = toneFromText(`${severity} ${blocker}`);
  const evidence = listField(row, ["evidence", "evidence_links", "sources"]);
  const rank = numberField(row, ["rank", "priority"], Number.NaN);
  const title = textField(row, ["title", "symbol", "ticker"], "Decision item");
  const primarySymbol = symbols[0];
  const openTicker = () => {
    if (primarySymbol) onOpenTicker(primarySymbol);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!primarySymbol) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpenTicker(primarySymbol);
    }
  };

  return (
    <div
      role={primarySymbol ? "button" : undefined}
      tabIndex={primarySymbol ? 0 : -1}
      aria-disabled={primarySymbol ? undefined : true}
      className={cn("block w-full text-left transition-transform hover:-translate-y-px focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2", primarySymbol ? "cursor-pointer" : "cursor-default")}
      onClick={openTicker}
      onKeyDown={onKeyDown}
    >
      <DecisionCard
        title={`${Number.isFinite(rank) ? `${rank}. ` : ""}${title}`}
        status={<StatusBadge tone={tone}>{titleLabel(severity)}</StatusBadge>}
        reason={displayField(row, ["reason", "why", "summary"])}
        evidence={<EvidenceList items={evidence.slice(0, 4)} />}
        nextAction={
          <div className="space-y-1">
            <div>{displayField(row, ["next_action", "nextAction"], "No explicit next action")}</div>
            {blocker && blocker.toLowerCase() !== "none" ? <div className="text-red-700">Blocked: {blocker}</div> : null}
          </div>
        }
        symbols={symbols}
        tone={tone}
      />
    </div>
  );
}
