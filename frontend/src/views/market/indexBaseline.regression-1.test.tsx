import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { IndexBaseline } from "./panels";

describe("IndexBaseline", () => {
  it("renders daily returns without nesting a table cell in card text", () => {
    const html = renderToStaticMarkup(<IndexBaseline rows={[{ symbol: "SPY", price: 100, return_1d: 0.01 }]} />);

    expect(html).toContain("Last daily move:");
    expect(html).not.toContain("<td");
  });
});
