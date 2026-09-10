import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { RouteErrorBoundary } from "./RouteErrorBoundary";
import { ApiError } from "../lib/http";

/* ---------------------------------------------------------------------------
   F21/T17. A route that throws must cost the route and not the app, and the
   thing that failed must be identifiable afterwards -- which for anything the
   server refused means its correlation id.
--------------------------------------------------------------------------- */

function Boom({ error }: { error: Error }): never {
  throw error;
}

beforeEach(() => {
  // React logs every caught error; the boundary logs its own. Neither is a
  // test failure, and both drown the output.
  vi.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RouteErrorBoundary", () => {
  it("renders the route when nothing throws", () => {
    render(
      <RouteErrorBoundary resetKey="catalog">
        <p>Catalog content</p>
      </RouteErrorBoundary>,
    );
    expect(screen.getByText("Catalog content")).toBeInTheDocument();
    expect(screen.queryByTestId("route-error")).not.toBeInTheDocument();
  });

  it("catches a render failure and names the route instead of blanking", () => {
    render(
      <RouteErrorBoundary resetKey="catalog" label="Catalog">
        <Boom error={new Error("kaboom")} />
      </RouteErrorBoundary>,
    );
    expect(screen.getByTestId("route-error")).toBeInTheDocument();
    expect(screen.getByText("Catalog could not be opened")).toBeInTheDocument();
  });

  it("reports the correlation id of an ApiError so support can find the request", () => {
    const error = new ApiError(500, "database unavailable", {
      code: "internal_error",
      correlationId: "corr-9f2",
    });
    render(
      <RouteErrorBoundary resetKey="quality" label="Data quality">
        <Boom error={error} />
      </RouteErrorBoundary>,
    );
    expect(screen.getByText("corr-9f2")).toBeInTheDocument();
    expect(screen.getByText("internal_error")).toBeInTheDocument();
    expect(screen.getByText("database unavailable")).toBeInTheDocument();
  });

  it("offers a reload for a failed chunk, because re-rendering a rejected lazy import cannot recover", () => {
    render(
      <RouteErrorBoundary resetKey="catalog" label="Catalog">
        <Boom error={new Error("Failed to fetch dynamically imported module: /assets/x.js")} />
      </RouteErrorBoundary>,
    );
    expect(screen.getByRole("button", { name: "Reload the app" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("offers an in-place retry for an ordinary render failure", () => {
    render(
      <RouteErrorBoundary resetKey="catalog" label="Catalog">
        <Boom error={new Error("kaboom")} />
      </RouteErrorBoundary>,
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("clears when the route changes, so a broken screen does not trap the outlet", () => {
    function Route({ id, fail }: { id: string; fail: boolean }) {
      return (
        <RouteErrorBoundary resetKey={id} label={id}>
          {fail ? <Boom error={new Error("kaboom")} /> : <p>Working {id}</p>}
        </RouteErrorBoundary>
      );
    }
    const { rerender } = render(<Route id="catalog" fail />);
    expect(screen.getByTestId("route-error")).toBeInTheDocument();

    rerender(<Route id="quality" fail={false} />);
    expect(screen.queryByTestId("route-error")).not.toBeInTheDocument();
    expect(screen.getByText("Working quality")).toBeInTheDocument();
  });

  it("reports the failure to its caller", () => {
    const onError = vi.fn();
    render(
      <RouteErrorBoundary resetKey="catalog" onError={onError}>
        <Boom error={new Error("kaboom")} />
      </RouteErrorBoundary>,
    );
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0]![0]).toBeInstanceOf(Error);
  });

  it("retrying re-renders the route rather than reloading the page", () => {
    let shouldFail = true;
    function Flaky() {
      if (shouldFail) throw new Error("kaboom");
      return <p>Recovered</p>;
    }
    render(
      <RouteErrorBoundary resetKey="catalog">
        <Flaky />
      </RouteErrorBoundary>,
    );
    shouldFail = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("Recovered")).toBeInTheDocument();
  });
});
