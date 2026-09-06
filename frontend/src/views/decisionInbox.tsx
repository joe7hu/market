import { Button } from "@/components/ui/button";

export type InboxStateChange = { state: "acknowledged" | "snoozed" | "dismissed" | "review_complete"; snoozed_until?: string; dismiss_reason?: string };

export function InboxStateControls({ itemId, busy, onState }: { itemId: string; busy: boolean; onState: (itemId: string, body: InboxStateChange) => void }) {
  const dismiss = () => {
    const reason = window.prompt("Why dismiss this event?")?.trim();
    if (reason) onState(itemId, { state: "dismissed", dismiss_reason: reason });
  };
  return (
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => onState(itemId, { state: "acknowledged" })}>Acknowledge</Button>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => onState(itemId, { state: "snoozed", snoozed_until: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString() })}>Snooze 1 day</Button>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => onState(itemId, { state: "review_complete" })}>Review complete</Button>
          <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={dismiss}>Dismiss…</Button>
        </div>
  );
}
