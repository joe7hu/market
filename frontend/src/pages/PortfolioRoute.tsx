import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { PortfolioPage } from "../views/portfolio";
import { Phase4SharedDecision } from "@/components/market/phase4SharedDecision";

export function PortfolioRoute() {
  const { data, model, loading, loadScope, openTicker, scopeStatus } = useMarketData();
  usePanelScope("portfolio");

  return <><Phase4SharedDecision data={data} scope="portfolio" status={scopeStatus?.portfolio} onRetry={() => void loadScope("portfolio", { force: true })} /><PortfolioPage data={data} model={model} loading={loading} scopeStatus={scopeStatus?.portfolio} onOpenTicker={openTicker} onRefresh={(force) => loadScope("portfolio", force ? { force: true } : undefined)} /></>;
}
