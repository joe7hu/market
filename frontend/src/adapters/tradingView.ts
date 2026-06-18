import type { TickerPayload } from "@/types";
import { textField } from "@/views/rowFormat";

export function resolveTradingViewSymbol(symbol: string, ticker: TickerPayload | null): string {
  const resolved = textField(ticker?.dossier?.identity, ["tradingview_symbol"]);
  if (resolved) return resolved.toUpperCase();
  // Exchange qualification is the backend's job via identity.tradingview_symbol.
  // If that field is absent, keep the widget symbol unqualified instead of
  // guessing an exchange.
  return symbol.toUpperCase();
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
