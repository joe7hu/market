import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { ThesisMonitorPage } from "../views/thesisMonitor";

export function ThesisMonitorRoute() {
  const { data, model, openTicker } = useMarketData();
  usePanelScope("research");

  return <ThesisMonitorPage data={data} model={model} onOpenTicker={openTicker} />;
}
