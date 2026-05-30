import { EmptyState } from "@/components/market/workstation";
import { WorkspacePage } from "./workspacePage";

export function SettingsPage() {
  return (
    <WorkspacePage eyebrow="Configuration" title="Settings" subtitle="Local application preferences and integration references.">
      <EmptyState title="No editable preferences" detail="Editable user preferences are not configured yet." />
    </WorkspacePage>
  );
}
