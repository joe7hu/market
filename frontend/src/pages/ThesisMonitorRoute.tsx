import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { ThesisMonitorPage } from "../views/thesisMonitor";

export function ThesisMonitorRoute() {
  const { data, openTicker, loadScope } = useMarketData();
  usePanelScope("thesis-monitor");

  return <ThesisMonitorPage data={data} onOpenTicker={openTicker} onReload={() => loadScope("thesis-monitor")} />;
}
