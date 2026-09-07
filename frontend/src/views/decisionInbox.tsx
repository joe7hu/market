import { Button } from "@/components/ui/button";
import { useState } from "react";
import { setDecisionInboxUsefulness } from "@/api/options";

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

export function InboxUsefulnessControls({ itemId, useful }: { itemId: string; useful?: boolean | null }) {
  const [rating, setRating] = useState(useful ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function rate(value: boolean) {
    setBusy(true);
    setError("");
    try { const saved = await setDecisionInboxUsefulness(itemId, value); setRating(saved.useful); }
    catch { setError("Feedback could not be saved. Try again."); }
    finally { setBusy(false); }
  }
  return <div className="space-y-1 border-t border-border pt-2">
    <p className="text-xs text-muted-foreground">Was this review useful?</p>
    <div role="group" aria-label="Review usefulness" className="flex gap-2">
      <Button type="button" size="sm" variant={rating === true ? "default" : "outline"} disabled={busy} aria-pressed={rating === true} onClick={() => void rate(true)}>Helpful</Button>
      <Button type="button" size="sm" variant={rating === false ? "default" : "outline"} disabled={busy} aria-pressed={rating === false} onClick={() => void rate(false)}>Not helpful</Button>
    </div>
    {rating !== null ? <p role="status" className="text-xs text-muted-foreground">Feedback saved. This does not change trade performance.</p> : null}
    {error ? <p role="alert" className="text-xs text-destructive">{error}</p> : null}
  </div>;
}
