import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { CompliancePackRead, MeRead } from "../lib/types";
import { ApiError } from "../lib/api";
import type { Session } from "../lib/session";

/* ---------------------------------------------------------------------------
   Compliance packs against the real, already-merged `compliance_api.py`
   (Phase E, EE.4/OB-5) -- not a stub. Mocks the API boundary the same way
   every other screen test in this app does (`StudioChangeSetsScreen.test.tsx`).
--------------------------------------------------------------------------- */

const fetchCompliancePacks = vi.fn();
const generateCompliancePack = vi.fn();
const downloadCompliancePack = vi.fn();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchCompliancePacks: (query: unknown, signal?: AbortSignal) => fetchCompliancePacks(query, signal),
    generateCompliancePack: (body: unknown, signal?: AbortSignal) => generateCompliancePack(body, signal),
    downloadCompliancePack: (packId: string, signal?: AbortSignal) => downloadCompliancePack(packId, signal),
  };
});

/* Who is offered Generate and Download is decided by the session's roles. `null` is
   "`/v1/me` has not answered" -- what every test before the role block runs as, and
   the controls are offered then (the server's 403 stays the authority). */
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

const asRoles = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});

const PACK: CompliancePackRead = {
  id: "pack_1", organization_id: "org1", name: "BCBS 239 Q2 2026", framework: "BCBS_239",
  period_start: "2026-04-01T00:00:00Z", period_end: "2026-06-30T23:59:59Z",
  sections: [{ title: "Lineage completeness", finding_count: 0 }],
  status: "COMPLETE", checksum: "sha256:abc123",
  generated_by: "compliance-officer@tenant.example", generated_at: "2026-09-01T00:00:00Z",
  created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
};

async function loadScreen() {
  const { ComplianceScreen } = await import("./ComplianceScreen");
  return ComplianceScreen;
}

beforeEach(() => {
  fetchCompliancePacks.mockReset();
  generateCompliancePack.mockReset();
  downloadCompliancePack.mockReset();
  fetchCompliancePacks.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
  vi.resetModules();
  history.replaceState(null, "", "/");
  sessionMe = null;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ComplianceScreen against the real compliance_api.py", () => {
  it("lists compliance packs from the real endpoint", async () => {
    fetchCompliancePacks.mockResolvedValue({ items: [PACK], limit: 100, offset: 0, total: 1 });
    const ComplianceScreen = await loadScreen();

    render(<ComplianceScreen />);

    await waitFor(() => expect(screen.getByText("BCBS 239 Q2 2026")).toBeInTheDocument());
    expect(fetchCompliancePacks).toHaveBeenCalledWith({ limit: 100, offset: 0 }, expect.anything());
  });

  it("shows an empty state when there are no packs yet", async () => {
    const ComplianceScreen = await loadScreen();
    render(<ComplianceScreen />);
    expect(await screen.findByText("No compliance packs yet")).toBeInTheDocument();
  });

  it("downloads and renders a pack's real evidence body on demand", async () => {
    fetchCompliancePacks.mockResolvedValue({ items: [PACK], limit: 100, offset: 0, total: 1 });
    downloadCompliancePack.mockResolvedValue({
      id: "pack_1", name: PACK.name, framework: "BCBS_239", checksum: "sha256:abc123", status: "COMPLETE",
    });
    const ComplianceScreen = await loadScreen();
    render(<ComplianceScreen />);
    await waitFor(() => expect(screen.getByText("BCBS 239 Q2 2026")).toBeInTheDocument());

    screen.getByRole("button", { name: "Download evidence" }).click();

    await waitFor(() => expect(downloadCompliancePack).toHaveBeenCalledWith("pack_1", undefined));
    expect(await screen.findByText(/"checksum": "sha256:abc123"/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide evidence" })).toBeInTheDocument();
  });

  it("surfaces a Viewer's real 403 on download as a row-scoped error, not a crash", async () => {
    fetchCompliancePacks.mockResolvedValue({ items: [PACK], limit: 100, offset: 0, total: 1 });
    downloadCompliancePack.mockRejectedValue(new ApiError(403, "insufficient role for this action"));
    const ComplianceScreen = await loadScreen();
    render(<ComplianceScreen />);
    await waitFor(() => expect(screen.getByText("BCBS 239 Q2 2026")).toBeInTheDocument());

    screen.getByRole("button", { name: "Download evidence" }).click();

    expect(await screen.findByText("insufficient role for this action")).toBeInTheDocument();
  });

  it("generates a pack through the real endpoint and refetches the list", async () => {
    fetchCompliancePacks.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
    generateCompliancePack.mockResolvedValue({ ...PACK, id: "pack_2" });
    const ComplianceScreen = await loadScreen();
    render(<ComplianceScreen />);
    await screen.findByText("No compliance packs yet");

    screen.getByRole("button", { name: "Generate pack" }).click();

    await waitFor(() => expect(generateCompliancePack).toHaveBeenCalledTimes(1));
    const call = generateCompliancePack.mock.calls[0]![0];
    expect(call.framework).toBe("MODEL_RISK");
    expect(typeof call.period_start).toBe("string");
    expect(typeof call.period_end).toBe("string");
    expect(call.name).toBeNull();
    await waitFor(() => expect(fetchCompliancePacks).toHaveBeenCalledTimes(2));
    // R11-D3: the pack is persisted as a checksummed row, not written to WORM
    // storage, so the confirmation says stored rather than archived.
    expect(await screen.findByText("Compliance pack generated and stored.")).toBeInTheDocument();
  });

  it("shows the real 422 (period_end not after period_start) without changing pack state", async () => {
    fetchCompliancePacks.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
    generateCompliancePack.mockRejectedValue(new ApiError(422, "period_end must be after period_start"));
    const ComplianceScreen = await loadScreen();
    render(<ComplianceScreen />);
    await screen.findByText("No compliance packs yet");

    screen.getByRole("button", { name: "Generate pack" }).click();

    expect(await screen.findByText("period_end must be after period_start")).toBeInTheDocument();
    expect(fetchCompliancePacks).toHaveBeenCalledTimes(1);
  });
});

describe("ComplianceScreen: who is offered Generate and Download", () => {
  it("offers an Auditor the evidence to read, and tells them why there is no Generate (R11-AUD01)", async () => {
    // The Auditor's job is reading evidence: the download route admits them, generating a pack
    // (which writes a record) still does not.
    sessionMe = asRoles("Auditor", "Viewer");
    fetchCompliancePacks.mockResolvedValue({ items: [PACK], limit: 100, offset: 0, total: 1 });
    const ComplianceScreen = await loadScreen();

    render(<ComplianceScreen />);

    await waitFor(() => expect(screen.getByText("BCBS 239 Q2 2026")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Generate pack" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download evidence" })).toBeInTheDocument();
    expect(screen.getByText(/Generating a pack needs the DataSteward or PlatformAdmin role/)).toBeInTheDocument();
    expect(screen.getByText(/list the packs below and download their evidence/)).toBeInTheDocument();
    expect(screen.queryByText("Not available to your roles")).not.toBeInTheDocument();
    expect(generateCompliancePack).not.toHaveBeenCalled();
  });

  it("tells a Viewer, who may only list packs, instead of offering controls that answer 403", async () => {
    sessionMe = asRoles("Viewer");
    fetchCompliancePacks.mockResolvedValue({ items: [PACK], limit: 100, offset: 0, total: 1 });
    const ComplianceScreen = await loadScreen();

    render(<ComplianceScreen />);

    await waitFor(() => expect(screen.getByText("BCBS 239 Q2 2026")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Generate pack" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download evidence" })).not.toBeInTheDocument();
    expect(screen.getByText(/Generating a pack needs the DataSteward or PlatformAdmin role/)).toBeInTheDocument();
    expect(screen.getByText("Not available to your roles")).toBeInTheDocument();
    expect(generateCompliancePack).not.toHaveBeenCalled();
    expect(downloadCompliancePack).not.toHaveBeenCalled();
  });

  it.each(["DataSteward", "PlatformAdmin"])("offers both to %s", async (role) => {
    sessionMe = asRoles(role);
    fetchCompliancePacks.mockResolvedValue({ items: [PACK], limit: 100, offset: 0, total: 1 });
    const ComplianceScreen = await loadScreen();

    render(<ComplianceScreen />);

    await waitFor(() => expect(screen.getByText("BCBS 239 Q2 2026")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Generate pack" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download evidence" })).toBeInTheDocument();
    expect(screen.queryByText("Not available to your roles")).not.toBeInTheDocument();
  });
});
