import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { FeedPage } from "../marketViews";

export function FeedRoute() {
  const { data, model, lastRefresh, loading, loadScope, openTicker } = useMarketData();
  usePanelScope("feed");

  return (
    <FeedPage
      data={data}
      model={model}
      lastRefresh={lastRefresh}
      loading={loading}
      onRefresh={() => void loadScope("feed")}
      onOpenTicker={openTicker}
    />
  );
}
