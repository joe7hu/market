import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { PortfolioPage } from "../marketViews";

export function PortfolioRoute() {
  const { model, loadScope, openTicker } = useMarketData();
  usePanelScope("portfolio");

  return <PortfolioPage model={model} onOpenTicker={openTicker} onRefresh={() => loadScope("portfolio")} />;
}
