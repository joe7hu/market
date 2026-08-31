import { useCallback } from "react";
import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { OpportunitiesPage } from "../views/opportunities";

export function OpportunitiesRoute() {
  const { data, loading, loadScope, openTicker, scopeStatus } = useMarketData();
  usePanelScope("opportunities");
  const loadScreener = useCallback(() => loadScope("opportunities", { includeScreener: true }), [loadScope]);
  const refresh = useCallback((includeScreener?: boolean) => loadScope("opportunities", { force: true, includeScreener }), [loadScope]);

  return (
    <OpportunitiesPage
      data={data}
      loading={loading}
      scopeStatus={scopeStatus.opportunities}
      onOpenTicker={openTicker}
      onLoadScreener={loadScreener}
      onRefresh={refresh}
    />
  );
}
