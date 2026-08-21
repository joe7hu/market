/** User-owned watchlist and thesis state requests. */

import type { components } from "../generated/apiSchema";
import { getJson, sendJson } from "../apiTransport";
import type { RowRecord } from "../types";

type ApiSchema = components["schemas"];

export type ThesisInput = Omit<ApiSchema["ThesisInput"], "direction" | "schema_version"> & {
  direction?: string | null;
  schema_version?: ApiSchema["ThesisInput"]["schema_version"];
};
export type WatchlistSymbolInput = ApiSchema["WatchlistSymbolInput"];
export type ThesisHistory = Omit<ApiSchema["ThesisHistoryResponse"], "revisions" | "review_events" | "symbol"> & {
  symbol: string | null;
  revisions: RowRecord[];
  review_events: RowRecord[];
};

export async function saveWatchlistSymbol(symbol: string): Promise<ApiSchema["TablePayloadResponse"]> {
  const payload = await sendJson<ApiSchema["WatchlistMutationResponse"]>("/api/watchlist/symbols", "POST", {
    symbol,
    asset_class: watchlistAssetClass(symbol),
    notes: "",
  });
  return payload.watchlist;
}

export async function deleteWatchlistSymbol(symbol: string): Promise<ApiSchema["TablePayloadResponse"]> {
  const payload = await sendJson<ApiSchema["WatchlistMutationResponse"]>(
    `/api/watchlist/symbols/${encodeURIComponent(symbol)}`,
    "DELETE",
  );
  return payload.watchlist;
}

export async function saveThesis(symbol: string, input: ThesisInput): Promise<ApiSchema["ThesisMutationResponse"]> {
  const direction = input.direction === "long" ? "bullish" : input.direction === "short" ? "bearish" : input.direction;
  const request: ApiSchema["ThesisInput"] = {
    ...input,
    direction: direction === "bullish" || direction === "bearish" ? direction : null,
    schema_version: input.schema_version ?? 3,
  };
  return sendJson<ApiSchema["ThesisMutationResponse"]>(`/api/theses/${encodeURIComponent(symbol)}`, "PUT", request);
}

export async function markThesisReviewed(
  symbol: string,
  outcome: "unchanged" | "updated" | "invalidated" | "closed" = "unchanged",
  notes = "",
): Promise<ApiSchema["ThesisReviewResponse"]> {
  return sendJson<ApiSchema["ThesisReviewResponse"]>(
    `/api/theses/${encodeURIComponent(symbol)}/review`,
    "POST",
    { outcome, notes },
  );
}

export async function getThesisHistory(symbol: string): Promise<ThesisHistory> {
  return getJson<ThesisHistory>(`/api/theses/${encodeURIComponent(symbol)}/history`);
}

function watchlistAssetClass(symbol: string): "crypto" | "equity" {
  const normalized = symbol.trim().toUpperCase();
  return normalized.endsWith("-USD") || ["BTC", "ETH", "SOL"].includes(normalized) ? "crypto" : "equity";
}
