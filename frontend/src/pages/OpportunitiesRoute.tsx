import { useCallback } from "react";
import { useMarketData } from "../marketData";
import { OpportunitiesPage } from "../views/opportunities";

export function OpportunitiesRoute() {
  const { data, loading, loadScope, openTicker, scopeStatus } = useMarketData();
  const loadScreener = useCallback(() => loadScope("opportunities", { force: true, includeScreener: true }), [loadScope]);
  const refresh = useCallback((includeScreener?: boolean) => loadScope("opportunities", { force: true, includeScreener }), [loadScope]);
  const loadMore = useCallback((screener: boolean) => {
    const table = screener ? data.screener : data.opportunitiesRanked;
    const offset = (table?.offset ?? 0) + (table?.limit ?? 120);
    return loadScope("opportunities", { offset, limit: 120, append: true, includeScreener: screener });
  }, [data.screener, data.opportunitiesRanked, loadScope]);
  return <OpportunitiesPage data={data} loading={loading} scopeStatus={scopeStatus.opportunities} onOpenTicker={openTicker} onLoadScreener={loadScreener} onRefresh={refresh} onLoadMore={loadMore} />;
}
