import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";

import { ApiError } from "../lib/http";
import { AsyncState, CopyLinkButton, Dialog, FormErrors } from "./primitives";

/* ---------------------------------------------------------------------------
   The shared primitives (review 2026-09-05, F08 / F21 · T11/T17).

   These cover the two things that were declared rather than implemented: a
   modal that traps and restores focus and makes the rest of the document
   inert, and a copy action that tells the user when the clipboard refused
   instead of saying "Link copied" over unchanged clipboard contents.
--------------------------------------------------------------------------- */

function Harness({ initiallyOpen = false }: { initiallyOpen?: boolean }) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <div>
      <button onClick={() => setOpen(true)}>Open</button>
      <button>Behind</button>
      {open ? (
        <Dialog
          title="Reject this proposal"
          description="The rationale is recorded."
          onClose={() => setOpen(false)}
          footer={<button>Confirm</button>}
        >
          <input aria-label="Reason" />
        </Dialog>
      ) : null}
    </div>
  );
}

describe("Dialog", () => {
  it("moves focus in, and gives it back to the control that opened it", async () => {
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open" });
    opener.focus();
    fireEvent.click(opener);

    const dialog = await screen.findByRole("dialog", { name: "Reject this proposal" });
    expect(dialog.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(dialog, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // The half that is always forgotten: without this the keyboard user is
    // returned to the top of the document.
    expect(document.activeElement).toBe(opener);
  });

  it("keeps Tab inside itself", async () => {
    const { container } = render(<Harness initiallyOpen />);
    const dialog = await screen.findByRole("dialog");
    const focusables = within(dialog).getAllByRole("button");
    const last = focusables[focusables.length - 1]!;
    const behind = container.querySelector<HTMLButtonElement>("button:nth-of-type(2)")!;

    last.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });

    // Wrapped back to the first control in the dialog rather than escaping to
    // the button in the page underneath.
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(behind);
  });

  it("makes the rest of the document inert while it is open, and restores it", async () => {
    const { container } = render(<Harness />);
    // Testing Library appends its container straight onto `document.body`, so
    // the container IS the sibling the dialog has to make inert.
    const page = container;
    fireEvent.click(screen.getByRole("button", { name: "Open" }));

    const dialog = await screen.findByRole("dialog");
    expect(page).toHaveAttribute("aria-hidden", "true");
    expect(page).toHaveAttribute("inert");
    // Proof it is not cosmetic: the page behind is no longer reachable by an
    // accessible-name query, which is exactly what it is no longer reachable
    // by for a screen reader.
    expect(screen.queryByRole("button", { name: "Open" })).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Close Reject this proposal" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(page).not.toHaveAttribute("aria-hidden");
    expect(page).not.toHaveAttribute("inert");
  });
});

describe("CopyLinkButton", () => {
  it("copies a link that names its screen, so the recipient lands on the object", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    history.replaceState(null, "", "/?asset=old#/catalog");

    render(<CopyLinkButton target={{ screen: "quality", params: { incident: "inc_1" } }} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));

    await waitFor(() => expect(writeText).toHaveBeenCalled());
    const copied = String(writeText.mock.calls[0]![0]);
    expect(copied).toContain("#/quality");
    expect(copied).toContain("incident=inc_1");
    // Fields the target screen does not declare are dropped rather than
    // shipped to somebody who cannot use them.
    expect(copied).not.toContain("asset=old");
  });

  it("says so when the clipboard refuses, and exposes the link to copy by hand", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
      configurable: true,
    });
    history.replaceState(null, "", "/#/sources");

    render(<CopyLinkButton target={{ screen: "sources", params: { source: "src_1" } }} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));

    // The defect this replaces: "Link copied" over an unchanged clipboard, so
    // the user pastes whatever they had copied before into a ticket.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/refused clipboard access/i);
    const fallback = screen.getByLabelText("Link to copy") as HTMLInputElement;
    expect(fallback.value).toContain("#/sources");
    expect(fallback.value).toContain("source=src_1");
    expect(screen.getByRole("button", { name: "Copy link" })).toBeInTheDocument();
  });
});

describe("AsyncState", () => {
  it("says a link is inaccessible rather than rendering an empty screen", () => {
    render(
      <AsyncState
        error={new ApiError(403, "policy_denied", { correlationId: "cid-42" })}
        subject="this incident"
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(/do not have access to this incident/i);
    expect(screen.getByText("cid-42")).toBeInTheDocument();
    // 403 is not retryable: offering "Try again" would be a lie.
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });
});

describe("FormErrors", () => {
  it("lists the server's per-field validation messages instead of one sentence", () => {
    render(
      <FormErrors
        error={
          new ApiError(422, "invalid", {
            fieldErrors: [
              { field: "body.term_key", message: "must be lowercase" },
              { field: "body.definition", message: "is too short" },
            ],
          })
        }
      />,
    );

    expect(screen.getByText("term_key")).toBeInTheDocument();
    expect(screen.getByText(/must be lowercase/)).toBeInTheDocument();
    expect(screen.getByText("definition")).toBeInTheDocument();
  });
});
