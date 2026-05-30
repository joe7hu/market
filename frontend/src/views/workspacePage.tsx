import type { ReactNode } from "react";

import { MetricTile, PageHeader } from "@/components/market/workstation";
import type { Tone } from "@/ui/tone";

export type OpenTicker = (symbol: string) => void;
export type MetricSpec = [string, ReactNode, string, Tone];

export function WorkspacePage({ eyebrow, title, subtitle, actions, metrics = [], children }: { eyebrow: string; title: string; subtitle: string; actions?: ReactNode; metrics?: MetricSpec[]; children: ReactNode }) {
  return (
    <section>
      <PageHeader eyebrow={eyebrow} title={title} subtitle={subtitle} actions={actions} />
      {metrics.length ? (
        <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {metrics.map(([label, value, caption, tone]) => <MetricTile key={label} label={label} value={value} caption={caption} tone={tone} />)}
        </div>
      ) : null}
      <div className="space-y-4">{children}</div>
    </section>
  );
}
