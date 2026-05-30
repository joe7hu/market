import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { FilingsPage } from "../views/genericPages";

export function FilingsRoute() {
  const { data, model, openTicker } = useMarketData();
  usePanelScope("filings");

  return <FilingsPage data={data} model={model} onOpenTicker={openTicker} />;
}
