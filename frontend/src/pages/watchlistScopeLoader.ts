import type { PanelScope } from "../marketData";
import type { PanelScopeOptions } from "../api";

export type ScopeLoader = (scope: PanelScope, options?: PanelScopeOptions) => Promise<void>;

export const CANDIDATE_PAGE_SIZE = 80;

export async function loadVisibleWatchlistScopes(loadScope: ScopeLoader): Promise<void> {
  await loadScope("watchlist-watched");
  await loadScope("watchlist-unwatched", { offset: 0, limit: CANDIDATE_PAGE_SIZE, append: false });
}
