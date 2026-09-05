import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { CompliancePackRead } from "../lib/types";
import { ApiError } from "../lib/api";

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
    expect(await screen.findByText("Compliance pack generated and archived.")).toBeInTheDocument();
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
