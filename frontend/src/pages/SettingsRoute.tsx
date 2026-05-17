import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { SettingsPage } from "../marketViews";

export function SettingsRoute() {
  const { data } = useMarketData();
  usePanelScope("settings");

  return <SettingsPage data={data} />;
}
