import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Toggle } from "./settings";

describe("settings Toggle", () => {
  it("has the supplied accessible name", () => {
    const html = renderToStaticMarkup(<Toggle label="Enable News" checked disabled={false} onChange={() => undefined} />);

    expect(html).toContain('aria-label="Enable News"');
    expect(html).toContain('type="checkbox"');
  });
});
