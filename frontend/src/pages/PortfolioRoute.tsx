import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { PortfolioPage } from "../views/portfolio";

export function PortfolioRoute() {
  const { data, model, loading, loadScope, openTicker, scopeStatus } = useMarketData();
  usePanelScope("portfolio");

  return <PortfolioPage data={data} model={model} loading={loading} scopeStatus={scopeStatus.portfolio} onOpenTicker={openTicker} onRefresh={(force) => loadScope("portfolio", force ? { force: true } : undefined)} />;
}
