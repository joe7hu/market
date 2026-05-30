import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { ResearchPage } from "../views/research";

export function ResearchRoute() {
  const { data, openTicker } = useMarketData();
  usePanelScope("research");

  return <ResearchPage data={data} onOpenTicker={openTicker} />;
}
