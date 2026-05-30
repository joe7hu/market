import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { SettingsPage } from "../views/genericPages";

export function SettingsRoute() {
  const { data } = useMarketData();
  usePanelScope("settings");

  return <SettingsPage data={data} />;
}
