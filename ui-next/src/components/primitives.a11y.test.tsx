import { describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AsyncState, ConfirmDialog, Dialog, FormErrors, useToast } from "./primitives";
import { ApiError } from "../lib/http";
import { expectFocusStaysWithin } from "../test/a11y";

/* ---------------------------------------------------------------------------
   R11-C2 — the shared interaction primitives, driven from a real keyboard.

   `primitives.test.tsx` already covers these structurally, and its Tab test
   is `fireEvent.keyDown(dialog, { key: "Tab" })` — which proves the Dialog's
   own handler runs, and cannot prove what the browser does with focus,
   because jsdom has no sequential focus navigation to run.
   `@testing-library/user-event` supplies one. That is the difference between
   "the component handles Tab" and "the user cannot get out", and F21's
   worked example is a component that did the first and not the second.

   This file is separate rather than merged into `primitives.test.tsx` so the
   two kinds of proof stay legible — and so this row's additions do not
   collide with another session's edits to that file.
--------------------------------------------------------------------------- */

function DialogHarness() {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button onClick={() => setOpen(true)}>Open the dialog</button>
      <button>A control behind the dialog</button>
      {open ? (
        <Dialog title="Edit description" onClose={() => setOpen(false)} footer={<button>Save</button>}>
          <label>
            Description
            <textarea />
          </label>
          <button>Suggest a description</button>
        </Dialog>
      ) : null}
    </div>
  );
}

describe("Dialog, from the keyboard", () => {
  it("keeps 40 forward Tab presses inside itself", async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);
    await user.click(screen.getByRole("button", { name: "Open the dialog" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit description" });

    await expectFocusStaysWithin(user, dialog, 40);
  });

  it("wraps backwards too, so Shift+Tab off the first control does not leave", async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);
    await user.click(screen.getByRole("button", { name: "Open the dialog" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit description" });

    for (let press = 0; press < 12; press += 1) {
      await user.tab({ shift: true });
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it("gives focus back to the opener on Escape, not to the top of the document", async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);
    const opener = screen.getByRole("button", { name: "Open the dialog" });

    await user.click(opener);
    await screen.findByRole("dialog", { name: "Edit description" });
    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(opener);
  });
});

describe("ConfirmDialog, from the keyboard", () => {
  it("opens with focus in the rationale box and refuses to confirm while it is empty", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        title="Reject this proposal"
        requireReason
        reasonLabel="Why"
        destructive
        onConfirm={onConfirm}
        onCancel={() => {}}
      />,
    );

    const dialog = await screen.findByRole("dialog", { name: "Reject this proposal" });
    const reason = within(dialog).getByRole("textbox", { name: "Why" });
    expect(document.activeElement).toBe(reason);

    // The blocked state is conveyed by the control being disabled, not only by
    // colour: a disabled button is announced as unavailable.
    expect(within(dialog).getByRole("button", { name: "Confirm" })).toBeDisabled();

    await user.keyboard("duplicate of an approved edge");
    const confirm = within(dialog).getByRole("button", { name: "Confirm" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith("duplicate of an approved edge");
  });

  it("announces a failure inside the dialog as an alert", async () => {
    render(
      <ConfirmDialog
        title="Revoke access"
        error="A different reviewer must approve this."
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("A different reviewer must approve this.");
  });
});

describe("asynchronous outcomes are announced, not just rendered", () => {
  it("reports loading as a status and failure as an alert", async () => {
    const { rerender } = render(<AsyncState loading>{null}</AsyncState>);
    expect(screen.getByRole("status")).toHaveTextContent("Loading…");

    rerender(
      <AsyncState error={new ApiError(403, "Forbidden")} subject="this incident">
        {null}
      </AsyncState>,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/do not have access to this incident/i);
  });

  it("puts a toast in a polite live region that exists before the message does", async () => {
    function ToastHarness() {
      const { show, node } = useToast();
      return (
        <div>
          <button onClick={() => show("Description published", "ok")}>Publish</button>
          {node}
        </div>
      );
    }
    const user = userEvent.setup();
    render(<ToastHarness />);

    // The region is mounted empty. A live region created at the same moment as
    // its text announces nothing, which is the usual way a "toast" is silent.
    const region = document.querySelector("[aria-live='polite']");
    expect(region).not.toBeNull();
    expect(region).toHaveTextContent("");

    await user.click(screen.getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(region).toHaveTextContent("Description published"));
  });

  it("moves focus to the validation summary when a save is rejected", async () => {
    function FormHarness() {
      const [error, setError] = useState<unknown>(null);
      return (
        <form>
          <FormErrors error={error} />
          <label>
            Name
            <input />
          </label>
          <button
            type="button"
            onClick={() =>
              setError(
                new ApiError(422, "Invalid", {
                  fieldErrors: [{ field: "body.name", message: "must not be blank" }],
                }),
              )
            }
          >
            Save
          </button>
        </form>
      );
    }
    const user = userEvent.setup();
    render(<FormHarness />);

    await user.click(screen.getByRole("button", { name: "Save" }));

    const summary = await screen.findByRole("alert");
    expect(summary).toHaveTextContent("must not be blank");
    // Submit sits below the fields; without this the user is left at the
    // bottom of the form with no idea which field the server rejected.
    expect(document.activeElement).toBe(summary);
  });
});
