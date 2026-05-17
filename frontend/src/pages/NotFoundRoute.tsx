import { Link } from "react-router-dom";
import { EmptyState, PageFrame } from "../components/primitives";

export function NotFoundRoute() {
  return (
    <PageFrame
      eyebrow="Route Not Found"
      title="Unknown Market Page"
      subtitle="The React app is loaded, but this URL does not map to a Market workspace."
      action={
        <Link className="ghost-button" to="/">Dashboard</Link>
      }
    >
      <EmptyState title="No workspace at this route" detail="Use the sidebar or return to the decision brief." />
    </PageFrame>
  );
}
