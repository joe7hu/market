import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { TodayPage } from "../marketViews";

export function TodayRoute() {
  const { model, lastRefresh, loading, loadScope, openTicker } = useMarketData();
  usePanelScope("today");

  return (
    <TodayPage
      model={model}
      lastRefresh={lastRefresh}
      loading={loading}
      onRefresh={() => void loadScope("today")}
      onOpenTicker={openTicker}
    />
  );
}
