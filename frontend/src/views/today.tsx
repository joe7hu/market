import { ArrowRight, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { ClickableDecisionCard, EmptyState, EvidenceList, MetricTile, PageHeader, StatusBadge } from "@/components/market/workstation";
import { cn } from "@/lib/utils";
import type { AppModel } from "@/model";
import type { PanelData, RowRecord } from "@/types";
import { buildTodayViewModel, todayCategories } from "@/viewModels/today";
import { displayField, formatMoney, formatPct, listField, numberField, symbolList, textField, titleLabel, toneFromText, type Tone } from "./rowFormat";
import { DataGridSection } from "./dataGridSection";

type TodayPageProps = {
  data: PanelData;
  model: AppModel;
  lastRefresh: Date | null;
  loading: boolean;
  onRefresh: () => void;
  onOpenTicker: (symbol: string) => void;
};

export function TodayPage({ data, model, lastRefresh, loading, onRefresh, onOpenTicker }: TodayPageProps) {
  const [activeCategory, setActiveCategory] = useState("all");
  const viewModel = useMemo(() => buildTodayViewModel(data, model), [data, model]);
  const visibleCategories = activeCategory === "all" ? todayCategories : todayCategories.filter((category) => category.key === activeCategory);
  const visibleBriefRows = activeCategory === "all" ? viewModel.briefRows : viewModel.briefRows.filter((row) => textField(row, ["category"]) === activeCategory);

  return (
    <section>
      <PageHeader
        eyebrow="Daily decision brief"
        title={viewModel.title}
        subtitle="What changed, what matters, and the next portfolio review action."
        actions={
          <Button type="button" variant="outline" onClick={onRefresh}>
            <RefreshCw className={loading ? "animate-spin" : ""} />
            Refresh
          </Button>
        }
      />

      <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricTile label="Portfolio P&L" value={model.holdings.length ? `${formatMoney(viewModel.portfolioPnl)} (${formatPct(viewModel.portfolioPnlPct)})` : "No positions"} tone={viewModel.portfolioPnl >= 0 ? "good" : "bad"} />
        <MetricTile label="Top Exposure" value={viewModel.largestHolding ? `${viewModel.largestHolding.ticker} ${viewModel.largestHolding.weight.toFixed(1)}%` : "None"} caption={viewModel.largestHolding?.nextStep} tone={viewModel.largestHolding && viewModel.largestHolding.weight > 30 ? "warn" : "info"} />
        <MetricTile label="Needs Review" value={viewModel.needsReview} caption="thesis or contradiction review" tone={viewModel.needsReview ? "warn" : "good"} />
        <MetricTile label="Brief Items" value={viewModel.briefRows.length} caption="portfolio, risk, and research signals" tone={viewModel.briefRows.length ? "info" : "muted"} />
      </div>

      <NextActionPanel action={viewModel.topAction} lastRefresh={lastRefresh} />

      <div className="mb-4 flex flex-wrap items-center gap-2 border-b border-border pb-3">
        <CategoryButton active={activeCategory === "all"} label="All" count={viewModel.briefRows.length} onClick={() => setActiveCategory("all")} />
        {todayCategories.map((category) => (
          <CategoryButton
            key={category.key}
            active={activeCategory === category.key}
            label={category.shortTitle}
            count={viewModel.categoryCounts.get(category.key) ?? 0}
            dotClassName={category.dot}
            onClick={() => setActiveCategory(category.key)}
          />
        ))}
      </div>

      {viewModel.briefRows.length ? (
        <div className={cn("grid gap-4", visibleCategories.length > 1 ? "xl:grid-cols-2" : "xl:max-w-3xl")}>
          {visibleCategories.map((category) => {
            const categoryRows = viewModel.briefRows.filter((row) => textField(row, ["category"]) === category.key).slice(0, 6);
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
                  {categoryRows.length ? categoryRows.map((row, index) => <TodayBriefCard key={textField(row, ["item_id", "id"], `${category.key}-${index}`)} row={row} onOpenTicker={onOpenTicker} />) : <EmptyState title="No items" detail="No portfolio review items are currently in this section." />}
                </CardContent>
              </Card>
            );
          })}
        </div>
      ) : (
        <EmptyState title="No daily brief loaded" detail="Refresh /today to load portfolio changes, risks, opportunities, and review checks." />
      )}

      <div className="mt-4">
        <DataGridSection title="Brief Evidence" rows={visibleBriefRows} onOpenTicker={onOpenTicker} />
      </div>
    </section>
  );
}

function NextActionPanel({ action, lastRefresh }: { action: string; lastRefresh: Date | null }) {
  return (
    <div className="mb-4 rounded-md border border-border bg-card px-4 py-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase text-muted-foreground">Primary next action</p>
          <p className="mt-1 text-base font-semibold leading-7 text-foreground">{action}</p>
          <p className="mt-1 text-sm text-muted-foreground">
            {lastRefresh ? "Brief loaded from current local evidence." : "Load the brief before changing sizing."}
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

  return (
    <ClickableDecisionCard
      enabled={Boolean(primarySymbol)}
      onOpen={() => primarySymbol && onOpenTicker(primarySymbol)}
      title={`${Number.isFinite(rank) ? `${rank}. ` : ""}${title}`}
      status={<StatusBadge tone={tone}>{titleLabel(severity)}</StatusBadge>}
      reason={displayField(row, ["reason", "why", "summary"])}
      evidence={<EvidenceList items={evidence.slice(0, 4)} />}
      nextAction={
        <div className="space-y-1">
          <div>{displayField(row, ["next_action", "nextAction"], "No explicit next action")}</div>
          {blocker && blocker.toLowerCase() !== "none" ? <div className="text-red-700">Check: {blocker}</div> : null}
        </div>
      }
      symbols={symbols}
      tone={tone}
    />
  );
}
