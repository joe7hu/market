import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { SuperinvestorsPage } from "../views/genericPages";

export function SuperinvestorsRoute() {
  const { data, model, openTicker } = useMarketData();
  usePanelScope("superinvestors");

  return <SuperinvestorsPage data={data} model={model} onOpenTicker={openTicker} />;
}
