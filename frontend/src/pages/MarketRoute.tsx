import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { MarketContextPage } from "../marketViews";

export function MarketRoute() {
  const { data } = useMarketData();
  usePanelScope("market");

  return <MarketContextPage data={data} />;
}
