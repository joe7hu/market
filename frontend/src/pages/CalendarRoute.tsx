import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { CalendarPage } from "../marketViews";

export function CalendarRoute() {
  const { model, openTicker } = useMarketData();
  usePanelScope("calendar");

  return <CalendarPage model={model} onOpenTicker={openTicker} />;
}
