import { Link } from "react-router-dom";

export function NotFoundRoute() {
  return (
    <section className="page-frame">
      <header className="page-header">
        <div>
          <p className="eyebrow">Route Not Found</p>
          <h1>Unknown Market Page</h1>
          <p>The React app is loaded, but this URL does not map to a Market workspace.</p>
        </div>
        <Link className="ghost-button" to="/">Dashboard</Link>
      </header>
    </section>
  );
}
