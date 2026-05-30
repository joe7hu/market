import { RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTableFrame, DecisionCard, EmptyState, EvidenceList, MetricTile, PageHeader, StatusBadge } from "@/components/market/workstation";
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

const categories: Array<[string, string]> = [
  ["top_portfolio_changes", "Portfolio Changes"],
  ["top_risks", "Risks"],
  ["top_opportunities", "Opportunities / Research"],
  ["blocked_stale_items", "Blocked / Stale"],
];

export function TodayPage({ data, model, lastRefresh, loading, onRefresh, onOpenTicker }: TodayPageProps) {
  const briefRows = rows(data.dailyBrief);
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
        subtitle="/today is backend-owned: the frontend renders reason, evidence, blocker, and next action without recomputing the decision read model."
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
        <MetricTile label="Next Action" value={topAction} caption={lastRefresh ? `Refreshed ${lastRefresh.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "Waiting for local data"} tone={blocked ? "warn" : "info"} />
      </div>

      {briefRows.length ? (
        <div className="grid gap-4 xl:grid-cols-2">
          {categories.map(([category, title]) => {
            const categoryRows = briefRows.filter((row) => textField(row, ["category"]) === category).slice(0, 6);
            return (
              <Card key={category} className="min-w-0">
                <CardHeader className="border-b border-border p-4">
                  <CardTitle className="text-base">{title}</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3 p-4">
                  {categoryRows.length ? categoryRows.map((row, index) => <TodayBriefCard key={textField(row, ["item_id", "id"], `${category}-${index}`)} row={row} onOpenTicker={onOpenTicker} />) : <EmptyState title="No ranked items" detail="The backend daily_brief read model returned no rows for this category." />}
                </CardContent>
              </Card>
            );
          })}
        </div>
      ) : (
        <EmptyState title="No daily brief loaded" detail="Refresh /today or run the Market refresh job to populate top portfolio changes, risks, opportunities, and blocked/stale items." />
      )}

      <div className="mt-4">
        <DataTableFrame title="Loaded Decision Rows">
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
              {briefRows.slice(0, 16).map((row, index) => (
                <tr key={index} className="border-b border-border">
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

function TodayBriefCard({ row, onOpenTicker }: { row: RowRecord; onOpenTicker: (symbol: string) => void }) {
  const symbols = symbolList(row);
  const blocker = textField(row, ["blocker"], "None");
  const severity = textField(row, ["severity", "status"], "info");
  const tone: Tone = toneFromText(`${severity} ${blocker}`);
  const evidence = listField(row, ["evidence", "evidence_links", "sources"]);
  const rank = numberField(row, ["rank", "priority"], Number.NaN);
  const title = textField(row, ["title", "symbol", "ticker"], "Decision item");

  return (
    <button type="button" className="block w-full text-left" onClick={() => symbols[0] && onOpenTicker(symbols[0])} disabled={!symbols[0]}>
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
    </button>
  );
}
