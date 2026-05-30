import type { RowRecord, TickerPayload } from "@/types";
import { textField } from "@/views/rowFormat";

export function resolveTradingViewSymbol(symbol: string, ticker: TickerPayload | null): string {
  const normalized = symbol.toUpperCase();
  const tables = ticker?.tables ?? {};
  const candidates = [
    ...compactRows(tables.quotes).map((row) => nestedString(row, ["raw", "symbol"])),
    ...compactRows(tables.tradingview_chart_state).map((row) => textField(row, ["symbol"])),
    ...compactRows(tables.tradingview_symbol_search).map((row) => {
      const exchange = textField(row, ["exchange"]);
      const rowSymbol = textField(row, ["symbol", "ticker"]);
      return exchange && rowSymbol && !rowSymbol.includes(":") ? `${exchange}:${rowSymbol}` : rowSymbol;
    }),
  ];
  const explicit = candidates.find((candidate) => candidate.includes(":"));
  if (explicit) return explicit.toUpperCase();
  if (normalized.endsWith("-USD")) return `COINBASE:${normalized.replace("-USD", "USD")}`;
  if (["SPY", "QQQ"].includes(normalized)) return `AMEX:${normalized}`;
  return `NASDAQ:${normalized}`;
}

export function tradingViewEmbedUrl(tradingViewSymbol: string): string {
  const params = new URLSearchParams({
    frameElementId: `market-chart-${tradingViewSymbol.replace(/[^A-Za-z0-9]/g, "-")}`,
    symbol: tradingViewSymbol,
    interval: "D",
    range: "12M",
    hidesidetoolbar: "1",
    symboledit: "1",
    saveimage: "0",
    toolbarbg: "F1F3F6",
    theme: "light",
    style: "1",
    timezone: "Etc/UTC",
    withdateranges: "1",
    hideideas: "1",
  });
  return `https://www.tradingview.com/widgetembed/?${params.toString()}`;
}

function compactRows(sectionRows: RowRecord[] | undefined): RowRecord[] {
  return (sectionRows ?? [])
    .map((row) => Object.fromEntries(Object.entries(row).filter(([, value]) => !isEmptyCell(value))) as RowRecord)
    .filter((row) => Object.keys(row).length > 0);
}

function isEmptyCell(value: RowRecord[string]): boolean {
  if (value === undefined || value === null || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  return typeof value === "object" && Object.keys(value).length === 0;
}

function nestedString(row: RowRecord, path: string[]): string {
  let current: unknown = row;
  for (const key of path) {
    if (!current || typeof current !== "object" || !(key in current)) return "";
    current = (current as Record<string, unknown>)[key];
  }
  return typeof current === "string" ? current.trim() : "";
}
