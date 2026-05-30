import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { CalendarPage } from "../views/genericPages";

export function CalendarRoute() {
  const { data, model, openTicker } = useMarketData();
  usePanelScope("calendar");

  return <CalendarPage data={data} model={model} onOpenTicker={openTicker} />;
}
