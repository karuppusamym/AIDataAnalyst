import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import { DelegationsScreen } from "./DelegationsScreen";
import { makeFixtureDelegations } from "../lib/fixtures";
import { ApiError } from "../lib/api";
import type { DelegationRead } from "../lib/types";

const fetchDelegations = vi.fn();
const grantDelegation = vi.fn();
const revokeDelegation = vi.fn();

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    fetchDelegations: (...args: unknown[]) => fetchDelegations(...args),
    grantDelegation: (...args: unknown[]) => grantDelegation(...args),
    revokeDelegation: (...args: unknown[]) => revokeDelegation(...args),
  };
});

const ORG = "00000000-0000-0000-0000-000000000001";

describe("DelegationsScreen (PG-4)", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    history.replaceState(null, "", "/");
    fetchDelegations.mockResolvedValue(await makeFixtureDelegations(ORG));
  });

  it("renders each row's computed status, distinguishing expired from genuinely active", async () => {
    render(<DelegationsScreen />);

    // Fixture: two ACTIVE-and-current, one ACTIVE-but-past-expiry, two REVOKED.
    await waitFor(() => expect(screen.getAllByText("active").length).toBe(2));
    expect(screen.getByText("expired")).toBeInTheDocument();
    expect(screen.getAllByText("revoked").length).toBe(2);
  });

  it("shows delegator, delegate and roles for a row", async () => {
    render(<DelegationsScreen />);
    await waitFor(() => expect(screen.getAllByText("priya.steward").length).toBeGreaterThan(0));
    expect(screen.getAllByText("morgan.covering").length).toBeGreaterThan(0);
    // "SemanticAdmin" also names a checkbox in the (currently closed) grant
    // form below, so this only asserts the role pill exists, not uniqueness.
    expect(screen.getAllByText("SemanticAdmin").length).toBeGreaterThan(0);
  });

  it("offers Revoke only for rows that are genuinely active, not expired or revoked", async () => {
    render(<DelegationsScreen />);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Revoke" }).length).toBe(2));
  });

  it("renders an error state rather than a blank screen", async () => {
    fetchDelegations.mockRejectedValue(new Error("boom"));
    render(<DelegationsScreen />);
    await waitFor(() =>
      expect(screen.getByText(/delegations could not be loaded/i)).toBeInTheDocument(),
    );
  });

  it("submits the grant form with the roles checked and an omitted starts_at", async () => {
    const now = new Date().toISOString();
    const granted: DelegationRead = {
      id: "delegation-new",
      organization_id: ORG,
      delegator_principal_id: "local-ui-admin",
      delegate_principal_id: "new.covering",
      delegated_roles: ["DataSteward", "Reviewer"],
      reason: "Covering while I am on leave next sprint.",
      starts_at: now,
      expires_at: now,
      status: "ACTIVE",
      created_by: "local-ui-admin",
      revoked_by: null,
      revoked_at: null,
      created_at: now,
      updated_at: now,
    };
    grantDelegation.mockResolvedValue(granted);
    render(<DelegationsScreen />);
    await waitFor(() => expect(fetchDelegations).toHaveBeenCalled());

    fireEvent.click(screen.getByText("Grant delegation"));

    fireEvent.change(screen.getByLabelText("Delegate to (principal id)"), {
      target: { value: "new.covering" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "DataSteward" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Reviewer" }));
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "Covering while I am on leave next sprint." },
    });
    const expiresAtValue = "2026-12-01T10:00";
    fireEvent.change(screen.getByLabelText("Expires at"), { target: { value: expiresAtValue } });

    fireEvent.click(screen.getByRole("button", { name: "Grant" }));

    await waitFor(() => expect(grantDelegation).toHaveBeenCalledTimes(1));
    expect(grantDelegation).toHaveBeenCalledWith(ORG, {
      delegate_principal_id: "new.covering",
      delegated_roles: ["DataSteward", "Reviewer"],
      reason: "Covering while I am on leave next sprint.",
      starts_at: null,
      expires_at: new Date(expiresAtValue).toISOString(),
    });
  });

  it("surfaces a validation error from the API without crashing the form", async () => {
    grantDelegation.mockRejectedValue(new ApiError(422, "cannot delegate authority to yourself"));
    render(<DelegationsScreen />);
    await waitFor(() => expect(fetchDelegations).toHaveBeenCalled());

    fireEvent.click(screen.getByText("Grant delegation"));
    fireEvent.change(screen.getByLabelText("Delegate to (principal id)"), {
      target: { value: "local-ui-admin" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Reviewer" }));
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "Attempting a self-delegation on purpose." },
    });
    fireEvent.change(screen.getByLabelText("Expires at"), {
      target: { value: "2026-12-01T10:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Grant" }));

    await waitFor(() =>
      expect(screen.getByText("cannot delegate authority to yourself")).toBeInTheDocument(),
    );
  });

  it("asks for confirmation before revoking, and does nothing if the user cancels", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<DelegationsScreen />);

    await waitFor(() => expect(screen.getAllByRole("button", { name: "Revoke" }).length).toBe(2));
    fireEvent.click(screen.getAllByRole("button", { name: "Revoke" })[0]!);

    expect(confirmSpy).toHaveBeenCalledWith("Revoke this delegation?");
    expect(revokeDelegation).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("revokes the delegation and updates that row in place once confirmed", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const page = await makeFixtureDelegations(ORG);
    const target = page.items.find((d) => d.delegator_principal_id === "priya.steward" && d.status === "ACTIVE")!;
    const revoked: DelegationRead = {
      ...target,
      status: "REVOKED",
      revoked_by: "local-ui-admin",
      revoked_at: new Date().toISOString(),
    };
    revokeDelegation.mockResolvedValue(revoked);
    render(<DelegationsScreen />);

    await waitFor(() => expect(screen.getAllByRole("button", { name: "Revoke" }).length).toBe(2));
    fireEvent.click(screen.getAllByRole("button", { name: "Revoke" })[0]!);

    await waitFor(() => expect(revokeDelegation).toHaveBeenCalledWith(target.id));
    await waitFor(() => expect(screen.getByText(/delegation revoked/i)).toBeInTheDocument());
    confirmSpy.mockRestore();
  });

  it("filters to only the client-computed expired row when 'Expired' is chosen", async () => {
    render(<DelegationsScreen />);
    await waitFor(() => expect(screen.getAllByRole("listitem").length).toBeGreaterThan(0));

    fireEvent.change(screen.getByLabelText("Delegation status filter"), {
      target: { value: "EXPIRED" },
    });

    await waitFor(() => expect(fetchDelegations).toHaveBeenCalledTimes(2));
    expect(fetchDelegations.mock.calls[1]![1]).toMatchObject({ status: "ACTIVE" });
    await waitFor(() => expect(screen.getAllByRole("listitem").length).toBe(1));
    expect(screen.getByText("expired")).toBeInTheDocument();
  });

  it("shows an empty state when no delegation matches", async () => {
    fetchDelegations.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
    render(<DelegationsScreen />);
    await waitFor(() =>
      expect(screen.getByText(/no delegation matches these filters/i)).toBeInTheDocument(),
    );
  });
});
