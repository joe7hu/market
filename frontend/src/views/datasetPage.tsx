import type { ReactNode } from "react";

import { DataGridSection } from "./dataGridSection";
import { WorkspacePage, type OpenTicker } from "./workspacePage";
import type { DatasetProfile } from "@/viewModels/datasetProfiles";

export function DatasetPage({ profile, onOpenTicker, beforeTables }: { profile: DatasetProfile; onOpenTicker?: OpenTicker; beforeTables?: ReactNode }) {
  return (
    <WorkspacePage
      eyebrow={profile.eyebrow}
      title={profile.title}
      subtitle={profile.subtitle}
      metrics={profile.metrics}
    >
      {beforeTables}
      {profile.sections.map((section) => <DataGridSection key={section.title} title={section.title} rows={section.rows} onOpenTicker={onOpenTicker} />)}
    </WorkspacePage>
  );
}
