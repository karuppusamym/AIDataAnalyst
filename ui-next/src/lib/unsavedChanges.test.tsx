import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import {
  confirmDiscardUnsaved,
  hasUnsavedChanges,
  registerDirtyReporter,
  resetUnsavedRegistryForTests,
  useUnsavedChanges,
} from "./unsavedChanges";

/* ---------------------------------------------------------------------------
   R11-S10 — the in-app half of the unsaved-changes warning.

   THE DEFECT these guard. `useUnsavedChanges` advertised two protections and
   delivered one. `beforeunload` was real, so closing the tab warned. The
   second -- "call before an in-app navigation that would discard the edit" --
   was a function it returned and that NOBODY called: its one consumer
   (`DescriptionEditor`) dropped the return value, and the shell's `navigate`
   had no way to learn that a form was dirty at all. Clicking any sidebar item
   with a half-written description on screen threw it away in silence.

   A test that only rendered the hook would have gone on passing through all of
   that, because the hook itself was never wrong -- the WIRING was missing. So
   what is asserted here is the wiring: that a dirty component is visible to a
   caller that never rendered it, which is the property the shell depends on.
--------------------------------------------------------------------------- */

function Editor({ dirty, message }: { dirty: boolean; message?: string }) {
  useUnsavedChanges(dirty, message);
  return <p>editor</p>;
}

beforeEach(() => {
  resetUnsavedRegistryForTests();
});

afterEach(() => {
  resetUnsavedRegistryForTests();
  vi.restoreAllMocks();
});

describe("the unsaved-changes registry", () => {
  it("reports nothing outstanding when no component is dirty", () => {
    render(<Editor dirty={false} />);
    expect(hasUnsavedChanges()).toBe(false);
  });

  it("makes a dirty component visible to a caller that never rendered it", () => {
    render(<Editor dirty />);
    // This is the shell's view: it does not know what is on screen, only that
    // something on it would lose work.
    expect(hasUnsavedChanges()).toBe(true);
  });

  it("stops reporting once the edit is saved", () => {
    const { rerender } = render(<Editor dirty />);
    expect(hasUnsavedChanges()).toBe(true);

    rerender(<Editor dirty={false} />);
    expect(hasUnsavedChanges()).toBe(false);
  });

  it("stops reporting when the dirty component unmounts", () => {
    const { unmount } = render(<Editor dirty />);
    expect(hasUnsavedChanges()).toBe(true);

    unmount();
    expect(hasUnsavedChanges()).toBe(false);
  });

  it("asks the user, and lets a navigation through only when they agree", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<Editor dirty />);

    expect(confirmDiscardUnsaved()).toBe(false);
    expect(confirm).toHaveBeenCalledWith("Discard your unsaved changes?");

    confirm.mockReturnValue(true);
    expect(confirmDiscardUnsaved()).toBe(true);
  });

  it("does not ask at all when nothing is dirty", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<Editor dirty={false} />);

    expect(confirmDiscardUnsaved()).toBe(true);
    expect(confirm).not.toHaveBeenCalled();
  });

  it("shows the component's own wording rather than a generic sentence", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Editor dirty message="This change set has unsaved items. Leave anyway?" />);

    confirmDiscardUnsaved();
    expect(confirm).toHaveBeenCalledWith("This change set has unsaved items. Leave anyway?");
  });

  it("warns before the tab is closed as well as before an in-app move", () => {
    render(<Editor dirty />);

    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });

  it("reports one warning when several things are dirty at once", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(
      <>
        <Editor dirty message="first" />
        <Editor dirty message="second" />
      </>,
    );

    confirmDiscardUnsaved();
    // One question, not one per form: a reviewer clicking away should not have
    // to dismiss a queue of prompts.
    expect(confirm).toHaveBeenCalledTimes(1);
  });
});

describe("the hook's own confirm, for a component guarding its own action", () => {
  function SelfGuarding({ dirty }: { dirty: boolean }) {
    const confirmLeave = useUnsavedChanges(dirty);
    return (
      <button type="button" onClick={() => confirmLeave() && screen.getByText("editor")}>
        close
      </button>
    );
  }

  it("returns false while dirty and the user declines", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<SelfGuarding dirty />);

    fireEvent.click(screen.getByRole("button", { name: "close" }));
    expect(confirm).toHaveBeenCalled();
  });
});

describe("a non-component source of unsaved state", () => {
  it("can register and unregister directly", () => {
    let unregister = () => {};
    act(() => {
      unregister = registerDirtyReporter(() => "a pending upload would be lost");
    });
    expect(hasUnsavedChanges()).toBe(true);

    act(() => unregister());
    expect(hasUnsavedChanges()).toBe(false);
  });

  it("ignores a reporter that says it is clean", () => {
    act(() => {
      registerDirtyReporter(() => null);
    });
    expect(hasUnsavedChanges()).toBe(false);
  });
});
