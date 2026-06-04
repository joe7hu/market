import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { OptionsRadarPage } from "../views/optionsRadar";

export function OptionsRadarRoute() {
  const { data, openTicker } = useMarketData();
  usePanelScope("options-radar");

  return <OptionsRadarPage data={data} onOpenTicker={openTicker} />;
}
