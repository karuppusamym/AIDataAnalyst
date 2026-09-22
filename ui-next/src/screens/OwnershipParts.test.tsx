import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

import type { BulkStewardshipOperationRead } from "../lib/types";
import type { Session } from "../lib/session";
import {
  EXPIRY_WARN_DAYS,
  HintedField,
  OwnershipConfirm,
  RequestedReview,
  describeExpiry,
  describeOperationStatus,
  listOr,
  stamp,
  stringParameter,
} from "./OwnershipParts";

const navigateTo = vi.fn<(screen: string, params?: Record<string, string>) => void>();
vi.mock("../lib/navigate", () => ({
  navigateTo: (screen: string, params?: Record<string, string>) => navigateTo(screen, params),
}));

/* `RequestedReview` offers "Open this review" only to a session the Review queue admits. */
let sessionRoles: string[] | undefined;
vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  return {
    ...actual,
    useSession: (): Session => ({
      state: "connected",
      me:
        sessionRoles === undefined
          ? null
          : {
              principal_id: "someone", principal_type: "USER", organization_id: null, roles: sessionRoles,
              persona: null, identity_provider: "DEVELOPMENT",
            },
      lapsed: false,
      lastSuccessAt: null,
      error: null,
      dataMode: "live",
      authMode: "development",
      authModeInferred: false,
      reload: () => undefined,
    }),
  };
});

/* ---------------------------------------------------------------------------
   The pieces the Ownership panels share (R11-AUD08, part 2): the words for an
   expiry and a status, and the two components whose accessibility is a property
   of the component rather than of any one panel.
--------------------------------------------------------------------------- */

const NOW = Date.parse("2026-09-21T12:00:00Z");
const DAY = 86_400_000;
const at = (days: number) => new Date(NOW + days * DAY).toISOString();

describe("describeExpiry", () => {
  it("says a row with no expiry has none, which is a fact about the row and not a warning", () => {
    expect(describeExpiry(null, NOW)).toEqual({ label: "no expiry set", tone: "mute" });
  });

  it("warns inside the window the server warns in, and not outside it", () => {
    expect(describeExpiry(at(EXPIRY_WARN_DAYS), NOW).tone).toBe("warn");
    expect(describeExpiry(at(EXPIRY_WARN_DAYS + 1), NOW).tone).toBe("mute");
    expect(describeExpiry(at(6), NOW)).toEqual({ label: "expires 2026-09-27 (in 6 days)", tone: "warn" });
    expect(describeExpiry(at(1), NOW).label).toBe("expires 2026-09-22 (in 1 day)");
  });

  it("says an expiry already past is past, not that the row is no longer an owner", () => {
    const past = describeExpiry(at(-3), NOW);

    expect(past.tone).toBe("bad");
    expect(past.label).toBe("past its expiry (2026-09-18, 3 days ago)");
    expect(past.label).not.toMatch(/^expired/);
    // The instant it passed reads as one day, never "0 days ago".
    expect(describeExpiry(new Date(NOW - 60_000).toISOString(), NOW).label).toContain("1 day ago");
  });

  it("shows a value it cannot read as it was sent rather than as a date it made up", () => {
    expect(describeExpiry("not a date", NOW)).toEqual({ label: "not a date", tone: "mute" });
  });
});

describe("describeOperationStatus", () => {
  it.each([
    ["REVIEW_REQUIRED", "waiting for review", "info"],
    ["APPLIED", "applied", "ok"],
    ["REJECTED", "rejected", "bad"],
    ["SOMETHING_NEW", "something new", "mute"],
  ])("%s reads as %s", (status, label, tone) => {
    expect(describeOperationStatus(status)).toEqual({ label, tone });
  });
});

describe("the small formatters", () => {
  it("joins a role list the way a sentence does", () => {
    expect(listOr([])).toBe("");
    expect(listOr(["A"])).toBe("A");
    expect(listOr(["A", "B"])).toBe("A or B");
    expect(listOr(["A", "B", "C"])).toBe("A, B or C");
  });

  it("writes an instant in UTC, fixed width, whatever the locale", () => {
    expect(stamp("2026-09-19T08:30:12Z")).toBe("2026-09-19 08:30 UTC");
    expect(stamp(null)).toBe("an unknown time");
    expect(stamp(undefined)).toBe("an unknown time");
  });

  it("reads a string parameter, and nothing that is not one", () => {
    const operation = { parameters: { owner_principal: "morgan", selection_truncated: true, n: 3 } } as unknown as BulkStewardshipOperationRead;

    expect(stringParameter(operation, "owner_principal")).toBe("morgan");
    expect(stringParameter(operation, "selection_truncated")).toBeNull();
    expect(stringParameter(operation, "n")).toBeNull();
    expect(stringParameter(operation, "missing")).toBeNull();
  });
});

describe("HintedField", () => {
  it("names the control by its label alone and describes it with the hint, so the name does not change as the hint does", () => {
    const view = render(
      <HintedField label="Rule key" hint="Unique in this organization.">
        {(describedBy) => <input aria-describedby={describedBy} />}
      </HintedField>,
    );

    const input = screen.getByRole("textbox", { name: "Rule key" });
    expect(input).toHaveAccessibleDescription("Unique in this organization.");

    view.rerender(
      <HintedField label="Rule key" hint="Use lowercase letters.">
        {(describedBy) => <input aria-describedby={describedBy} />}
      </HintedField>,
    );
    expect(screen.getByRole("textbox", { name: "Rule key" })).toHaveAccessibleDescription("Use lowercase letters.");
  });
});

describe("OwnershipConfirm", () => {
  it("states each fact as its own line, and confirms and cancels through its buttons only", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <OwnershipConfirm
        title="Apply it?"
        summary="This asks for a review."
        facts={[<>First fact.</>, <>Second fact.</>]}
        confirmLabel="Request review"
        busy={false}
        error={null}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "Apply it?" });
    expect(dialog).toHaveAccessibleDescription("This asks for a review.");
    expect(within(dialog).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["First fact.", "Second fact."]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Request review" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("is busy and unable to be cancelled while a request is in flight, and shows a refusal", () => {
    render(
      <OwnershipConfirm
        title="Apply it?"
        summary="s"
        facts={[]}
        confirmLabel="Request review"
        busy
        error="ownership rule matched no active tables"
        onConfirm={() => undefined}
        onCancel={() => undefined}
      />,
    );

    expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("ownership rule matched no active tables");
  });

  it("does not close on a click outside it", () => {
    const onCancel = vi.fn();
    render(
      <OwnershipConfirm title="t" summary="s" facts={[]} confirmLabel="Go" busy={false} error={null} onConfirm={() => undefined} onCancel={onCancel} />,
    );

    fireEvent.mouseDown(screen.getByRole("dialog").parentElement!);

    expect(onCancel).not.toHaveBeenCalled();
  });
});

describe("RequestedReview", () => {
  const operation = (overrides: Partial<BulkStewardshipOperationRead> = {}) =>
    ({
      id: "op-1", subject_ids: ["a"], status: "REVIEW_REQUIRED", applied_count: 0, governance_review_id: "review-1", parameters: {},
      ...overrides,
    }) as unknown as BulkStewardshipOperationRead;

  it("says one subject in the singular", () => {
    render(<RequestedReview operation={operation()} noun="tables" headline="Review requested" onDismiss={() => undefined} />);

    expect(screen.getByRole("status", { name: "Review requested" })).toHaveTextContent("1 table in this request.");
  });

  it("names what the request leaves out only when the caller can say", () => {
    const view = render(
      <RequestedReview operation={operation()} noun="ownerships" headline="h" onDismiss={() => undefined} />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("1 ownership in this request.");
    expect(screen.getByRole("status")).not.toHaveTextContent("stay with");

    view.rerender(
      <RequestedReview operation={operation()} noun="ownerships" headline="h" remainder="2 stay with priya." onDismiss={() => undefined} />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("2 stay with priya.");
  });

  it.each(["DataSteward", "PlatformAdmin", "Reviewer", "SemanticAdmin"])(
    "offers %s, whom the Review queue admits, the review the request opened",
    (role) => {
      sessionRoles = [role];
      navigateTo.mockReset();
      render(<RequestedReview operation={operation()} noun="tables" headline="h" onDismiss={() => undefined} />);

      fireEvent.click(screen.getByRole("button", { name: "Open this review" }));

      expect(navigateTo).toHaveBeenCalledWith("governance", { review: "review-1" });
    },
  );

  it.each(["MetadataAdmin", "Viewer", "Auditor"])(
    "does not send %s to a Review queue that would refuse them, and still says what happens next",
    (role) => {
      sessionRoles = [role];
      render(<RequestedReview operation={operation()} noun="tables" headline="h" onDismiss={() => undefined} />);

      expect(screen.queryByRole("button", { name: "Open this review" })).not.toBeInTheDocument();
      expect(screen.getByRole("status")).toHaveTextContent("Nothing changes until a different reviewer approves it");
      expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    },
  );

  it("offers no review link on a guess while identity is in flight", () => {
    sessionRoles = undefined;
    render(<RequestedReview operation={operation()} noun="tables" headline="h" onDismiss={() => undefined} />);

    expect(screen.queryByRole("button", { name: "Open this review" })).not.toBeInTheDocument();
  });
});
