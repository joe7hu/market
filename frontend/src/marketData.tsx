import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { emptyPanelData, loadEventScoutSnapshot, loadPanelScope, type PanelScopeOptions } from "./api/panel";
import { mergeSnapshot, withScopeStatus } from "./apiPanelData";
import { buildModel, type AppModel } from "./model";
import type { PanelData, ScopeSnapshotStatus } from "./types";

export type PanelScope = "feed" | "today" | "watchlist" | "watchlist-watched" | "watchlist-unwatched" | "sources" | "superinvestors" | "market" | "portfolio" | "research" | "opportunities" | "thesis-monitor" | "options-radar" | "filings" | "calendar" | "health" | "settings";

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
  const generationsRef = useRef(new Map<string, number>());
  const scopeGenerationsRef = useRef(new Map<string, number>());
  const eventScoutGenerationRef = useRef(0);
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
    const generation = (generationsRef.current.get(requestKey) ?? 0) + 1;
    generationsRef.current.set(requestKey, generation);
    const scopeGeneration = (scopeGenerationsRef.current.get(scope) ?? 0) + 1;
    scopeGenerationsRef.current.set(scope, scopeGeneration);
    const request = (async () => {
      setLoading(true);
      dataRef.current = withScopeStatus(dataRef.current, scope, { state: "loading" });
      setData(dataRef.current);
      try {
        const loaded = await loadPanelScope(scope, options);
        if (generationsRef.current.get(requestKey) !== generation || scopeGenerationsRef.current.get(scope) !== scopeGeneration) return;
        let supplemental;
        let eventScoutGeneration: number | undefined;
        if (scope === "today" || scope === "options-radar") {
          eventScoutGeneration = ++eventScoutGenerationRef.current;
          try {
            supplemental = await loadEventScoutSnapshot();
          } catch {
            // Event Scout is a bounded supplemental read; the core page keeps
            // its existing last-good snapshot when the event endpoint is down.
          }
        }
        if (generationsRef.current.get(requestKey) !== generation || scopeGenerationsRef.current.get(scope) !== scopeGeneration) return;
        let nextData = mergeSnapshot(dataRef.current, loaded.snapshot, { ...options, queryKey: scope, generation: scopeGeneration });
        if (loaded.settings) nextData = { ...nextData, settings: loaded.settings };
        if (supplemental && eventScoutGeneration === eventScoutGenerationRef.current) {
          nextData = mergeSnapshot(nextData, supplemental, { queryKey: "event-scout", generation: eventScoutGeneration });
        }
        dataRef.current = nextData;
        setData(nextData);
        if (nextData.scopeStatus[scope]?.state !== "failed") setLastRefresh(new Date());
      } catch (error) {
        if (generationsRef.current.get(requestKey) !== generation || scopeGenerationsRef.current.get(scope) !== scopeGeneration) return;
        const message = error instanceof Error ? error.message : "Unable to load this page.";
        const failed = withScopeStatus(dataRef.current, scope, { state: "failed", error: message });
        dataRef.current = failed;
        setData(failed);
        throw error;
      } finally {
        inFlightScopesRef.current.delete(requestKey);
        setLoading(inFlightScopesRef.current.size > 0);
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
