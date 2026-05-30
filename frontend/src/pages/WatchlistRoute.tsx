import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { WatchlistPage } from "@/views/watchlist";

export function WatchlistRoute() {
  const { data, openTicker } = useMarketData();
  usePanelScope("watchlist");

  return <WatchlistPage data={data} onOpenTicker={openTicker} />;
}
