import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { PortfolioPage } from "../views/portfolio";

export function PortfolioRoute() {
  const { data, model, loadScope, openTicker } = useMarketData();
  usePanelScope("portfolio");

  return <PortfolioPage data={data} model={model} onOpenTicker={openTicker} onRefresh={() => loadScope("portfolio")} />;
}
