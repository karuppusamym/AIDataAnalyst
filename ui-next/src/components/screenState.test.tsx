import { describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";

import { ApiError } from "../lib/http";
import { failureText, useAsyncResource, useSubmitAction } from "./screenState";

/* ---------------------------------------------------------------------------
   The controllers the five governed-object screens now share (R06).

   These assert the two behaviours that were NOT uniform before the
   extraction, and that no screen test can reach on its own because it needs
   two overlapping requests or two submits in one tick:

     - a superseded response is discarded even when it arrives last;
     - a second submit dispatched in the same tick as the first is refused.

   jsdom, not a browser: these are logic assertions about ordering and
   re-entrancy, not interactive validation.
--------------------------------------------------------------------------- */

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("failureText", () => {
  it("prefers the server's own detail over a paraphrase", () => {
    expect(failureText(new ApiError(403, "dbt integration is disabled for this organization"))).toBe(
      "dbt integration is disabled for this organization",
    );
  });

  it("falls back to an Error's message, then to a sentence", () => {
    expect(failureText(new Error("network down"))).toBe("network down");
    expect(failureText("not an error object")).toBe("The request failed.");
  });
});

describe("useAsyncResource", () => {
  function Probe({ load, keyValue }: { load: (key: string, signal: AbortSignal) => Promise<string>; keyValue: string }) {
    const resource = useAsyncResource<string>((signal) => load(keyValue, signal), [keyValue]);
    return (
      <div>
        <span data-testid="data">{resource.data ?? "—"}</span>
        <span data-testid="loading">{resource.loading ? "loading" : "idle"}</span>
        <span data-testid="error">{resource.error ?? "—"}</span>
      </div>
    );
  }

  it("discards a superseded response even when it resolves after the newer one", async () => {
    const slow = deferred<string>();
    const fast = deferred<string>();
    const load = vi.fn((key: string) => (key === "first" ? slow.promise : fast.promise));

    const { rerender } = render(<Probe load={load} keyValue="first" />);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));

    rerender(<Probe load={load} keyValue="second" />);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));

    await act(async () => {
      fast.resolve("second answer");
    });
    expect(screen.getByTestId("data")).toHaveTextContent("second answer");

    // The first request now finishes last. Aborting cancelled the request but
    // cannot order these two `setState` calls; the monotonic ticket does.
    await act(async () => {
      slow.resolve("first answer");
    });
    expect(screen.getByTestId("data")).toHaveTextContent("second answer");
  });

  it("does not report a superseded failure as the current error", async () => {
    const slow = deferred<string>();
    const fast = deferred<string>();
    const load = vi.fn((key: string) => (key === "first" ? slow.promise : fast.promise));

    const { rerender } = render(<Probe load={load} keyValue="first" />);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
    rerender(<Probe load={load} keyValue="second" />);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));

    await act(async () => {
      fast.resolve("second answer");
    });
    await act(async () => {
      slow.reject(new ApiError(500, "the first request failed"));
    });

    expect(screen.getByTestId("error")).toHaveTextContent("—");
    expect(screen.getByTestId("data")).toHaveTextContent("second answer");
  });

  it("starts in the loading state, so an enabled resource never flashes its empty state", () => {
    render(<Probe load={() => deferred<string>().promise} keyValue="first" />);
    expect(screen.getByTestId("loading")).toHaveTextContent("loading");
  });

  it("makes no request while disabled, and reports neither loading nor error", async () => {
    const load = vi.fn(() => Promise.resolve("never"));
    function Disabled() {
      const resource = useAsyncResource<string>(() => load(), [], { enabled: false });
      return <span data-testid="loading">{resource.loading ? "loading" : "idle"}</span>;
    }
    render(<Disabled />);
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("idle"));
    expect(load).not.toHaveBeenCalled();
  });
});

describe("useSubmitAction", () => {
  it("refuses a second submit dispatched in the same tick as the first", async () => {
    const gate = deferred<string>();
    const perform = vi.fn(() => gate.promise);
    let submit!: () => void;

    function Form() {
      const action = useSubmitAction<string>();
      submit = () => {
        // Two dispatches out of the same render: the closure both of them
        // captured says `submitting === false`, which is exactly the case a
        // state-based guard let through.
        void action.run(perform);
        void action.run(perform);
      };
      return <span data-testid="submitting">{action.submitting ? "working" : "idle"}</span>;
    }

    render(<Form />);
    act(() => submit());

    expect(perform).toHaveBeenCalledTimes(1);
    await act(async () => {
      gate.resolve("done");
    });
    expect(screen.getByTestId("submitting")).toHaveTextContent("idle");
  });

  it("reports a rejection in the server's own words and resolves to null", async () => {
    let outcome: string | null | undefined;
    function Form() {
      const action = useSubmitAction<string>();
      const [ran, setRan] = useState(false);
      if (!ran) {
        setRan(true);
        void action
          .run(() => Promise.reject(new ApiError(409, "someone else changed this record")))
          .then((value) => {
            outcome = value;
          });
      }
      return <span data-testid="error">{action.error ?? "—"}</span>;
    }

    render(<Form />);
    await waitFor(() => expect(screen.getByTestId("error")).toHaveTextContent("someone else changed this record"));
    expect(outcome).toBeNull();
  });
});
