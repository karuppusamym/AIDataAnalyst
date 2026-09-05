import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { CrossSourceResolutionCandidateRead } from "../lib/_cross_source_api";

const fetchDomains = vi.fn();
const fetchCandidates = vi.fn();
const discoverRelationships = vi.fn();
const discoverResolutions = vi.fn();
const decide = vi.fn();
const fetchDatasources = vi.fn();

vi.mock("../lib/_cross_source_api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../lib/_cross_source_api");
  return {
    ...actual,
    fetchOrgDataDomains: (...a: unknown[]) => fetchDomains(...a),
    fetchCrossSourceResolutionCandidates: (...a: unknown[]) => fetchCandidates(...a),
    discoverCrossSourceRelationships: (...a: unknown[]) => discoverRelationships(...a),
    discoverCrossSourceObjectResolutions: (...a: unknown[]) => discoverResolutions(...a),
    decideCrossSourceResolutionCandidate: (...a: unknown[]) => decide(...a),
    fetchCrossBoundaryGrants: async () => [],
  };
});

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../lib/api");
  return { ...actual, fetchOrgDatasources: (...a: unknown[]) => fetchDatasources(...a) };
});

vi.mock("../lib/org", () => ({ useOrgId: () => "org-1", getCurrentOrgId: () => "org-1" }));

import { CrossSourceScreen } from "./CrossSourceScreen";

const DOMAINS = [
  { id: "dom_fin", name: "Finance", organization_id: "o1", line_of_business_id: "l1", parent_domain_id: null, code: "FIN", is_default: false, status: "ACTIVE", created_at: "", updated_at: "" },
  { id: "dom_retail", name: "Retail", organization_id: "o1", line_of_business_id: "l2", parent_domain_id: null, code: "RET", is_default: false, status: "ACTIVE", created_at: "", updated_at: "" },
];

const DATASOURCES = [
  { id: "ds_a", name: "snowflake_prod", data_domain_id: "dom_fin", organization_id: "o1", line_of_business_id: "l1", project_id: "p1", connector_type: "SNOWFLAKE", dialect: "snowflake", environment: "PROD", credential_reference: "x", status: "ACTIVE", capabilities: {}, created_at: "", updated_at: "" },
  { id: "ds_b", name: "oracle_core", data_domain_id: "dom_fin", organization_id: "o1", line_of_business_id: "l1", project_id: "p1", connector_type: "ORACLE", dialect: "oracle", environment: "PROD", credential_reference: "x", status: "ACTIVE", capabilities: {}, created_at: "", updated_at: "" },
  { id: "ds_c", name: "bigquery_mi", data_domain_id: "dom_retail", organization_id: "o1", line_of_business_id: "l2", project_id: "p2", connector_type: "BIGQUERY", dialect: "bigquery", environment: "PROD", credential_reference: "x", status: "ACTIVE", capabilities: {}, created_at: "", updated_at: "" },
];

function candidate(
  overrides: Partial<CrossSourceResolutionCandidateRead> = {},
): CrossSourceResolutionCandidateRead {
  return {
    id: "xsr_1",
    organization_id: "o1",
    source_datasource_id: "ds_a",
    source_table_id: "customer_dim",
    target_datasource_id: "ds_b",
    target_table_id: "party_master",
    detection_rule: "NAME_AND_COLUMN_OVERLAP",
    confidence: 0.88,
    evidence: { shared_column_count: 7 },
    status: "PENDING",
    created_by: "discovery",
    reviewed_by: null,
    review_reason: null,
    reviewed_at: null,
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

beforeEach(() => {
  location.hash = "";
  history.replaceState(null, "", location.pathname);
  fetchDomains.mockReset().mockResolvedValue(DOMAINS);
  fetchDatasources.mockReset().mockResolvedValue({ items: DATASOURCES, limit: 500, offset: 0, total: 3 });
  fetchCandidates.mockReset().mockResolvedValue([]);
  discoverRelationships.mockReset();
  discoverResolutions.mockReset();
  decide.mockReset();
});

async function selectDomain(value = "dom_fin") {
  await waitFor(() => expect(screen.getByLabelText("Data domain")).toBeInTheDocument());
  await waitFor(() =>
    expect(
      Array.from((screen.getByLabelText("Data domain") as HTMLSelectElement).options).length,
    ).toBeGreaterThan(1),
  );
  fireEvent.change(screen.getByLabelText("Data domain"), { target: { value } });
}

it("asks for a domain before anything else, because that is the permission boundary", async () => {
  render(<CrossSourceScreen />);
  await waitFor(() => expect(screen.getByText("Select a data domain")).toBeInTheDocument());
  // The phrase appears in the page lede too, so assert on the empty state's
  // own hint rather than on the text alone.
  expect(
    screen.getByText(
      "Cross-source work is scoped by domain, because that is the boundary permission is granted across.",
    ),
  ).toBeInTheDocument();
});

it("scopes candidate lookup to the datasources in the chosen domain", async () => {
  render(<CrossSourceScreen />);
  await selectDomain();

  await waitFor(() => expect(fetchCandidates).toHaveBeenCalled());
  const [ids, status] = fetchCandidates.mock.calls.at(-1) as [string[], string | null];
  // ds_c belongs to another domain and must not be scanned as part of this one.
  expect(ids.sort()).toEqual(["ds_a", "ds_b"]);
  expect(status).toBe("PENDING");
});

it("renders a candidate as a symmetric pair with its evidence", async () => {
  fetchCandidates.mockResolvedValue([candidate()]);
  render(<CrossSourceScreen />);
  await selectDomain();

  await waitFor(() => expect(screen.getByText("customer_dim")).toBeInTheDocument());
  expect(screen.getByText("party_master")).toBeInTheDocument();
  expect(screen.getByText("snowflake_prod")).toBeInTheDocument();
  expect(screen.getByText("oracle_core")).toBeInTheDocument();
  expect(screen.getByText("0.88")).toBeInTheDocument();
  expect(screen.getByText(/shared column count: 7/)).toBeInTheDocument();
});

it("decides a candidate rather than auto-approving on confidence", async () => {
  // Nothing here applies a threshold; the score orders the queue and a human
  // still decides every row.
  fetchCandidates.mockResolvedValue([candidate({ confidence: 0.99 })]);
  decide.mockResolvedValue(candidate({ status: "APPROVED" }));
  render(<CrossSourceScreen />);
  await selectDomain();

  await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Approve" }));

  await waitFor(() => expect(decide).toHaveBeenCalledWith("xsr_1", "APPROVE", null));
});

it("sends a reason when rejecting, which the server requires", async () => {
  fetchCandidates.mockResolvedValue([candidate()]);
  decide.mockResolvedValue(candidate({ status: "REJECTED" }));
  render(<CrossSourceScreen />);
  await selectDomain();

  await waitFor(() => expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Reject" }));

  await waitFor(() => expect(decide).toHaveBeenCalled());
  const [, decision, reason] = decide.mock.calls.at(-1) as [string, string, string | null];
  expect(decision).toBe("REJECT");
  expect(reason).toBeTruthy();
});

it("shows no decision controls for an already-decided candidate", async () => {
  fetchCandidates.mockResolvedValue([
    candidate({ status: "APPROVED", reviewed_by: "checker@example.com" }),
  ]);
  render(<CrossSourceScreen />);
  await selectDomain();

  await waitFor(() => expect(screen.getByText(/approved by checker@example.com/)).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
});

it("scans within the domain by default, and across only when asked", async () => {
  discoverRelationships.mockResolvedValue(4);
  render(<CrossSourceScreen />);
  await selectDomain();

  fireEvent.click(screen.getByRole("button", { name: "Find relationships" }));
  await waitFor(() => expect(discoverRelationships).toHaveBeenCalledWith("dom_fin", {}));

  fireEvent.change(screen.getByLabelText("Also pair against"), { target: { value: "dom_retail" } });
  fireEvent.click(screen.getByRole("button", { name: "Find relationships" }));
  await waitFor(() =>
    expect(discoverRelationships).toHaveBeenLastCalledWith("dom_fin", {
      target_data_domain_id: "dom_retail",
    }),
  );
});

it("turns a refused cross-boundary scan into the grant request, not a dead end", async () => {
  const { ApiError } = await import("../lib/api");
  discoverRelationships.mockRejectedValue(new ApiError(403, "cross-domain access denied"));
  render(<CrossSourceScreen />);
  await selectDomain();

  fireEvent.change(screen.getByLabelText("Also pair against"), { target: { value: "dom_retail" } });
  fireEvent.click(screen.getByRole("button", { name: "Find relationships" }));

  await waitFor(() =>
    expect(screen.getByText(/No active grant lets this domain scan into that one/)).toBeInTheDocument(),
  );
  // And the request form is pre-filled with the domain that was refused.
  await waitFor(() =>
    expect((screen.getByLabelText("See into which domain") as HTMLSelectElement).value).toBe(
      "dom_retail",
    ),
  );
});

it("reports a scan that found nothing as a real answer", async () => {
  discoverResolutions.mockResolvedValue(0);
  render(<CrossSourceScreen />);
  await selectDomain();

  fireEvent.click(screen.getByRole("button", { name: "Find same-object tables" }));
  await waitFor(() =>
    expect(screen.getByText(/No new object resolution candidates/)).toBeInTheDocument(),
  );
});
