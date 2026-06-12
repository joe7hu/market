export type Tone = "good" | "warn" | "bad" | "info" | "muted";

const OPERATIONAL_STATUS_TONES: Record<string, Tone> = {
  ok: "good",
  live: "good",
  loaded: "good",
  succeeded: "good",
  success: "good",
  complete: "good",
  completed: "good",
  ready: "good",
  checked: "good",

  running: "info",
  pending: "info",
  queued: "info",
  configured: "info",

  partial: "warn",
  warning: "warn",
  warn: "warn",
  stale: "warn",
  blocked: "warn",
  degraded: "warn",
  not_loaded: "warn",
  not_ingested: "warn",
  missing_dependency: "warn",
  missing: "warn",
  auth_required: "warn",
  login_required: "warn",
  quote_entitlement_failure: "warn",
  rate_limited: "warn",
  unreachable: "warn",
  offline: "warn",

  failed: "bad",
  failure: "bad",
  error: "bad",
  critical: "bad",
  gateway_offline: "bad",
  repair_required: "bad",
  malformed_symbol: "bad",
  session_failure: "bad",

  disabled: "muted",
  skipped: "muted",
  none: "muted",
};

export function toneFromOperationalStatus(value: string | undefined | null): Tone | undefined {
  if (!value) return undefined;
  const normalized = value.trim().toLowerCase().replace(/[\s-]+/g, "_");
  return OPERATIONAL_STATUS_TONES[normalized];
}
