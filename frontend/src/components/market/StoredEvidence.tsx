import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/** React text rendering only: stored source content is never executable HTML. */
export function EvidenceFields({ value }: { value: unknown }) {
  if (value == null || value === "") return <span className="text-muted-foreground">Unavailable</span>;
  if (typeof value !== "object") return <span className="whitespace-pre-wrap break-words">{String(value)}</span>;
  const entries = Object.entries(value);
  if (!entries.length) return <span className="text-muted-foreground">No stored evidence</span>;
  return <dl className="space-y-2">{entries.map(([key, item]) => <div key={key} className="border-l border-border pl-3"><dt className="text-xs font-medium text-muted-foreground">{key.replaceAll("_", " ")}</dt><dd className="mt-1 text-sm">{item && typeof item === "object" ? <details><summary className="cursor-pointer">{Array.isArray(item) ? `${item.length} records` : String(item.trial_key ?? item.gate_code ?? item.event ?? "Stored details")}</summary><div className="mt-2"><EvidenceFields value={item} /></div></details> : <EvidenceFields value={item} />}</dd></div>)}</dl>;
}

export function StoredEvidence({ title, value }: { title: string; value: unknown }) {
  return <Card><CardHeader><CardTitle>{title}</CardTitle></CardHeader><CardContent><EvidenceFields value={value} /></CardContent></Card>;
}
