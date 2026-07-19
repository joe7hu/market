import { Suspense, lazy } from "react";

const OptionsChainPage = lazy(async () => ({ default: (await import("../views/optionsChain")).OptionsChainPage }));

export function OptionsChainRoute() {
  return <Suspense fallback={<p className="text-sm text-muted-foreground">Loading option history workstation…</p>}><OptionsChainPage /></Suspense>;
}
