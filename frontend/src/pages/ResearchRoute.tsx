import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { ResearchPage } from "../marketViews";

export function ResearchRoute() {
  const { data, model, openTicker } = useMarketData();
  usePanelScope("research");

  return <ResearchPage data={data} model={model} onOpenTicker={openTicker} />;
}
