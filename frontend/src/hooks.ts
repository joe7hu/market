import { useEffect, useState } from "react";
import { loadTicker, type TickerPayload } from "./api/panel";
import { useMarketData, type PanelScope } from "./marketData";

const DEFAULT_PANEL_SCOPE_RETRY_DELAYS_MS = [1000, 3000, 8000] as const;

export type PanelScopeRetryOptions = {
  retries?: number;
  retryDelaysMs?: readonly number[];
};

export function retryDelayForAttempt(attempt: number, delaysMs: readonly number[]): number | null {
  if (!Number.isInteger(attempt) || attempt < 0 || attempt >= delaysMs.length) return null;
  const delay = delaysMs[attempt];
  return Number.isFinite(delay) && delay >= 0 ? delay : null;
}

export function usePanelScope(scope: PanelScope, options: PanelScopeRetryOptions = {}) {
  const { loadScope } = useMarketData();
  const retries = Math.max(0, Math.floor(options.retries ?? 0));
  const retryDelaysMs = options.retryDelaysMs ?? DEFAULT_PANEL_SCOPE_RETRY_DELAYS_MS;

  useEffect(() => {
    let cancelled = false;
    let retryAttempt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const load = async (): Promise<void> => {
      try {
        await loadScope(scope);
      } catch {
        if (cancelled || retryAttempt >= retries) return;
        const delay = retryDelayForAttempt(retryAttempt, retryDelaysMs);
        retryAttempt += 1;
        if (delay === null) return;
        timer = setTimeout(() => {
          timer = undefined;
          void load();
        }, delay);
      }
    };

    void load();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [loadScope, retryDelaysMs, retries, scope]);
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
