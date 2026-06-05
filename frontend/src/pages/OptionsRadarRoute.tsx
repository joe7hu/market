import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { OptionsRadarPage } from "../views/optionsRadar";

export function OptionsRadarRoute() {
  const { data, loadScope, openTicker } = useMarketData();
  usePanelScope("options-radar");

  return <OptionsRadarPage data={data} onOpenTicker={openTicker} onRefresh={() => loadScope("options-radar")} />;
}
