import { afterEach, describe, expect, it, vi } from "vitest";
import { startMarketDiagnostics, type DiagnosticTool } from "./webmcp";

const cleanups: Array<() => void> = [];
afterEach(() => { cleanups.splice(0).forEach(stop => stop()); vi.unstubAllGlobals(); });
async function register() {
  const tools: DiagnosticTool[] = [];
  const signals: AbortSignal[] = [];
  const context = { registerTool: vi.fn(async (tool: DiagnosticTool, options: { signal: AbortSignal }) => { tools.push(tool); signals.push(options.signal); }) };
  const stop = startMarketDiagnostics(context, { enabled: true });
  cleanups.push(stop);
  await vi.waitFor(() => expect(tools).toHaveLength(5));
  return { tools, signals, stop, context };
}
const invoke = (tool: DiagnosticTool, args: unknown = {}) => tool.execute(args, { signal: new AbortController().signal }).then(JSON.parse);
function json(data: unknown) { return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } }); }

describe("optional read-only WebMCP adapter", () => {
  it("does nothing by default and when the experimental API is absent", () => {
    const registerTool = vi.fn();
    startMarketDiagnostics({ registerTool })();
    startMarketDiagnostics(undefined, { enabled: true })();
    expect(registerTool).not.toHaveBeenCalled();
  });
  it("registers five read-only tools and reads only the existing same-origin GET routes", async () => {
    const fetch = vi.fn(async () => json({ status: { ready: true }, tables: { opportunities_ranked: { rows: [], count: 0 } } }));
    vi.stubGlobal("fetch", fetch);
    const { tools, signals, stop } = await register();
    for (const tool of tools) {
      expect(tool.annotations).toEqual({ readOnlyHint: true, untrustedContentHint: true, consequentialHint: false });
      expect(tool.inputSchema.additionalProperties).toBe(false);
      const args = tool.name === "market_inspect_paper_trade" ? { trade_id: "00000000-0000-0000-0000-000000000001" } : {};
      expect(await invoke(tool, args)).toMatchObject({ ok: true, read_only: true });
    }
    expect(fetch).toHaveBeenCalledTimes(6);
    for (const [path, options] of fetch.mock.calls as unknown as Array<[string, RequestInit]>) {
      expect(path).toMatch(/^\/api\/(workstation\/status|today|panel-snapshot|paper\/)/);
      expect(options.method ?? "GET").toBe("GET");
      expect(options.signal).toBeInstanceOf(AbortSignal);
      expect(path).not.toMatch(/settings|refresh|jobs|initialize|https:/);
    }
    stop();
    expect(signals.every(signal => signal.aborted)).toBe(true);
    expect(await invoke(tools[0])).toMatchObject({ ok: false, error: "disabled" });
    expect(fetch).toHaveBeenCalledTimes(6);
  });
  it.each([0, 51, 1.5, "5", null])("rejects invalid row limits %s before any request", async limit => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const { tools } = await register();
    expect(await invoke(tools[2], { limit })).toMatchObject({ ok: false, error: "invalid_arguments" });
    expect(fetch).not.toHaveBeenCalled();
  });
  it("rejects path injection, arbitrary URLs, missing IDs and invalid input shapes", async () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const { tools } = await register();
    for (const args of [null, [], "{}", { url: "https://example.com" }]) {
      expect(await invoke(tools[0], args)).toMatchObject({ error: "invalid_arguments" });
    }
    expect(await invoke(tools[3], { symbol: "../../secret?token=x" })).toMatchObject({ error: "invalid_arguments" });
    expect(await invoke(tools[4])).toMatchObject({ error: "invalid_arguments" });
    expect(await invoke(tools[4], { trade_id: "../../orders" })).toMatchObject({ error: "invalid_arguments" });
    expect(fetch).not.toHaveBeenCalled();
  });
  it("normalizes symbol filters and preserves blocked/partial server states", async () => {
    const fetch = vi.fn(async () => json({ status: "partial", blockers: ["paper_mark_missing"] })); vi.stubGlobal("fetch", fetch);
    const { tools } = await register();
    const result = await invoke(tools[3], { symbol: "nvda", limit: 3 });
    expect(result.data.performance).toMatchObject({ status: "partial", blockers: ["paper_mark_missing"] });
    expect(String((fetch.mock.calls as unknown as Array<[string]>)[0]?.[0])).toContain("symbol=NVDA");
    expect(String((fetch.mock.calls as unknown as Array<[string]>)[0]?.[0])).toContain("limit=3");
  });
  it("bounds output and hides private transport errors rather than returning healthy empty data", async () => {
    const fetch = vi.fn(async () => json({ body: "x".repeat(300_000) })); vi.stubGlobal("fetch", fetch);
    const { tools } = await register();
    expect(await invoke(tools[0])).toMatchObject({ ok: false, error: "result_too_large" });
    fetch.mockRejectedValueOnce(new Error("database-password=private"));
    const failure = await invoke(tools[0]);
    expect(failure).toMatchObject({ ok: false, error: "read_failed" });
    expect(JSON.stringify(failure)).not.toContain("private");
  });
  it("limits concurrency and cancels outstanding requests on cleanup", async () => {
    vi.stubGlobal("fetch", vi.fn((_path, options: RequestInit) => new Promise((_resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    })));
    const { tools, stop } = await register();
    const first = invoke(tools[0]), second = invoke(tools[1]);
    expect(await invoke(tools[0])).toMatchObject({ error: "busy" });
    stop();
    expect(await first).toMatchObject({ error: "cancelled_or_timed_out" });
    expect(await second).toMatchObject({ error: "cancelled_or_timed_out" });
  });
  it("rolls back partial registration and reports the adapter unavailable", async () => {
    const signals: AbortSignal[] = [];
    const onError = vi.fn();
    const stop = startMarketDiagnostics({ registerTool: async (_tool, { signal }) => {
      signals.push(signal);
      if (signals.length === 2) throw new Error("unsupported API");
    } }, { enabled: true, onError });
    cleanups.push(stop);
    await vi.waitFor(() => expect(onError).toHaveBeenCalledTimes(1));
    expect(signals).toHaveLength(2);
    expect(signals.every(signal => signal.aborted)).toBe(true);
  });
});
