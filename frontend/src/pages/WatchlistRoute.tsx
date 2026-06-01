import { useCallback, useEffect } from "react";
import { useMarketData } from "../marketData";
import { WatchlistPage } from "@/views/watchlist";

const CANDIDATE_PAGE_SIZE = 80;

export function WatchlistRoute() {
  const { data, loadScope, openTicker } = useMarketData();
  const loadWatchlist = useCallback(async () => {
    await loadScope("watchlist-watched");
  }, [loadScope]);
  const loadUnwatchedPage = useCallback(async (offset: number) => {
    await loadScope("watchlist-unwatched", { offset, limit: CANDIDATE_PAGE_SIZE, append: offset > 0 });
  }, [loadScope]);

  useEffect(() => {
    void loadWatchlist().catch(() => undefined);
  }, [loadWatchlist]);

  return <WatchlistPage data={data} onOpenTicker={openTicker} onRefresh={loadWatchlist} onLoadUnwatchedPage={loadUnwatchedPage} />;
}
