import { useEffect, useState } from "react";

import { loadStatus, type StatusPayload } from "@/api/panel";

type Release = {
  backend_commit?: string;
  frontend_build?: string;
  scheduler_release?: string;
};

const frontendBuild = typeof __MARKET_FRONTEND_BUILD__ === "string" ? __MARKET_FRONTEND_BUILD__ : "unknown";

function useBuildStatus() {
  const [status, setStatus] = useState<StatusPayload | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void loadStatus(controller.signal).then(setStatus).catch(() => undefined);
    return () => controller.abort();
  }, []);
  return status;
}

function releaseFrom(status: StatusPayload | null): Release {
  const metadata = status?.metadata;
  return metadata && typeof metadata === "object" && metadata.release && typeof metadata.release === "object"
    ? metadata.release as Release
    : {};
}

export function BuildMismatchBanner() {
  const release = releaseFrom(useBuildStatus());
  const backend = release.backend_commit;
  if (!import.meta.env.DEV || !backend || backend === "unknown" || frontendBuild === "unknown" || backend === frontendBuild) return null;
  return <div role="status" className="border-b border-amber-500/40 bg-amber-500/10 px-4 py-2 text-xs text-amber-900 dark:text-amber-100">Development build mismatch: this UI is {frontendBuild}, but the API is {backend}. Restart the matching frontend/API checkout before trusting visual results.</div>;
}

export function BuildIdentityCard() {
  const status = useBuildStatus();
  const release = releaseFrom(status);
  const metadata = status?.metadata;
  const schema = metadata && typeof metadata === "object" ? String(metadata.schema_revision ?? "unknown") : "unknown";
  return <section className="rounded-xl border border-border bg-card p-4" aria-label="Build identity"><div className="flex flex-wrap items-baseline justify-between gap-2"><h2 className="text-base font-semibold">Build identity</h2><span className="text-xs text-muted-foreground">Runtime contract</span></div><div className="mt-3 grid gap-3 text-sm sm:grid-cols-3"><div><div className="text-xs uppercase tracking-wide text-muted-foreground">UI build</div><div className="mt-1 font-mono font-semibold">{frontendBuild}</div></div><div><div className="text-xs uppercase tracking-wide text-muted-foreground">API build</div><div className="mt-1 font-mono font-semibold">{release.backend_commit ?? "unknown"}</div></div><div><div className="text-xs uppercase tracking-wide text-muted-foreground">Schema</div><div className="mt-1 font-mono font-semibold">{schema}</div></div></div>{status?.metadata?.schema_compatible === false ? <p className="mt-3 text-sm text-amber-700 dark:text-amber-300">The API reports an incompatible schema. Restart after applying the matching migration.</p> : null}</section>;
}
