import type { AppModel, Holding } from "@/model";
import type { PanelData, RowRecord } from "@/types";
import type { Tone } from "@/ui/tone";
import { rows } from "@/utils";
import { textField } from "@/views/rowFormat";

export type TodayCategory = {
  key: string;
  title: string;
  shortTitle: string;
  tone: Tone;
  dot: string;
};

export const todayCategories: TodayCategory[] = [
  { key: "top_portfolio_changes", title: "Portfolio Changes", shortTitle: "Portfolio", tone: "info", dot: "bg-blue-600" },
  { key: "top_risks", title: "Risks", shortTitle: "Risk", tone: "warn", dot: "bg-amber-500" },
  { key: "top_opportunities", title: "Opportunities / Research", shortTitle: "Research", tone: "good", dot: "bg-green-600" },
  { key: "blocked_stale_items", title: "Action Checks", shortTitle: "Checks", tone: "bad", dot: "bg-red-600" },
];

export type TodayViewModel = {
  title: string;
  briefRows: RowRecord[];
  categoryCounts: Map<string, number>;
  largestHolding: Holding | undefined;
  portfolioPnl: number;
  portfolioPnlPct: number;
  needsReview: number;
  topAction: string;
};

export function buildTodayViewModel(data: PanelData, model: AppModel): TodayViewModel {
  const briefRows = rows(data.dailyBrief);
  const categoryCounts = new Map(todayCategories.map((category) => [category.key, briefRows.filter((row) => textField(row, ["category"]) === category.key).length]));
  const pricedHoldings = model.holdings.filter((holding) => holding.hasMarketValue);
  const largestHolding = pricedHoldings.slice().sort((a, b) => b.weight - a.weight)[0];
  const portfolioPnl = model.holdings.reduce((total, holding) => total + holding.unrealizedPnl, 0);
  const portfolioPnlPct = model.portfolioValue ? (portfolioPnl / model.portfolioValue) * 100 : 0;
  const needsReview = model.thesisMonitorRows.filter((row) => {
    const value = textField(row, ["needs_review"]).toLowerCase();
    return value === "yes" || value === "true";
  }).length;
  const topAction = briefRows[0] ? textField(briefRows[0], ["next_action", "nextAction"], "Review the top decision brief item.") : "Load the daily brief before changing sizing.";

  return {
    title: briefRows[0] ? textField(briefRows[0], ["title"], "Today") : "Today",
    briefRows,
    categoryCounts,
    largestHolding,
    portfolioPnl,
    portfolioPnlPct,
    needsReview,
    topAction,
  };
}
