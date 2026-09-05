import { describe, expect, it, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useUrlState } from "./useUrlState";

beforeEach(() => {
  history.replaceState(null, "", "/");
});

describe("useUrlState", () => {
  it("reads the current URL and writes patches back via history.replaceState", () => {
    history.replaceState(null, "", "/?q=orders");
    const { result } = renderHook(() => useUrlState());
    const [params] = result.current;
    expect(params.get("q")).toBe("orders");

    act(() => {
      const [, update] = result.current;
      update({ type: "TABLE" });
    });
    expect(result.current[0].get("q")).toBe("orders");
    expect(result.current[0].get("type")).toBe("TABLE");
    expect(new URLSearchParams(location.search).get("type")).toBe("TABLE");
  });

  it("preserves the active hash route when filters change", () => {
    history.replaceState(null, "", "/?q=orders#/catalog");
    const { result } = renderHook(() => useUrlState());

    act(() => result.current[1]({ type: "TABLE" }));

    expect(location.search).toBe("?q=orders&type=TABLE");
    expect(location.hash).toBe("#/catalog");
  });

  it("deletes a key when the patch value is null or empty", () => {
    history.replaceState(null, "", "/?q=orders&type=TABLE");
    const { result } = renderHook(() => useUrlState());

    act(() => {
      const [, update] = result.current;
      update({ type: null, q: "" });
    });
    expect(result.current[0].get("type")).toBeNull();
    expect(result.current[0].get("q")).toBeNull();
  });
});
