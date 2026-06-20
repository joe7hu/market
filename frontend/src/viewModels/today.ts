import type { AppModel, Holding } from "@/model";
import type { PanelData, RowRecord } from "@/types";
import type { Tone } from "@/ui/tone";
import { rows } from "@/utils";
import { numberField, textField } from "@/views/rowFormat";

export type TodayCategory = {
  key: string;
  title: string;
  subtitle: string;
  tone: Tone;
  dot: string;
};

// Lanes match the backend daily_brief categories 1:1, in the order they read on the page.
export const todayCategories: TodayCategory[] = [
  { key: "decide_now", title: "Decide now", subtitle: "Candidates, risks, and thesis reviews that want an action today", tone: "warn", dot: "bg-amber-500" },
  { key: "whats_changed", title: "What changed", subtitle: "Fresh source-backed signals on names you own or watch", tone: "info", dot: "bg-blue-600" },
  { key: "catalysts", title: "This week", subtitle: "Scheduled catalysts in the next two weeks", tone: "good", dot: "bg-violet-600" },
  { key: "portfolio_pulse", title: "Portfolio pulse", subtitle: "Biggest movers and concentration in your book", tone: "info", dot: "bg-emerald-600" },
];

export type TodayViewModel = {
  preopenBrief: RowRecord | null;
  decideNow: RowRecord[];
  whatsChanged: RowRecord[];
  catalysts: RowRecord[];
  portfolioPulse: RowRecord[];
  hero: RowRecord | null;
  largestHolding: Holding | undefined;
  portfolioPnl: number;
  portfolioPnlPct: number;
  decisionsDue: number;
  sourceUpdates: number;
  needsReview: number;
  briefCount: number;
};

function byCategory(briefRows: RowRecord[], key: string): RowRecord[] {
  return briefRows
    .filter((row) => textField(row, ["category"]) === key)
    .sort((a, b) => numberField(b, ["score"], 0) - numberField(a, ["score"], 0));
}

export function buildTodayViewModel(data: PanelData, model: AppModel): TodayViewModel {
  const preopenBrief = rows(data.preopenDailyBrief)[0] ?? null;
  const briefRows = rows(data.dailyBrief);
  const decideNow = byCategory(briefRows, "decide_now");
  const whatsChanged = byCategory(briefRows, "whats_changed");
  // Catalysts read best on a time axis: soonest first.
  const catalysts = byCategory(briefRows, "catalysts").sort((a, b) => numberField(a, ["days_until"], 999) - numberField(b, ["days_until"], 999));
  const portfolioPulse = byCategory(briefRows, "portfolio_pulse");

  const pricedHoldings = model.holdings.filter((holding) => holding.hasMarketValue);
  const largestHolding = pricedHoldings.slice().sort((a, b) => b.weight - a.weight)[0];
  const portfolioPnl = model.holdings.reduce((total, holding) => total + holding.unrealizedPnl, 0);
  const portfolioPnlPct = model.portfolioValue ? (portfolioPnl / model.portfolioValue) * 100 : 0;
  const needsReview = model.thesisMonitorRows.filter((row) => {
    const value = textField(row, ["needs_review"]).toLowerCase();
    return value === "yes" || value === "true";
  }).length;

  return {
    preopenBrief,
    decideNow,
    whatsChanged,
    catalysts,
    portfolioPulse,
    // The single most important thing on the page: the top-scored thing to decide.
    hero: decideNow[0] ?? whatsChanged[0] ?? null,
    largestHolding,
    portfolioPnl,
    portfolioPnlPct,
    decisionsDue: decideNow.length,
    sourceUpdates: whatsChanged.length,
    needsReview,
    briefCount: briefRows.length,
  };
}
