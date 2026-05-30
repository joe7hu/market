import { RefreshCw } from "lucide-react";

import { DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { AppModel } from "@/model";
import type { PanelData } from "@/types";
import { buildPortfolioViewModel } from "@/viewModels/portfolio";
import { formatMoney, formatPct } from "./rowFormat";
import { DataGridSection } from "./dataGridSection";
import { WorkspacePage, type OpenTicker } from "./workspacePage";

export function PortfolioPage({ data, model, onOpenTicker, onRefresh }: { data: PanelData; model: AppModel; onOpenTicker: OpenTicker; onRefresh: () => Promise<void> }) {
  const viewModel = buildPortfolioViewModel(data, model);
  return (
    <WorkspacePage
      eyebrow="Portfolio"
      title="Portfolio"
      subtitle="Position sizing, concentration risk, and review actions that can change portfolio decisions."
      actions={<Button type="button" variant="outline" onClick={() => void onRefresh()}><RefreshCw /> Refresh</Button>}
      metrics={[
        ["Portfolio Value", formatMoney(model.portfolioValue), `${model.holdings.length} holdings`, model.portfolioValue ? "info" : "muted"],
        ["Top Exposure", viewModel.topHolding ? `${viewModel.topHolding.ticker} ${viewModel.topHolding.weight.toFixed(1)}%` : "None", viewModel.topHolding?.nextStep ?? "no priced holding", viewModel.topHolding && viewModel.topHolding.weight > 30 ? "warn" : "info"],
        ["Risk Cards", viewModel.riskRows.length, "concentration and thesis gaps", viewModel.riskRows.length ? "warn" : "muted"],
        ["Review Actions", viewModel.reviewRows.length, "open portfolio decisions", viewModel.reviewRows.length ? "warn" : "good"],
      ]}
    >
      <PortfolioExposureMap holdings={model.holdings} onOpenTicker={onOpenTicker} />
      <HoldingsTable holdings={model.holdings} onOpenTicker={onOpenTicker} />
      <DataGridSection title="Risk Cards" rows={viewModel.riskRows} onOpenTicker={onOpenTicker} />
      <DataGridSection title="Review Actions" rows={viewModel.reviewRows} onOpenTicker={onOpenTicker} />
      <DataGridSection title="Exposure Clusters" rows={viewModel.exposureClusterRows} onOpenTicker={onOpenTicker} />
    </WorkspacePage>
  );
}

function PortfolioExposureMap({ holdings, onOpenTicker }: { holdings: AppModel["holdings"]; onOpenTicker: OpenTicker }) {
  const priced = holdings.filter((holding) => holding.hasMarketValue).slice().sort((a, b) => b.weight - a.weight);
  if (!priced.length) return null;
  return (
    <DataTableFrame title="Exposure Map">
      <div className="grid gap-3 p-4 lg:grid-cols-2 xl:grid-cols-3">
        {priced.slice(0, 6).map((holding) => {
          const tone = holding.weight > 50 ? "bad" : holding.weight > 30 ? "warn" : "info";
          return (
            <button
              key={holding.ticker}
              type="button"
              className="min-h-24 rounded-md border border-border bg-background p-3 text-left transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => onOpenTicker(holding.ticker)}
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-base font-semibold">{holding.ticker}</div>
                  <div className="mt-1 text-sm text-muted-foreground">{formatMoney(holding.marketValue)}</div>
                </div>
                <StatusBadge tone={tone}>{holding.weight.toFixed(1)}%</StatusBadge>
              </div>
              <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted">
                <div className={cn("h-full rounded-full", tone === "bad" ? "bg-red-600" : tone === "warn" ? "bg-amber-500" : "bg-blue-600")} style={{ width: `${Math.min(100, Math.max(2, holding.weight))}%` }} />
              </div>
              <p className="mt-2 line-clamp-2 text-xs leading-5 text-muted-foreground">{holding.nextStep}</p>
            </button>
          );
        })}
      </div>
    </DataTableFrame>
  );
}

function HoldingsTable({ holdings, onOpenTicker }: { holdings: AppModel["holdings"]; onOpenTicker: OpenTicker }) {
  if (!holdings.length) return <EmptyState title="No holdings loaded" detail="No portfolio holdings are available." />;
  return (
    <DataTableFrame title="Holdings">
      <table className="w-full min-w-[760px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-2">Ticker</th>
            <th className="px-3 py-2">Quantity</th>
            <th className="px-3 py-2">Price</th>
            <th className="px-3 py-2">Market Value</th>
            <th className="px-3 py-2">Weight</th>
            <th className="px-3 py-2">Unrealized</th>
            <th className="px-3 py-2">Review</th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((holding) => (
            <tr key={holding.ticker} className="border-b border-border">
              <td className="px-3 py-2"><Button type="button" variant="link" className="h-auto min-w-11 justify-start p-0" onClick={() => onOpenTicker(holding.ticker)}>{holding.ticker}</Button></td>
              <td className="px-3 py-2">{holding.quantity.toLocaleString()}</td>
              <td className="px-3 py-2">{formatMoney(holding.price)}</td>
              <td className="px-3 py-2">{formatMoney(holding.marketValue)}</td>
              <td className="px-3 py-2">{holding.hasMarketValue ? formatPct(holding.weight) : "-"}</td>
              <td className="px-3 py-2"><StatusBadge tone={holding.unrealizedPnl >= 0 ? "good" : "bad"}>{formatMoney(holding.unrealizedPnl)}</StatusBadge></td>
              <td className="px-3 py-2">{holding.nextStep}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </DataTableFrame>
  );
}
