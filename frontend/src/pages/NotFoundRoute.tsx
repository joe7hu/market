import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { EmptyState, PageHeader } from "@/components/market/workstation";

export function NotFoundRoute() {
  return (
    <section>
      <PageHeader
        eyebrow="Route not found"
        title="Unknown Market Page"
        subtitle="The React app is loaded, but this URL does not map to a Market workspace."
        actions={<Button asChild variant="outline"><Link to="/today">Today</Link></Button>}
      />
      <EmptyState title="No workspace at this route" detail="Use the sidebar or return to the decision brief." />
    </section>
  );
}
