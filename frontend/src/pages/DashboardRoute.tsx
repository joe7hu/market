import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { DashboardPage } from "../marketViews";

export function DashboardRoute() {
  const { model, lastRefresh, loading, loadScope, openTicker } = useMarketData();
  usePanelScope("dashboard");

  return (
    <DashboardPage
      model={model}
      lastRefresh={lastRefresh}
      loading={loading}
      onRefresh={() => void loadScope("dashboard")}
      onOpenTicker={openTicker}
    />
  );
}
