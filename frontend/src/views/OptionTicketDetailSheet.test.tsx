import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OptionTicketLoadError } from "./OptionTicketDetailSheet";

describe("OptionTicketLoadError", () => {
  it("shows a safe recovery message without API or parser details", () => {
    const html = renderToStaticMarkup(<OptionTicketLoadError onRetry={() => undefined} />);

    expect(html).toContain("Ticket detail could not load.");
    expect(html).toContain("Check the decision link or retry.");
    expect(html).toContain('role="alert"');
    expect(html).not.toContain("/api/options/tickets");
    expect(html).not.toContain("uuid_parsing");
  });
});
