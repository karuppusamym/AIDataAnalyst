import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

import { ApiError } from "../lib/http";

/* ---------------------------------------------------------------------------
   The route outlet's error boundary (review 2026-09-05, F21 · T17).

   THE DEFECT this component exists to remove: every screen is a `React.lazy`
   chunk rendered inside a bare `<Suspense>`. `Suspense` handles the pending
   promise and nothing else, so a rejected one -- a chunk evicted by a deploy,
   a proxy that answered HTML to a `.js` request, an offline tab, or simply a
   screen that threw while rendering -- propagated to the root and unmounted
   the whole tree. The user got a white page with no message, no correlation
   id, and no way back other than guessing at a browser reload.

   THE INVARIANT: a failure inside one route may cost that route and nothing
   else. The shell -- navigation, scope, session state -- stays mounted and
   usable, and the failure is reported with whatever the server gave us to
   identify it.

   WHY RETRY IS TWO DIFFERENT ACTIONS. `React.lazy` memoises the rejected
   import promise: re-rendering the same lazy component after a chunk load
   failure replays the same rejection forever. So a chunk failure offers a
   reload, which actually refetches, while a render/data failure offers an
   in-place retry, which actually re-renders. Offering the wrong one is a
   button that looks like recovery and is not.

   THIS IS NOT the shared async/error/toast primitive set. Screens' own
   in-content loading and error states belong to `components/primitives.tsx`;
   this catches what those cannot -- the throw that has already escaped.
--------------------------------------------------------------------------- */

/** A failed dynamic import, as reported by browsers that all word it differently. */
function isChunkLoadFailure(error: Error): boolean {
  const text = `${error.name}: ${error.message}`;
  return (
    /ChunkLoadError/i.test(text) ||
    /Loading chunk [\w-]+ failed/i.test(text) ||
    /dynamically imported module/i.test(text) ||
    /Importing a module script failed/i.test(text) ||
    /error loading dynamically imported module/i.test(text)
  );
}

export interface RouteErrorBoundaryProps {
  children: ReactNode;
  /**
   * Changing this clears the error.
   *
   * The shell passes the screen id, so navigating away from a broken screen
   * works without a reload -- without it, one failed route would hold the
   * outlet in its error state for the rest of the session.
   */
  resetKey?: string;
  /** Human name of what failed, for the message. */
  label?: string;
  /** Reported alongside the message so a support request can be correlated. */
  onError?: (error: Error, info: ErrorInfo) => void;
}

interface RouteErrorBoundaryState {
  error: Error | null;
  resetKey: string | undefined;
}

export class RouteErrorBoundary extends Component<
  RouteErrorBoundaryProps,
  RouteErrorBoundaryState
> {
  state: RouteErrorBoundaryState = { error: null, resetKey: undefined };

  static getDerivedStateFromError(error: Error): Partial<RouteErrorBoundaryState> {
    return { error };
  }

  static getDerivedStateFromProps(
    props: RouteErrorBoundaryProps,
    state: RouteErrorBoundaryState,
  ): Partial<RouteErrorBoundaryState> | null {
    if (state.resetKey !== props.resetKey) {
      return { error: null, resetKey: props.resetKey };
    }
    return null;
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.props.onError?.(error, info);
    // The console is the only reporting sink this app has; leaving the stack
    // only in React's own dev overlay would lose it in a production build.
    console.error("Route failed to render", error, info.componentStack);
  }

  private retry = (): void => {
    this.setState({ error: null });
  };

  private reload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    const chunkFailure = isChunkLoadFailure(error);
    const apiError = error instanceof ApiError ? error : null;
    const label = this.props.label ?? "This page";

    return (
      <div className="routefail" role="alert" data-testid="route-error">
        <h2 className="routefail__title">{label} could not be opened</h2>
        <p className="routefail__detail">
          {chunkFailure
            ? "The code for this page could not be downloaded. This usually means the app was " +
              "updated while your tab was open, or the network dropped mid-request."
            : apiError
              ? apiError.message
              : "The page stopped while rendering. Nothing you were doing has been submitted."}
        </p>

        {apiError?.correlationId ? (
          <p className="routefail__ref">
            Support reference: <code>{apiError.correlationId}</code>
            {apiError.code ? <> · code <code>{apiError.code}</code></> : null}
          </p>
        ) : null}

        <div className="routefail__actions">
          {chunkFailure ? (
            <button className="routefail__primary" onClick={this.reload}>
              Reload the app
            </button>
          ) : (
            <button className="routefail__primary" onClick={this.retry}>
              Try again
            </button>
          )}
          <button
            className="routefail__secondary"
            onClick={() => {
              // The shell is still mounted, so this is a real escape hatch and
              // not a reload in disguise.
              window.location.hash = "#/home";
              this.retry();
            }}
          >
            Go to Overview
          </button>
        </div>

        <details className="routefail__more">
          <summary>Technical detail</summary>
          <pre>{`${error.name}: ${error.message}`}</pre>
        </details>
      </div>
    );
  }
}
