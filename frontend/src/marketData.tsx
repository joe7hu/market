import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { emptyPanelData, loadPanelScope } from "./api";
import { buildModel, type AppModel } from "./model";
import type { PanelData } from "./types";

export type PanelScope = "today" | "portfolio" | "research" | "filings" | "calendar" | "health" | "settings";

type MarketDataContextValue = {
  data: PanelData;
  model: AppModel;
  loading: boolean;
  lastRefresh: Date | null;
  loadScope: (scope: PanelScope) => Promise<void>;
  openTicker: (symbol: string) => void;
};

const MarketDataContext = createContext<MarketDataContextValue | null>(null);

export function MarketDataProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [data, setData] = useState<PanelData>(() => emptyPanelData());
  const [loading, setLoading] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const dataRef = useRef(data);
  dataRef.current = data;

  const loadScope = useCallback(async (scope: PanelScope) => {
    setLoading(true);
    try {
      const nextData = await loadPanelScope(scope, dataRef.current);
      dataRef.current = nextData;
      setData(nextData);
      setLastRefresh(new Date());
    } finally {
      setLoading(false);
    }
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
