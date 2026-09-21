import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ObjectKnowledgeRead } from "../lib/api/knowledge";
import type { Session } from "../lib/session";
import type { MeRead } from "../lib/types";

/* ---------------------------------------------------------------------------
   Who is offered the Catalog evidence pane's Knowledge section.

   `GET /v1/metadata/tables/{table_id}/okf-knowledge` admits six roles. For a
   session known to hold none of them -- an Auditor, Reviewer, Viewer or
   DataAdmin, all demo users -- the section could only ever answer "You are not
   permitted to read this bundle", so it is not offered. Unknown identity
   (`/v1/me` not yet answered) still offers it; the server's 403 is the authority.
--------------------------------------------------------------------------- */

const fetchObjectKnowledge =
  vi.fn<(tableId: string, signal?: AbortSignal) => Promise<ObjectKnowledgeRead>>();

vi.mock("../lib/api/knowledge", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/knowledge")>();
  return {
    ...actual,
    fetchObjectKnowledge: (tableId: string, signal?: AbortSignal) => fetchObjectKnowledge(tableId, signal),
  };
});

let sessionMe: MeRead | null = null;
vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  return {
    ...actual,
    useSession: (): Session => ({
      state: "demo",
      me: sessionMe,
      lapsed: false,
      lastSuccessAt: null,
      error: null,
      dataMode: "fixtures",
      authMode: "development",
      authModeInferred: false,
      reload: () => undefined,
    }),
  };
});

const { ObjectKnowledge } = await import("./ObjectKnowledge");

const asRoles = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});

const toggle = () => screen.queryByRole("button", { name: /Knowledge/ });

beforeEach(() => {
  fetchObjectKnowledge.mockReset();
  fetchObjectKnowledge.mockResolvedValue({ table_id: "t-1", items: [] });
  sessionMe = null;
});

describe("ObjectKnowledge: who is offered the section", () => {
  it.each([["Auditor", "Viewer"], ["Reviewer"], ["Viewer"], ["DataAdmin"]])(
    "is not offered to %s, and asks nothing",
    async (...roles) => {
      sessionMe = asRoles(...roles);
      const { container } = render(<ObjectKnowledge tableId="t-1" />);

      expect(toggle()).not.toBeInTheDocument();
      expect(container).toBeEmptyDOMElement();
      expect(fetchObjectKnowledge).not.toHaveBeenCalled();
    },
  );

  it.each(["AgentDeveloper", "Analyst", "DataProductOwner", "DataSteward", "MetadataAdmin", "PlatformAdmin"])(
    "is offered to %s and reads on open",
    async (role) => {
      sessionMe = asRoles(role);
      render(<ObjectKnowledge tableId="t-1" />);

      await userEvent.click(toggle()!);

      expect(fetchObjectKnowledge).toHaveBeenCalledWith("t-1", expect.anything());
    },
  );

  it("is offered while identity is still unknown", () => {
    sessionMe = null;
    render(<ObjectKnowledge tableId="t-1" />);

    expect(toggle()).toBeInTheDocument();
  });
});
