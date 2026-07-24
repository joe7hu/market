import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { emptyPanelData, loadPanelScope, type PanelScopeOptions } from "./api";
import { withScopeStatus } from "./apiPanelData";
import { buildModel, type AppModel } from "./model";
import type { PanelData, ScopeSnapshotStatus } from "./types";

export type PanelScope = "feed" | "today" | "watchlist" | "watchlist-watched" | "watchlist-unwatched" | "sources" | "superinvestors" | "market" | "portfolio" | "research" | "thesis-monitor" | "options-radar" | "filings" | "calendar" | "health" | "settings";

type MarketDataContextValue = {
  data: PanelData;
  model: AppModel;
  loading: boolean;
  lastRefresh: Date | null;
  scopeStatus: Record<string, ScopeSnapshotStatus>;
  loadScope: (scope: PanelScope, options?: PanelScopeOptions) => Promise<void>;
  openTicker: (symbol: string) => void;
};

const MarketDataContext = createContext<MarketDataContextValue | null>(null);

export function MarketDataProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [data, setData] = useState<PanelData>(() => emptyPanelData());
  const [loading, setLoading] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const dataRef = useRef(data);
  const inFlightScopesRef = useRef(new Map<string, Promise<void>>());
  dataRef.current = data;

  const loadScope = useCallback(async (scope: PanelScope, options?: PanelScopeOptions) => {
    if (options?.force) {
      const pending = [...inFlightScopesRef.current.entries()]
        .filter(([key]) => key.startsWith(`${scope}:`))
        .map(([, request]) => request);
      if (pending.length) await Promise.allSettled(pending);
    }
    const requestKey = `${scope}:${JSON.stringify(options ?? {})}`;
    const inFlight = inFlightScopesRef.current.get(requestKey);
    if (inFlight) return inFlight;
    const request = (async () => {
      setLoading(true);
      setData((current) => withScopeStatus(current, scope, { state: "loading" }));
      try {
        const nextData = await loadPanelScope(scope, dataRef.current, options);
        dataRef.current = nextData;
        setData(nextData);
        setLastRefresh(new Date());
      } catch (error) {
        const message = error instanceof Error ? error.message : "Unable to load this page.";
        const failed = withScopeStatus(dataRef.current, scope, { state: "failed", error: message });
        dataRef.current = failed;
        setData(failed);
        throw error;
      } finally {
        inFlightScopesRef.current.delete(requestKey);
        setLoading(false);
      }
    })();
    inFlightScopesRef.current.set(requestKey, request);
    return request;
  }, []);

  const openTicker = useCallback((symbol: string) => {
    const normalized = symbol.trim().toUpperCase();
    if (normalized) {
      navigate(`/tickers/${encodeURIComponent(normalized)}`);
    }
  }, [navigate]);

  const model = useMemo(() => buildModel(data), [data]);
  const value = useMemo(() => ({
    data,
    model,
    loading,
    lastRefresh,
    scopeStatus: data.scopeStatus,
    loadScope,
    openTicker,
  }), [data, model, loading, lastRefresh, loadScope, openTicker]);

  return <MarketDataContext.Provider value={value}>{children}</MarketDataContext.Provider>;
}

export function useMarketData(): MarketDataContextValue {
  const value = useContext(MarketDataContext);
  if (!value) {
    throw new Error("useMarketData must be used inside MarketDataProvider");
  }
  return value;
}
