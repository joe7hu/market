import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { OpportunitiesPage } from "../marketViews";

export function OpportunitiesRoute() {
  const { model, openTicker } = useMarketData();
  usePanelScope("opportunities");

  return <OpportunitiesPage model={model} onOpenTicker={openTicker} />;
}
