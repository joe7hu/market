import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { SourcesPage } from "../marketViews";

export function SourcesRoute() {
  const { data, openTicker } = useMarketData();
  usePanelScope("sources");

  return <SourcesPage data={data} onOpenTicker={openTicker} />;
}
