import { useParams } from "react-router-dom";
import { useTicker } from "../hooks";
import { useMarketData } from "../marketData";
import { TickerPage } from "../marketViews";

export function TickerRoute() {
  const params = useParams();
  const symbol = (params.symbol ?? "").toUpperCase();
  const ticker = useTicker(symbol);
  const { data, model, openTicker } = useMarketData();

  return <TickerPage symbol={symbol} ticker={ticker} model={model} data={data} onOpenTicker={openTicker} />;
}
