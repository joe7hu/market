import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { loadDecisionInbox } from "@/api";
import { PageHeader, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { JsonValue, RowRecord } from "@/types";
import { titleLabel, toneFromText } from "./rowFormat";

/** Actionable ticket and paper-order lifecycle events only. */
export function DecisionInboxPage() {
  const [items, setItems] = useState<RowRecord[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadFirst() {
    setLoading(true);
    setError(null);
    try {
      const payload = await loadDecisionInbox();
      setItems(payload.items);
      setCursor(payload.next_cursor);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Decision Inbox could not load.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void loadFirst(); }, []);

  async function loadMore() {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const payload = await loadDecisionInbox(cursor);
      setItems((current) => [...current, ...payload.items]);
      setCursor(payload.next_cursor);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "More Inbox events could not load.");
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <div>
      <PageHeader
        eyebrow="Decision Inbox"
        title="Actionable ticket updates"
        subtitle="READY changes, high-priority research, paper fills and exits, critical portfolio risk, and execution halts. Research events never create paper orders or Telegram delivery."
        actions={<Button type="button" variant="outline" disabled={loading} onClick={() => void loadFirst()}>Refresh</Button>}
      />
      {loading ? <p className="text-sm text-muted-foreground">Loading actionable events…</p> : null}
      {error ? <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm"><p>GET /api/decision-inbox: {error}</p><Button type="button" size="sm" variant="outline" className="mt-3" onClick={() => void loadFirst()}>Retry</Button></div> : null}
      {!loading && !error && !items.length ? <p className="rounded-md border border-dashed border-border p-5 text-sm text-muted-foreground">No actionable decision event is active.</p> : null}
      <div className="space-y-3">
        {items.map((item) => <InboxItem key={text(item, "id", `${text(item, "event_type")}-${text(item, "created_at")}`)} item={item} />)}
      </div>
      {cursor ? <Button type="button" variant="outline" className="mt-4" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "Loading…" : "Load more"}</Button> : null}
    </div>
  );
}

function InboxItem({ item }: { item: RowRecord }) {
  const payload = record(item.payload);
  const event = text(item, "event_type").toUpperCase();
  const state = text(payload, "state", event);
  const decisionId = text(item, "opportunity_id");
  const paperOrderId = text(item, "paper_order_id");
  const symbol = text(payload, "symbol", "Option decision");
  const lane = text(item, "lane", text(payload, "lane", "radar"));
  const reason = text(payload, "reason", text(payload, "primary_reason", text(payload, "primary_blocker")));
  const delivery = text(item, "delivery_status");
  return (
    <Card className="min-w-0">
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={toneFromText(state)}>{titleLabel(state)}</StatusBadge>
          <StatusBadge tone="info">{lane}</StatusBadge>
          {delivery ? <StatusBadge tone={toneFromText(delivery)}>{titleLabel(delivery)}</StatusBadge> : null}
          <span className="text-xs text-muted-foreground">{text(item, "created_at")}</span>
        </div>
        <div className="min-w-0">
          <p className="font-medium">{symbol} · {text(payload, "structure", "option").replaceAll("_", " ")}</p>
          <p className="mt-1 break-words text-sm text-muted-foreground">{reason || "Review the immutable ticket and current risk gate."}</p>
        </div>
        <div className="flex flex-wrap gap-2 text-sm">
          {decisionId ? <Button asChild size="sm" variant="outline"><Link to={`/options-radar?decision=${encodeURIComponent(decisionId)}`}>Open ticket</Link></Button> : null}
          {paperOrderId ? <span className="break-all rounded-md bg-muted px-2 py-1 text-xs text-muted-foreground">Paper order {paperOrderId}</span> : null}
          {text(payload, "expires_at") ? <span className="rounded-md bg-muted px-2 py-1 text-xs text-muted-foreground">Valid until {text(payload, "expires_at")}</span> : null}
        </div>
      </CardContent>
    </Card>
  );
}

function record(value: JsonValue | undefined): RowRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? value as RowRecord : {};
}

function text(row: RowRecord, key: string, fallback = ""): string {
  const value = row[key];
  return value === null || value === undefined ? fallback : String(value);
}
