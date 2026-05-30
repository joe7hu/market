import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { FeedPage } from "../views/feed";

export function FeedRoute() {
  const { data, lastRefresh, loading, loadScope, openTicker } = useMarketData();
  usePanelScope("feed");

  return (
    <FeedPage
      data={data}
      lastRefresh={lastRefresh}
      loading={loading}
      onRefresh={() => void loadScope("feed")}
      onOpenTicker={openTicker}
    />
  );
}
