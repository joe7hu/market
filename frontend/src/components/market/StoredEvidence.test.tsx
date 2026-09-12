import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { StoredEvidence } from "./StoredEvidence";

it("renders original nested evidence as escaped readable text", () => {
  const html = renderToStaticMarkup(<StoredEvidence title="Original evidence" value={{ measured_value: 0, threshold: 0.2, source: "<script>alert('source')</script>", missing: null }} />);
  expect(html).toContain("measured value");
  expect(html).toContain(">0</span>");
  expect(html).toContain("0.2");
  expect(html).toContain("Unavailable");
  expect(html).toContain("&lt;script&gt;");
  expect(html).not.toContain("<script>");
});
