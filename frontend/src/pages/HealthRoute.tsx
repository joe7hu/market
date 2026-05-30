import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { HealthPage } from "../views/genericPages";

export function HealthRoute() {
  const { data, model } = useMarketData();
  usePanelScope("health");

  return <HealthPage data={data} model={model} />;
}
