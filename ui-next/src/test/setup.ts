import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});

// jsdom has no ResizeObserver; @tanstack/react-virtual (CatalogTable, UX-11;
// VirtualList, UX-15) needs one to exist to mount at all. A no-op stub is
// enough for a virtualized screen's shell to render without throwing.
if (!("ResizeObserver" in globalThis)) {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).ResizeObserver = ResizeObserverStub;
}

// jsdom never lays anything out, so `getBoundingClientRect()` always reports
// a zero-sized box -- and `@tanstack/virtual-core`'s `observeElementRect`
// measures the scroll container with exactly that call, synchronously, on
// mount (before the no-op ResizeObserver stub above ever gets a chance to
// fire), overriding any `initialRect` estimate a virtualizer config passes.
// A zero-height viewport means zero rows in range: UX-15's `VirtualList`
// (review queue, marketplace, refusals, Studio change sets) would mount with
// no rows ever rendered, which is not "a virtualized screen renders
// correctly," it's a jsdom measurement gap masquerading as one. This is the
// standard fix (also `@tanstack/virtual-core`'s own testing guidance): give
// every element a plausible nonzero box so the real measurement path runs
// end to end, rather than skip straight to asserting on `items` state.
{
  const real = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function (this: Element) {
    const rect = real.call(this);
    if (rect.width === 0 && rect.height === 0) {
      return {
        width: 1024,
        height: 640,
        top: 0,
        left: 0,
        right: 1024,
        bottom: 640,
        x: 0,
        y: 0,
        toJSON() {
          return this;
        },
      } as DOMRect;
    }
    return rect;
  };
}
