import { useCallback, useEffect, useMemo, useState } from "react";

import { deleteWatchlistSymbol, saveWatchlistSymbol } from "@/api";
import type { PanelData } from "@/types";
import { buildWatchlistViewModel, type WatchState, type WatchlistFilters } from "@/viewModels/watchlist";
import { WorkspacePage, type OpenTicker } from "@/views/workspacePage";

import type { WatchlistRefreshStatus } from "./columns";
import { WatchlistControls, WatchlistRefreshAction } from "./controls";
import { readLocalStates, storageKey } from "./format";
import { WatchlistSection } from "./table";

export function WatchlistPage({
  data,
  refreshStatus,
  refreshFinishedAt,
  refreshError,
  onManualRefresh,
  onOpenTicker,
  onRefresh,
  onLoadUnwatchedPage,
}: {
  data: PanelData;
  refreshStatus: WatchlistRefreshStatus;
  refreshFinishedAt: Date | null;
  refreshError: string | null;
  onManualRefresh: () => Promise<void>;
  onOpenTicker: OpenTicker;
  onRefresh: () => Promise<void>;
  onLoadUnwatchedPage: (offset: number) => Promise<void>;
}) {
  const [pendingSymbol, setPendingSymbol] = useState<string | null>(null);
  const [loadingUnwatched, setLoadingUnwatched] = useState(false);
  const [newSymbol, setNewSymbol] = useState("");
  const [filters, setFilters] = useState<WatchlistFilters>({
    query: "",
    minRating: 0,
    maxForwardPe: null,
    minRoic: null,
    sort: "rank",
  });
  const viewModel = useMemo(() => buildWatchlistViewModel(data, filters, {}), [data, filters]);
  const loadedUnwatchedCount = data.watchlistUnwatched?.rows?.length ?? 0;
  const totalUnwatchedCount = data.watchlistUnwatched?.count ?? loadedUnwatchedCount;
  const canLoadMoreUnwatched = loadedUnwatchedCount < totalUnwatchedCount;

  const updateFilter = <K extends keyof WatchlistFilters>(key: K, value: WatchlistFilters[K]) => setFilters((current) => ({ ...current, [key]: value }));
  const loadMoreUnwatched = useCallback(async () => {
    if (loadingUnwatched || !canLoadMoreUnwatched) return;
    setLoadingUnwatched(true);
    try {
      await onLoadUnwatchedPage(loadedUnwatchedCount);
    } finally {
      setLoadingUnwatched(false);
    }
  }, [canLoadMoreUnwatched, loadedUnwatchedCount, loadingUnwatched, onLoadUnwatchedPage]);

  useEffect(() => {
    const legacyStates = readLocalStates();
    const entries = Object.entries(legacyStates).filter((entry): entry is [string, WatchState] => Boolean(entry[0] && entry[1]));
    if (!entries.length) return;
    let cancelled = false;
    void Promise.all(
      entries.map(([symbol, state]) => (state === "watched" ? saveWatchlistSymbol(symbol) : deleteWatchlistSymbol(symbol))),
    ).then(async () => {
      if (cancelled) return;
      window.localStorage.removeItem(storageKey);
      await onRefresh();
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [onRefresh]);

  const persistWatchState = async (symbol: string, currentState: WatchState) => {
    setPendingSymbol(symbol);
    try {
      if (currentState === "candidate") {
        await saveWatchlistSymbol(symbol);
      } else {
        await deleteWatchlistSymbol(symbol);
      }
      await onRefresh();
    } finally {
      setPendingSymbol(null);
    }
  };
  const addSymbol = async () => {
    const symbol = newSymbol.trim().toUpperCase();
    if (!symbol) return;
    setPendingSymbol(symbol);
    try {
      await saveWatchlistSymbol(symbol);
      setNewSymbol("");
      await onRefresh();
    } finally {
      setPendingSymbol(null);
    }
  };

  return (
    <WorkspacePage
      eyebrow="Market data"
      title="Watchlist"
      subtitle="Dynamic ticker selection, valuation quality, and momentum context for deciding what deserves attention."
      actions={
        <WatchlistRefreshAction
          status={refreshStatus}
          finishedAt={refreshFinishedAt}
          error={refreshError}
          onRefresh={onManualRefresh}
        />
      }
    >
      <WatchlistControls
        filters={filters}
        counts={viewModel.counts}
        totalRows={viewModel.watchedRows.length + totalUnwatchedCount}
        visibleRows={viewModel.visibleRows.length}
        newSymbol={newSymbol}
        pending={Boolean(pendingSymbol)}
        onNewSymbolChange={setNewSymbol}
        onAddSymbol={addSymbol}
        onChange={updateFilter}
      />
      <WatchlistSection
        title="Watched"
        detail={`${viewModel.watchedRows.length.toLocaleString()} shown, including owned positions`}
        rows={viewModel.watchedRows}
        pendingSymbol={pendingSymbol}
        onOpenTicker={onOpenTicker}
        onSetWatchState={persistWatchState}
      />
      <WatchlistSection
        title="Not watched"
        detail={`${viewModel.unwatchedRows.length.toLocaleString()} shown from ${loadedUnwatchedCount.toLocaleString()} loaded / ${totalUnwatchedCount.toLocaleString()} candidates`}
        rows={viewModel.unwatchedRows}
        canLoadMore={canLoadMoreUnwatched}
        loadingMore={loadingUnwatched}
        onLoadMore={loadMoreUnwatched}
        pendingSymbol={pendingSymbol}
        onOpenTicker={onOpenTicker}
        onSetWatchState={persistWatchState}
      />
    </WorkspacePage>
  );
}
