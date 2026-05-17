import { useEffect, useState } from "react";
import { loadTicker } from "./api";
import { useMarketData, type PanelScope } from "./marketData";
import type { TickerPayload } from "./types";

export function usePanelScope(scope: PanelScope) {
  const { loadScope } = useMarketData();

  useEffect(() => {
    void loadScope(scope).catch(() => undefined);
  }, [loadScope, scope]);
}

export function useTicker(symbol: string): TickerPayload | null {
  const [ticker, setTicker] = useState<TickerPayload | null>(null);
  const normalized = symbol.trim().toUpperCase();

  useEffect(() => {
    if (!normalized) {
      setTicker(null);
      return;
    }
    let cancelled = false;
    setTicker(null);
    void loadTicker(normalized)
      .then((payload) => {
        if (!cancelled && (payload.ticker ?? "").toUpperCase() === normalized) {
          setTicker(payload);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setTicker(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [normalized]);

  return ticker;
}
