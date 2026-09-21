import type { DecisionFunnel } from "@/api/panel";

export function DecisionFunnelPanel({ funnel }: { funnel: DecisionFunnel | null }) {
  return <section className="overflow-hidden rounded-xl border border-border bg-card" aria-labelledby="decision-funnel-title">
    <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
      <div><h2 id="decision-funnel-title" className="font-semibold">Decision funnel</h2>
        <p className="text-sm text-muted-foreground">Backend policy {funnel?.policy_version ?? "unavailable"}</p>
        <p className="text-xs text-muted-foreground">{funnel?.scope === "monitored_stock_lane" ? "Watched and held stocks only; historical symbols and crypto are outside this stock-validation lane." : "Published stock-validation lane."} Each symbol stops at its first blocker. A downstream stage not reached is not another outage.</p>
      </div>
      <span className="text-sm text-muted-foreground">{funnel ? `${funnel.actionable}/${funnel.total} actionable` : "Waiting for database status"}</span>
    </div>
    {funnel ? <div className="overflow-x-auto border-t border-border"><table className="w-full text-left text-sm">
      <thead className="bg-muted/40 text-xs uppercase text-muted-foreground"><tr><th className="px-4 py-2">Stage</th><th className="px-4 py-2">Reached / passed</th><th className="px-4 py-2">First blocker</th><th className="px-4 py-2">Owner / retry</th></tr></thead>
      <tbody>{(funnel.stages ?? []).map(stage => <tr key={stage.stage} className="border-t border-border align-top">
        <td className="px-4 py-3 font-medium">{stage.stage.replaceAll("_", " ")}</td>
        <td className="px-4 py-3">{stage.reached_count} reached · {stage.count} passed{stage.not_reached_count ? <div className="text-xs text-muted-foreground">{stage.not_reached_count} stopped upstream</div> : null}</td>
        <td className="px-4 py-3 text-muted-foreground">{stage.top_blockers?.[0] ? `${stage.top_blockers[0].reason} (${stage.top_blockers[0].count})` : stage.reached_count === 0 ? "Not reached — resolve the earlier blocker" : "Passed"}
          {(stage.diagnostic_blockers?.length ?? 0) > 0 ? <details className="mt-2 text-xs"><summary className="cursor-pointer">Independent diagnostics · {stage.available_count}/{stage.total} available</summary>{stage.diagnostic_blockers!.map(blocker => <p key={blocker.reason}>{blocker.reason}: {blocker.affected_symbols?.join(", ")} ({blocker.count})</p>)}</details> : null}
        </td><td className="px-4 py-3"><div>{stage.owner}</div><div className="text-muted-foreground">{stage.retry}</div></td>
      </tr>)}</tbody>
    </table></div> : null}
  </section>;
}
