import { Component, type ErrorInfo, type ReactNode } from "react";

import { Button } from "@/components/ui/button";

type RouteErrorBoundaryProps = {
  route: string;
  failedApis: string[];
  children: ReactNode;
};

type RouteErrorBoundaryState = { error: Error | null };

/** A small route boundary keeps a failed operational panel from becoming blank. */
export class RouteErrorBoundary extends Component<RouteErrorBoundaryProps, RouteErrorBoundaryState> {
  state: RouteErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): RouteErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Console output is intentional: it keeps the route, source, and React
    // component stack available to local diagnostics without exposing it in UI.
    console.error(`Route failed: ${this.props.route}`, error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <section className="rounded-md border border-destructive/40 bg-destructive/5 p-5">
        <h1 className="text-lg font-semibold">This page could not render.</h1>
        <p className="mt-2 text-sm text-muted-foreground">Route: {this.props.route}</p>
        <p className="mt-1 text-sm text-muted-foreground">Failed API candidates: {this.props.failedApis.join(", ")}</p>
        <p className="mt-3 break-words text-sm text-muted-foreground">{this.state.error.message}</p>
        <Button type="button" className="mt-4" variant="outline" onClick={() => this.setState({ error: null })}>Retry</Button>
      </section>
    );
  }
}
