import { describe, expect, it, vi } from "vitest";

import { getJson } from "./apiTransport";

describe("api transport", () => {
  it("coalesces identical signal-less GETs while the request is in flight", async () => {
    let resolveFetch: ((response: Response) => void) | undefined;
    const fetchPromise = new Promise<Response>((resolve) => { resolveFetch = resolve; });
    const fetchMock = vi.fn(() => fetchPromise);
    vi.stubGlobal("fetch", fetchMock);

    const first = getJson<{ ok: boolean }>("/api/today");
    const second = getJson<{ ok: boolean }>("/api/today");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    resolveFetch!(new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }));

    await expect(Promise.all([first, second])).resolves.toEqual([{ ok: true }, { ok: true }]);
    vi.unstubAllGlobals();
  });
});
