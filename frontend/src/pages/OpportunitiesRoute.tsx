import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { OpportunitiesPage } from "../views/opportunities";

export function OpportunitiesRoute() {
  const { data, loading, loadScope, openTicker, scopeStatus } = useMarketData();
  usePanelScope("opportunities");

  return (
    <OpportunitiesPage
      data={data}
      loading={loading}
      scopeStatus={scopeStatus.opportunities}
      onOpenTicker={openTicker}
      onRefresh={() => loadScope("opportunities", { force: true })}
    />
  );
}
