import { usePanelScope } from "../hooks";
import { SettingsPage } from "../views/settings";

export function SettingsRoute() {
  usePanelScope("settings");

  return <SettingsPage />;
}
