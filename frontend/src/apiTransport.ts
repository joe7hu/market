/** The only browser-to-API transport seam. Domain modules own request paths. */

const inFlightGets = new Map<string, Promise<unknown>>();

async function parseJson<T>(response: Response, path: string): Promise<T> {
  if (!response.ok) {
    const text = await response.text();
    let message = text || `${response.status} ${response.statusText}`;
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      if (typeof parsed.detail === "string") message = parsed.detail;
    } catch {
      // Keep the raw response text when the server does not return JSON.
    }
    throw new Error(message);
  }

  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    const text = await response.text();
    throw new Error(`Expected JSON from ${path}, got ${contentType || "unknown"}: ${text.slice(0, 40)}`);
  }
  return (await response.json()) as T;
}

async function requestGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${path}${path.includes("?") ? "&" : "?"}_=${Date.now()}`, {
    cache: "no-store",
    signal,
    headers: { Accept: "application/json" },
  });
  return parseJson<T>(response, path);
}

export function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  if (signal) return requestGet<T>(path, signal);
  const existing = inFlightGets.get(path);
  if (existing) return existing as Promise<T>;
  const request = requestGet<T>(path).finally(() => {
    if (inFlightGets.get(path) === request) inFlightGets.delete(path);
  });
  inFlightGets.set(path, request);
  return request;
}

export async function sendJson<T>(path: string, method: "POST" | "PUT" | "DELETE", body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return parseJson<T>(response, path);
}

export async function patchJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "PATCH",
    cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return parseJson<T>(response, path);
}
