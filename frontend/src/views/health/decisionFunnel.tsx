import type { DecisionFunnel } from "@/api/panel";

export function DecisionFunnelPanel({ funnel }: { funnel: DecisionFunnel | null }) {
  return (
    <section className="overflow-hidden rounded-xl border border-border bg-card" aria-labelledby="decision-funnel-title">
      <div className="flex items-center justify-between px-4 py-3">
        <div>
          <h2 id="decision-funnel-title" className="font-semibold">Decision funnel</h2>
          <p className="text-sm text-muted-foreground">Backend policy {funnel?.policy_version ?? "unavailable"}</p>
        </div>
        <span className="text-sm text-muted-foreground">
          {funnel ? `${funnel.actionable}/${funnel.total} actionable` : "Waiting for database status"}
        </span>
      </div>
      {funnel ? (
        <div className="overflow-x-auto border-t border-border">
          <table className="w-full text-left text-sm">
            <thead className="bg-muted/40 text-xs uppercase text-muted-foreground">
              <tr><th className="px-4 py-2">Stage</th><th className="px-4 py-2">Reach</th><th className="px-4 py-2">Top blocker</th><th className="px-4 py-2">Owner / retry</th></tr>
            </thead>
            <tbody>
              {(funnel.stages ?? []).map((stage) => (
                <tr key={stage.stage} className="border-t border-border align-top">
                  <td className="px-4 py-3 font-medium">{stage.stage.replaceAll("_", " ")}</td>
                  <td className="px-4 py-3">{stage.count}/{stage.total} · {(stage.percentage * 100).toFixed(0)}%</td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {stage.top_blockers?.[0]
                      ? `${stage.top_blockers[0].reason} (${stage.top_blockers[0].count})`
                      : "None"}
                  </td>
                  <td className="px-4 py-3"><div>{stage.owner}</div><div className="text-muted-foreground">{stage.retry}</div></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
