import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { TodayPage } from "../views/today";

export function TodayRoute() {
  const { data, model, lastRefresh, loading, loadScope, openTicker, scopeStatus } = useMarketData();
  usePanelScope("today");

  return (
    <TodayPage
      data={data}
      model={model}
      lastRefresh={lastRefresh}
      loading={loading}
      scopeStatus={scopeStatus.today}
      onRefresh={() => void loadScope("today")}
      onOpenTicker={openTicker}
    />
  );
}
