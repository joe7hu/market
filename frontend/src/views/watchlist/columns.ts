export const columnHelp = {
  watch: {
    label: "Watch",
    detail: "Manual watchlist state. Starred rows stay in the watched set; unstar to demote a name when the thesis or setup no longer deserves attention.",
  },
  ticker: {
    label: "Ticker",
    detail: "Canonical symbol that opens the ticker dossier. Use the dossier before a buy, hold, or sell decision when a row-level signal needs evidence.",
  },
  company: {
    label: "Company",
    detail: "Company or fund name from configured instruments, TradingView, or yfinance. A ticker-only name usually means reference data is still thin.",
  },
  price: {
    label: "Price",
    detail: "Latest stored daily or intraday price. Treat a missing or stale price as a coverage gap before acting.",
  },
  marketCap: {
    label: "Mkt Cap",
    detail: "Latest market capitalization from screener or yfinance data. Use it to size opportunity/risk and avoid comparing small caps directly with mega caps.",
  },
  ps: {
    label: "P/S",
    detail: "Price-to-sales multiple from the latest screener or yfinance fundamentals. Lower can support buys, but only with acceptable growth and margins.",
  },
  pe: {
    label: "P/E",
    detail: "Trailing price-to-earnings multiple. High values require stronger durability/growth; negative or missing earnings show as blank.",
  },
  forwardPe: {
    label: "Fwd P/E",
    detail: "Forward price-to-earnings from estimates or yfinance. Prefer it over trailing P/E for changing earnings cycles, but verify estimate quality.",
  },
  revenueGrowth: {
    label: "Rev YoY",
    detail: "Year-over-year revenue growth from SEC facts or yfinance revenue growth. Rising growth supports buys/holds; slowing growth can trigger review.",
  },
  fcfYield: {
    label: "FCF Yield",
    detail: "Free cash flow divided by market cap. Higher yield can mark cheaper self-funding businesses; weak or negative yield raises valuation risk.",
  },
  fcfMargin: {
    label: "FCF Margin",
    detail: "Free cash flow divided by revenue. Higher margins support quality/hold conviction; deterioration can weaken the thesis even if revenue grows.",
  },
  roic: {
    label: "ROIC",
    detail: "Return on invested capital from fundamentals or screeners. Sustained high ROIC supports quality buys/holds; low ROIC needs a turnaround case.",
  },
  returnYtd: {
    label: "% YTD",
    detail: "Price return from the first available trading day of the current year. Use it as context for crowdedness and tax-year momentum.",
  },
  chart1y: {
    label: "Chart 1Y",
    detail: "One-year stored close-price sparkline. A rising line supports trend alignment; a falling line needs valuation or thesis evidence to offset it.",
  },
  return1y: {
    label: "% 1Y",
    detail: "Trailing one-year price return. Compare it with RS 3M to see whether momentum is persistent or only a short-term bounce.",
  },
  drawdown: {
    label: "Delta 52W Highs",
    detail: "Percent below the stored 52-week high. Small gaps show strength; deep gaps may be value setups or sell-risk depending on fundamentals.",
  },
  rs3m: {
    label: "RS 3M",
    detail: "Percentile rank among tickers with technical coverage by trailing 3-month return; bars show the 3-month rank path. Favor high/rising ranks for buys and holds.",
  },
  relVol1m: {
    label: "RelVol 1M",
    detail: "Recent 1-month average volume divided by the prior-volume baseline. 0.95x means about 5% below normal; >1.2x can confirm institutional interest.",
  },
  atrPct1m: {
    label: "ATR % 1M",
    detail: "Average true range over roughly 1 month divided by price. +3.6% means a normal daily range near 3.6%; use it for position sizing and stops.",
  },
  valuationPercentile: {
    label: "Price %ile",
    detail: "Current closing-price percentile versus the ticker's latest 252 daily closes. Low percentile is near the bottom of its one-year range; high percentile is near the top.",
  },
  optionsStatus: {
    label: "Opt",
    detail: "Options summary status from the latest option-chain pull (Robinhood when available, else TradingView/IBKR). Loaded means chain quotes, IV, and Greeks exist.",
  },
  optionsIv: {
    label: "Opt IV",
    detail: "Nearest usable expiry ATM implied volatility bucket from the latest option-chain IV.",
  },
  optionsMove: {
    label: "Opt Move",
    detail: "Nearest usable expiry expected move as a percent of spot — the ATM call+put straddle when both legs are quoted, otherwise IV-implied (spot × ATM IV × √(dte/365)) when quotes are thin (e.g. premarket).",
  },
  optionsSkew: {
    label: "Skew",
    detail: "25-delta put IV minus call IV. Put premium means downside options are priced richer than upside calls.",
  },
  research: {
    label: "Research",
    detail: "Compact research state for this ticker: thesis-review flags, available packets or memos, and packet evidence count. Open the ticker dossier for full bull/bear/why-now detail.",
  },
  sma20: {
    label: "20SMA",
    detail: "Whether price is above the 20-day simple moving average. Good for short-term timing; a break below can flag a failed entry.",
  },
  sma50: {
    label: "50SMA",
    detail: "Whether price is above the 50-day simple moving average. Helps decide whether a pullback is orderly or momentum is weakening.",
  },
  sma200: {
    label: "200SMA",
    detail: "Whether price is above the 200-day simple moving average. Below it, require stronger valuation/thesis evidence before buying.",
  },
  next: {
    label: "Next",
    detail: "Backend-generated next action from decision models and source coverage. Use it as a prompt for research, hold review, or sell-risk checks.",
  },
} as const;

export type ColumnHelpKey = keyof typeof columnHelp;
export type WatchlistRefreshStatus = "idle" | "starting" | "running" | "succeeded" | "failed";
