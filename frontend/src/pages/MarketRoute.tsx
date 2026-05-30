import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { MarketContextPage } from "../views/genericPages";

export function MarketRoute() {
  const { data } = useMarketData();
  usePanelScope("market");

  return <MarketContextPage data={data} />;
}
