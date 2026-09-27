import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const { fetchOrganizations, fetchMe } = vi.hoisted(() => ({
  fetchOrganizations: vi.fn(),
  fetchMe: vi.fn(),
}));

vi.mock("./api", () => ({ fetchOrganizations, fetchMe }));

import { DEFAULT_ORG_ID, OrgProvider, useOrgSelection, type OrgSelection } from "./org";
import { setCurrentOrgId } from "./org-context";

/* ---------------------------------------------------------------------------
   Which organization the shell opens in when the remembered one cannot be used.

   THE DEFECT. Only Auditor, Operations, OrganizationAdmin and PlatformAdmin may list organizations
   (`GET /v1/organizations`). A Steward, Analyst, Reviewer or DataAdmin therefore had no list to
   choose from, the remembered id was the placeholder `DEFAULT_ORG_ID`, and every screen asked a
   tenant that exists nowhere. Under OIDC the token already names the caller's organization
   (`GET /v1/me` -> `organization_id`); the shell never used it.

   The rule these tests hold: the remembered id wins when it is selectable; otherwise the caller's
   own organization wins if it is in the list or the list is empty; otherwise the first listed one.
   The one case that must NOT change is an administrator under the development identity, where
   `/v1/me` only echoes the placeholder header this client sent.
--------------------------------------------------------------------------- */

const OWN = "9b90b35f-dcf5-49d3-8f0e-2f269987ae87";
const OTHER = "5ee85f6d-9c27-4d87-93ad-dbe22acac062";
const org = (id: string, name: string) => ({ id, name, slug: name.toLowerCase(), status: "ACTIVE" }) as never;

let captured: OrgSelection | null = null;

function Probe() {
  const selection = useOrgSelection();
  captured = selection;
  return <span data-testid="org">{selection?.orgId}</span>;
}

async function mountAndSettle() {
  captured = null;
  render(
    <OrgProvider>
      <Probe />
    </OrgProvider>,
  );
  await waitFor(() => expect(captured?.loading).toBe(false));
}

beforeEach(() => {
  fetchOrganizations.mockReset();
  fetchMe.mockReset();
  localStorage.clear();
  setCurrentOrgId(DEFAULT_ORG_ID);
});

describe("organization resolution", () => {
  it("adopts the caller's own organization when the caller may not list organizations", async () => {
    fetchOrganizations.mockRejectedValue(new Error("one of these roles is required: Auditor, Operations"));
    fetchMe.mockResolvedValue({ organization_id: OWN });
    await mountAndSettle();

    expect(screen.getByTestId("org").textContent).toBe(OWN);
    expect(localStorage.getItem("atlas.org.id")).toBe(OWN);
    // The refusal is expected for a non-administrator, so it is not shown as a failure.
    expect(captured?.error).toBeNull();
  });

  it("keeps a remembered organization that is in the list, without asking who the caller is", async () => {
    setCurrentOrgId(OTHER);
    fetchOrganizations.mockResolvedValue([org(OWN, "Northwind"), org(OTHER, "Local Bank")]);
    await mountAndSettle();

    expect(screen.getByTestId("org").textContent).toBe(OTHER);
    expect(fetchMe).not.toHaveBeenCalled();
  });

  it("prefers the caller's own organization when it is in the list", async () => {
    fetchOrganizations.mockResolvedValue([org(OTHER, "Local Bank"), org(OWN, "Northwind")]);
    fetchMe.mockResolvedValue({ organization_id: OWN });
    await mountAndSettle();

    expect(screen.getByTestId("org").textContent).toBe(OWN);
  });

  it("does not let an echoed placeholder outrank the first real organization (administrator, development identity)", async () => {
    // `/v1/me` under the development identity returns the X-Organization-Id header the client sent,
    // which is the placeholder. It is not in the list, so the first real organization is chosen.
    fetchOrganizations.mockResolvedValue([org(OTHER, "Local Bank"), org(OWN, "Northwind")]);
    fetchMe.mockResolvedValue({ organization_id: DEFAULT_ORG_ID });
    await mountAndSettle();

    expect(screen.getByTestId("org").textContent).toBe(OTHER);
    expect(captured?.error).toBeNull();
  });

  it("reports the refusal and keeps the remembered id when the identity names no organization", async () => {
    fetchOrganizations.mockRejectedValue(new Error("one of these roles is required: Auditor"));
    fetchMe.mockResolvedValue({ organization_id: null });
    await mountAndSettle();

    expect(screen.getByTestId("org").textContent).toBe(DEFAULT_ORG_ID);
    expect(captured?.error).toContain("one of these roles is required");
  });

  it("survives a failing identity call as well", async () => {
    fetchOrganizations.mockRejectedValue(new Error("refused"));
    fetchMe.mockRejectedValue(new Error("down"));
    await mountAndSettle();

    expect(screen.getByTestId("org").textContent).toBe(DEFAULT_ORG_ID);
    expect(captured?.error).toBe("refused");
  });
});
