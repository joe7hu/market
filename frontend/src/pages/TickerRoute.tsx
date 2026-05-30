import { useParams } from "react-router-dom";
import { usePanelScope, useTicker } from "../hooks";
import { useMarketData } from "../marketData";
import { TickerPage } from "../views/ticker";

export function TickerRoute() {
  const params = useParams();
  const symbol = (params.symbol ?? "").toUpperCase();
  const ticker = useTicker(symbol);
  const { data, openTicker } = useMarketData();
  usePanelScope("feed");

  return <TickerPage symbol={symbol} ticker={ticker} data={data} onOpenTicker={openTicker} />;
}
