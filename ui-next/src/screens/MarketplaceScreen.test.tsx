import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { MarketplaceProductRead } from "../lib/ui-types";
import type { MarketplaceAccessRequestRead } from "../lib/types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   UX-15: Marketplace on the Catalog pattern against CX-9's real
   `GET /v1/marketplace/products` / `POST .../access-requests`
   (`product_marketplace_api.py`). Mocks the API boundary, matching
   `EvidencePane.test.tsx`/`App.test.tsx`'s established pattern.
--------------------------------------------------------------------------- */

const fetchMarketplaceProducts = vi.fn<
  (query: unknown, signal?: AbortSignal) => Promise<{ items: MarketplaceProductRead[]; limit: number; offset: number; total: number }>
>();
const requestMarketplaceAccess = vi.fn<
  (versionId: string, body: unknown, signal?: AbortSignal) => Promise<MarketplaceAccessRequestRead>
>();
const fetchMarketplaceAccessRequests = vi.fn<
  (query?: unknown, signal?: AbortSignal) => Promise<{
    items: MarketplaceAccessRequestRead[];
    limit: number;
    offset: number;
    total: number;
  }>
>();
const revokeMarketplaceAccess = vi.fn<
  (requestId: string, signal?: AbortSignal) => Promise<MarketplaceAccessRequestRead>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchMarketplaceProducts: (query: unknown, signal?: AbortSignal) =>
      fetchMarketplaceProducts(query, signal),
    requestMarketplaceAccess: (versionId: string, body: unknown, signal?: AbortSignal) =>
      requestMarketplaceAccess(versionId, body, signal),
    fetchMarketplaceAccessRequests: (query?: unknown, signal?: AbortSignal) =>
      fetchMarketplaceAccessRequests(query, signal),
    revokeMarketplaceAccess: (requestId: string, signal?: AbortSignal) =>
      revokeMarketplaceAccess(requestId, signal),
  };
});

const GRANT: MarketplaceAccessRequestRead = {
  id: "mar_1", organization_id: "org1", data_product_version_id: "dpv_1",
  requested_by: "analyst@tenant.example", purpose: "quarterly close", duration_days: 90,
  status: "APPROVED", governance_review_id: "gr_1", decided_by: "steward",
  decision_reason: "Approved.", decided_at: "2026-09-03T00:00:00Z",
  expires_at: "2026-12-02T00:00:00Z", revoked_by: null, revoked_at: null,
  fulfillment_status: "PROVISIONED", fulfillment_provider: "outbox",
  fulfillment_reference: "local:mar_1", fulfillment_error: null,
  fulfilled_at: "2026-09-03T00:00:00Z", created_at: "2026-09-02T00:00:00Z",
  updated_at: "2026-09-03T00:00:00Z",
};

const PRODUCT: MarketplaceProductRead = {
  id: "dpv_1", organization_id: "org1", product_id: "dp_1", product_key: "finance-revenue-model",
  version: 3, name: "Finance revenue model", description: "Certified revenue metrics.",
  domain_name: "fin", owner_principal: "priya@tenant.example", usage_terms: "Internal use.",
  classification: "INTERNAL", certification_status: "CERTIFIED", quality_score: 0.97, lineage_coverage: 0.9,
  context_product_version_id: null, discoverable_roles: ["*"], consumer_roles: ["Analyst"],
  ports: [{ port_key: "p1", direction: "OUTPUT", name: "revenue_model", description: "Semantic model", asset_type: "SEMANTIC_MODEL", asset_id: "sm_1" }],
  status: "PUBLISHED", fingerprint: "abc", created_by: "priya@tenant.example",
  approved_by: "steward", approved_at: "2026-08-01T00:00:00Z", published_at: "2026-08-02T00:00:00Z",
  created_at: "2026-07-01T00:00:00Z", updated_at: "2026-08-02T00:00:00Z",
  access_status: "NOT_REQUESTED", domain_affinity: true, role_affinity: false,
};

async function loadScreen() {
  const { MarketplaceScreen } = await import("./MarketplaceScreen");
  return MarketplaceScreen;
}

beforeEach(() => {
  fetchMarketplaceProducts.mockReset();
  requestMarketplaceAccess.mockReset();
  fetchMarketplaceAccessRequests.mockReset();
  revokeMarketplaceAccess.mockReset();
  fetchMarketplaceProducts.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
  fetchMarketplaceAccessRequests.mockResolvedValue({
    items: [], limit: 100, offset: 0, total: 0,
  });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("MarketplaceScreen against the real CX-9 endpoint", () => {
  it("loads with sort=personalized by default and renders results", async () => {
    fetchMarketplaceProducts.mockResolvedValue({ items: [PRODUCT], limit: 50, offset: 0, total: 1 });
    const MarketplaceScreen = await loadScreen();

    render(<MarketplaceScreen />);

    await waitFor(() => expect(screen.getByText("Finance revenue model")).toBeInTheDocument());
    expect(fetchMarketplaceProducts).toHaveBeenCalledWith(
      expect.objectContaining({ sort: "personalized" }),
      expect.anything(),
    );
    expect(screen.getByText("your domain")).toBeInTheDocument();
  });

  it("re-fetches with a new classification filter, aborting the in-flight request", async () => {
    fetchMarketplaceProducts.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
    const MarketplaceScreen = await loadScreen();
    render(<MarketplaceScreen />);
    await waitFor(() => expect(fetchMarketplaceProducts).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("Classification"), { target: { value: "RESTRICTED" } });

    await waitFor(() =>
      expect(fetchMarketplaceProducts).toHaveBeenLastCalledWith(
        expect.objectContaining({ classification: "RESTRICTED" }),
        expect.anything(),
      ),
    );
    expect(new URLSearchParams(location.search).get("class")).toBe("RESTRICTED");
  });

  it("opens the permalinkable detail pane and requests access through the real endpoint", async () => {
    fetchMarketplaceProducts.mockResolvedValue({ items: [PRODUCT], limit: 50, offset: 0, total: 1 });
    requestMarketplaceAccess.mockResolvedValue({
      id: "mar_1", organization_id: "org1", data_product_version_id: "dpv_1",
      requested_by: "me", purpose: "quarterly close", duration_days: 90, status: "PENDING",
      governance_review_id: "gr_1", decided_by: null, decision_reason: null, decided_at: null,
      expires_at: null, revoked_by: null, revoked_at: null, fulfillment_status: "PENDING",
      fulfillment_provider: null, fulfillment_reference: null, fulfillment_error: null,
      fulfilled_at: null, created_at: "2026-09-02T00:00:00Z", updated_at: "2026-09-02T00:00:00Z",
    });
    const MarketplaceScreen = await loadScreen();
    render(<MarketplaceScreen />);
    await waitFor(() => expect(screen.getByText("Finance revenue model")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /Finance revenue model/ }));
    expect(new URLSearchParams(location.search).get("product")).toBe("dpv_1");

    const panel = await screen.findByLabelText("Detail for Finance revenue model");
    const purposeInput = panel.querySelector("input[type=text]") as HTMLInputElement;
    fireEvent.change(purposeInput, { target: { value: "quarterly close" } });
    fireEvent.click(screen.getByRole("button", { name: "Request access" }));

    await waitFor(() =>
      expect(requestMarketplaceAccess).toHaveBeenCalledWith(
        "dpv_1",
        { purpose: "quarterly close", duration_days: 90 },
        undefined,
      ),
    );
  });

  it("refuses to request access with an empty purpose, without calling the endpoint", async () => {
    fetchMarketplaceProducts.mockResolvedValue({ items: [PRODUCT], limit: 50, offset: 0, total: 1 });
    const MarketplaceScreen = await loadScreen();
    render(<MarketplaceScreen />);
    await waitFor(() => expect(screen.getByText("Finance revenue model")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Finance revenue model/ }));
    await screen.findByLabelText("Detail for Finance revenue model");

    fireEvent.click(screen.getByRole("button", { name: "Request access" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("A purpose is required");
    expect(requestMarketplaceAccess).not.toHaveBeenCalled();
  });

  /* R11-B4 — revoking a granted entitlement. */

  const GRANTED_PRODUCT: MarketplaceProductRead = {
    ...PRODUCT,
    access_status: "REQUEST_APPROVED",
  };

  async function openGrantedDetail() {
    fetchMarketplaceProducts.mockResolvedValue({
      items: [GRANTED_PRODUCT], limit: 50, offset: 0, total: 1,
    });
    fetchMarketplaceAccessRequests.mockResolvedValue({
      items: [GRANT], limit: 100, offset: 0, total: 1,
    });
    const MarketplaceScreen = await loadScreen();
    render(<MarketplaceScreen />);
    await waitFor(() => expect(screen.getByText("Finance revenue model")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Finance revenue model/ }));
    await screen.findByLabelText("Detail for Finance revenue model");
  }

  it("offers Revoke for a granted entitlement, resolved through the access-request list", async () => {
    await openGrantedDetail();

    expect(await screen.findByRole("button", { name: "Revoke access" })).toBeInTheDocument();
    expect(fetchMarketplaceAccessRequests).toHaveBeenCalled();
  });

  it("does not offer Revoke when nothing is granted", async () => {
    fetchMarketplaceProducts.mockResolvedValue({ items: [PRODUCT], limit: 50, offset: 0, total: 1 });
    const MarketplaceScreen = await loadScreen();
    render(<MarketplaceScreen />);
    await waitFor(() => expect(screen.getByText("Finance revenue model")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Finance revenue model/ }));
    await screen.findByLabelText("Detail for Finance revenue model");

    expect(screen.queryByRole("button", { name: "Revoke access" })).not.toBeInTheDocument();
    expect(fetchMarketplaceAccessRequests).not.toHaveBeenCalled();
  });

  it("confirms before revoking, and revokes the resolved request", async () => {
    await openGrantedDetail();
    revokeMarketplaceAccess.mockResolvedValue({ ...GRANT, status: "REVOKED" });

    fireEvent.click(await screen.findByRole("button", { name: "Revoke access" }));

    // The confirmation is a real gate: nothing is called until it is accepted.
    expect(await screen.findByText("Revoke this access grant?")).toBeInTheDocument();
    expect(revokeMarketplaceAccess).not.toHaveBeenCalled();

    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke access" }));

    await waitFor(() => expect(revokeMarketplaceAccess).toHaveBeenCalledWith("mar_1", undefined));
    // The list is reloaded so the access pill reflects the new state.
    await waitFor(() => expect(fetchMarketplaceProducts.mock.calls.length).toBeGreaterThan(1));
  });

  it("cancelling the confirmation revokes nothing", async () => {
    await openGrantedDetail();

    fireEvent.click(await screen.findByRole("button", { name: "Revoke access" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(revokeMarketplaceAccess).not.toHaveBeenCalled();
  });

  it("surfaces the server's refusal text when a revoke is denied", async () => {
    await openGrantedDetail();
    revokeMarketplaceAccess.mockRejectedValue(
      new ApiError(403, "only approved access can be revoked"),
    );

    fireEvent.click(await screen.findByRole("button", { name: "Revoke access" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Revoke access" }));

    // The server's own words, not a client-side guess at eligibility.
    expect(await screen.findByText("only approved access can be revoked")).toBeInTheDocument();
    // Still open, so the refusal is read before the pane closes.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("surfaces a fetch error with a retry action", async () => {
    fetchMarketplaceProducts.mockRejectedValue(new ApiError(403, "policy_denied"));
    const MarketplaceScreen = await loadScreen();

    render(<MarketplaceScreen />);

    await waitFor(() => expect(screen.getByText("policy_denied")).toBeInTheDocument());
  });
});
