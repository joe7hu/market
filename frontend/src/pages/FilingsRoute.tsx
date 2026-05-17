import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { FilingsPage } from "../marketViews";

export function FilingsRoute() {
  const { model, openTicker } = useMarketData();
  usePanelScope("filings");

  return <FilingsPage model={model} onOpenTicker={openTicker} />;
}
