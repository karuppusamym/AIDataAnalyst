import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { CrossBoundaryGrantRead, DataDomainRead } from "../lib/types";

const fetchGrants = vi.fn();
const requestGrant = vi.fn();

vi.mock("../lib/_cross_source_api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../lib/_cross_source_api");
  return {
    ...actual,
    fetchCrossBoundaryGrants: (...args: unknown[]) => fetchGrants(...args),
    requestCrossBoundaryGrant: (...args: unknown[]) => requestGrant(...args),
  };
});

import { CrossBoundaryGrants } from "./CrossBoundaryGrants";

beforeEach(() => {
  fetchGrants.mockReset();
  requestGrant.mockReset();
});

const DOMAINS: DataDomainRead[] = [
  {
    id: "dom_fin",
    organization_id: "o1",
    line_of_business_id: "lob_fin",
    parent_domain_id: null,
    name: "Finance",
    code: "FIN",
    is_default: false,
    status: "ACTIVE",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: "dom_retail",
    organization_id: "o1",
    line_of_business_id: "lob_retail",
    parent_domain_id: null,
    name: "Retail",
    code: "RET",
    is_default: false,
    status: "ACTIVE",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

function grant(overrides: Partial<CrossBoundaryGrantRead> = {}): CrossBoundaryGrantRead {
  return {
    id: "g1",
    organization_id: "o1",
    source_data_domain_id: "dom_retail",
    target_data_domain_id: "dom_fin",
    edge_kinds: ["SUGGESTED_RELATIONSHIP"],
    reason: "Party resolution against the retail customer master.",
    status: "ACTIVE",
    requested_by: "steward@example.com",
    approved_by: "checker@example.com",
    approved_at: "2026-09-02T00:00:00Z",
    expires_at: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
    ...overrides,
  };
}

it("states the direction of a grant in words, not just two domain names", async () => {
  // "Finance may see into Retail" and the reverse are different grants with
  // different owners; a list of two names would leave a steward guessing.
  fetchGrants.mockResolvedValue([grant()]);

  render(<CrossBoundaryGrants domainId="dom_fin" domains={DOMAINS} />);

  await waitFor(() => expect(screen.getByText(/may see into/)).toBeInTheDocument());
  const flow = screen.getByText(/may see into/).parentElement;
  expect(flow?.textContent).toContain("Finance");
  expect(flow?.textContent).toContain("Retail");
  expect(screen.getByText(/your domain is looking in/)).toBeInTheDocument();
});

it("says which side of the boundary the viewed domain is on", async () => {
  fetchGrants.mockResolvedValue([
    grant({ source_data_domain_id: "dom_fin", target_data_domain_id: "dom_retail" }),
  ]);

  render(<CrossBoundaryGrants domainId="dom_fin" domains={DOMAINS} />);

  await waitFor(() =>
    expect(screen.getByText(/your domain is being looked into/)).toBeInTheDocument(),
  );
});

it("offers no approve control — a grant is decided on the review queue", async () => {
  // Approving here would either duplicate the review surface or bypass
  // maker-checker. Neither is acceptable, so the component only requests.
  fetchGrants.mockResolvedValue([grant({ status: "PENDING_APPROVAL", approved_by: null })]);

  render(<CrossBoundaryGrants domainId="dom_fin" domains={DOMAINS} />);

  await waitFor(() => expect(screen.getByText("pending_approval")).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
  expect(screen.getByText(/waiting for a decision on the Review queue/)).toBeInTheDocument();
});

it("requests a grant on the domain that owns the data, naming this one as the asker", async () => {
  fetchGrants.mockResolvedValue([]);
  requestGrant.mockResolvedValue(grant({ status: "PENDING_APPROVAL" }));

  render(<CrossBoundaryGrants domainId="dom_fin" domains={DOMAINS} />);
  await waitFor(() => expect(screen.getByText(/No grants either way/)).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", { name: "Request access" }));
  fireEvent.change(screen.getByLabelText("See into which domain"), {
    target: { value: "dom_retail" },
  });
  fireEvent.change(screen.getByLabelText("Why"), { target: { value: "party resolution" } });
  fireEvent.click(screen.getByRole("button", { name: "Request" }));

  await waitFor(() =>
    expect(requestGrant).toHaveBeenCalledWith("dom_retail", {
      target_data_domain_id: "dom_fin",
      reason: "party resolution",
    }),
  );
  expect(screen.getByText(/someone other than you has to approve it/)).toBeInTheDocument();
});

it("does not offer the viewed domain as a target for its own grant", async () => {
  fetchGrants.mockResolvedValue([]);

  render(<CrossBoundaryGrants domainId="dom_fin" domains={DOMAINS} />);
  await waitFor(() => expect(screen.getByText(/No grants either way/)).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Request access" }));

  const options = Array.from(
    (screen.getByLabelText("See into which domain") as HTMLSelectElement).options,
  ).map((o) => o.value);
  expect(options).not.toContain("dom_fin");
  expect(options).toContain("dom_retail");
});

it("opens pre-filled when the caller names a withheld domain", async () => {
  // The graph reports which domain it withheld; the fix should be one click
  // from the problem, not a form the steward has to re-derive.
  fetchGrants.mockResolvedValue([]);

  render(
    <CrossBoundaryGrants
      domainId="dom_fin"
      domains={DOMAINS}
      suggestedSourceDomainId="dom_retail"
    />,
  );

  await waitFor(() =>
    expect((screen.getByLabelText("See into which domain") as HTMLSelectElement).value).toBe(
      "dom_retail",
    ),
  );
});

it("says plainly when nothing can be seen across the boundary either way", async () => {
  fetchGrants.mockResolvedValue([]);

  render(<CrossBoundaryGrants domainId="dom_fin" domains={DOMAINS} />);

  await waitFor(() =>
    expect(
      screen.getByText(/Nothing outside this domain can be seen from it/),
    ).toBeInTheDocument(),
  );
});
