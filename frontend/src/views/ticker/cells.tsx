import { StatusBadge } from "@/components/market/workstation";
import { toneFromText } from "@/views/rowFormat";

import type { MetricCell } from "./data";

export function DecisionStat({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="rounded-md border border-border bg-background px-3 py-2">
      <div className="text-xs font-medium uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 text-lg font-semibold">{value}</div>
      <div className="mt-1 text-xs leading-5 text-muted-foreground">{detail}</div>
    </div>
  );
}

export function ReasonList({ title, rows, empty }: { title: string; rows: string[]; empty: string }) {
  return (
    <section>
      <h3 className="mb-2 text-sm font-semibold">{title}</h3>
      {rows.length ? (
        <ul className="space-y-2 text-sm leading-6">
          {rows.map((row, index) => <li key={`${title}-${index}`}>{row}</li>)}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">{empty}</p>
      )}
    </section>
  );
}

export function MetricGrid({ rows, empty }: { rows: MetricCell[]; empty: string }) {
  if (!rows.length) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <dl className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
      {rows.map(([label, value, detail]) => (
        <div key={label} className="min-h-24 rounded-md border border-border bg-background px-3 py-2">
          <dt className="text-xs font-medium uppercase text-muted-foreground">{label}</dt>
          <dd className="mt-1 break-words text-lg font-semibold tabular-nums">{value}</dd>
          <dd className="mt-1 text-xs leading-5 text-muted-foreground">{detail}</dd>
        </div>
      ))}
    </dl>
  );
}

export function SimpleTable({ rows, columns, empty }: { rows: Array<Record<string, string>>; columns: Array<[key: string, label: string]>; empty: string }) {
  if (!rows.length) return <div className="px-4 py-6 text-sm text-muted-foreground">{empty}</div>;
  return (
    <table className="w-full min-w-full text-sm">
      <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
        <tr>{columns.map(([, label]) => <th key={label} className="px-3 py-3 font-medium">{label}</th>)}</tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={index} className="border-b border-border align-top">
            {columns.map(([key]) => (
              <td key={key} className="max-w-[480px] px-3 py-3 leading-6">
                {(key === "signal" || key === "status") && row[key] !== "-" ? <StatusBadge tone={toneFromText(row[key])}>{row[key]}</StatusBadge> : row[key]}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
