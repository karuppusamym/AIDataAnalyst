import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { AssetEvidenceRead } from "../lib/types";
import { ApiError } from "../lib/api";
import { EvidencePane } from "./EvidencePane";

/* ---------------------------------------------------------------------------
   UX-7: the evidence pane is a genuine permalink target -- it resolves by
   `tableId` alone, fetched straight from the gated evidence endpoint,
   regardless of whether the catalog grid happens to have that row loaded
   (`row` is optional, cosmetic-only progressive enhancement). And it is
   permission-aware: a 403 from that fetch renders as a real denial, never a
   silent fallback that could be mistaken for "nothing here".
--------------------------------------------------------------------------- */

const fetchAssetEvidence = vi.fn<(tableId: string, signal?: AbortSignal) => Promise<AssetEvidenceRead>>();
vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchAssetEvidence: (tableId: string, signal?: AbortSignal) =>
      fetchAssetEvidence(tableId, signal),
  };
});

const EVIDENCE: AssetEvidenceRead = {
  table_id: "t_deep_link",
  table_name: "risk_exposure_snapshot",
  generated_at: "2026-09-01T00:00:00Z",
  items: [
    { category: "OWNERSHIP", claim: "Owned by Risk Analytics", source: "ownership_assignment" },
  ],
};

describe("EvidencePane as a permalink target", () => {
  it("shows the idle state when no tableId is present in the URL", () => {
    fetchAssetEvidence.mockReset();
    render(<EvidencePane tableId={null} row={null} onClose={() => {}} />);

    expect(screen.getByText("Select an asset")).toBeInTheDocument();
    expect(fetchAssetEvidence).not.toHaveBeenCalled();
  });

  it("resolves evidence by tableId alone, with no matching CatalogRowRead loaded", async () => {
    fetchAssetEvidence.mockReset();
    fetchAssetEvidence.mockResolvedValue(EVIDENCE);

    // `row` is deliberately omitted/null here -- this is the deep-link case:
    // a colleague opened `?asset=t_deep_link` and the catalog grid's current
    // page/filter never loaded that row.
    render(<EvidencePane tableId="t_deep_link" row={null} onClose={() => {}} />);

    expect(fetchAssetEvidence).toHaveBeenCalledWith("t_deep_link", expect.anything());
    await waitFor(() =>
      expect(screen.getByText("Owned by Risk Analytics")).toBeInTheDocument(),
    );
    // The header still names the asset, sourced from the evidence payload
    // itself rather than a preloaded row.
    expect(screen.getByText("risk_exposure_snapshot")).toBeInTheDocument();
    expect(screen.getByText("Opened from a permalink")).toBeInTheDocument();
  });

  it("surfaces a 403 from the gated endpoint as an explicit denial, not a silent empty pane", async () => {
    fetchAssetEvidence.mockReset();
    fetchAssetEvidence.mockRejectedValue(new ApiError(403, "policy_denied"));

    render(<EvidencePane tableId="t_secret" row={null} onClose={() => {}} />);

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/not authorized/i),
    );
    // Not silently rendered as "nothing to show" -- the idle empty state
    // never mounts once a tableId is present.
    expect(screen.queryByText("Select an asset")).not.toBeInTheDocument();
  });

  it("re-fetches when the permalink's tableId changes, even with the same row prop", async () => {
    fetchAssetEvidence.mockReset();
    fetchAssetEvidence.mockResolvedValue(EVIDENCE);

    const { rerender } = render(
      <EvidencePane tableId="t_deep_link" row={null} onClose={() => {}} />,
    );
    await waitFor(() => expect(fetchAssetEvidence).toHaveBeenCalledTimes(1));

    rerender(<EvidencePane tableId="t_other" row={null} onClose={() => {}} />);
    await waitFor(() => expect(fetchAssetEvidence).toHaveBeenCalledTimes(2));
    expect(fetchAssetEvidence).toHaveBeenLastCalledWith("t_other", expect.anything());
  });
});
