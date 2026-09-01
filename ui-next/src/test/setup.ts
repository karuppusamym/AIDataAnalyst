import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});

// jsdom has no ResizeObserver; @tanstack/react-virtual (CatalogTable, UX-11)
// needs one to exist to mount at all. A no-op stub is enough for tests that
// merely need the shell around a virtualized screen to render without
// throwing -- no test here asserts on virtualization behaviour itself.
if (!("ResizeObserver" in globalThis)) {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).ResizeObserver = ResizeObserverStub;
}
