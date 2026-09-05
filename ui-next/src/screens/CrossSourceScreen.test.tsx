import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { CrossSourceResolutionCandidateRead } from "../lib/_cross_source_api";

const fetchDomains = vi.fn();
const fetchCandidates = vi.fn();
const discoverRelationships = vi.fn();
const discoverResolutions = vi.fn();
const decide = vi.fn();
const fetchRelationships = vi.fn();
const decideRelationship = vi.fn();
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
    fetchCrossSourceRelationshipCandidates: (...a: unknown[]) => fetchRelationships(...a),
    decideRelationshipCandidate: (...a: unknown[]) => decideRelationship(...a),
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
  fetchRelationships.mockReset().mockResolvedValue([]);
  decideRelationship.mockReset();
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


/* ---------------------------------------------------------------------------
   Cross-source relationship review (2026-09-05). Discovery lived here from the
   start; the results were reviewed on the per-datasource Relationships screen
   -- the one scope that cannot express "this column points into another
   system", so the rows a steward most needed were the ones its filter hid.
--------------------------------------------------------------------------- */

function relationship(overrides: Record<string, unknown> = {}) {
  return {
    id: "xrc_1",
    organization_id: "o1",
    datasource_id: "ds_b",
    target_datasource_id: "ds_a",
    source_table_id: "party_master",
    source_column_id: "party_id",
    target_table_id: "customer_dim",
    target_column_id: "customer_id",
    detection_rule: "NAME_AND_TYPE_AND_INCLUSION",
    confidence: 0.91,
    evidence: { inclusion_ratio: 0.97 },
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

it("reviews cross-source relationships on the screen that discovers them", async () => {
  fetchRelationships.mockResolvedValue([relationship()]);
  render(<CrossSourceScreen />);
  await selectDomain();

  await waitFor(() => expect(screen.getByText("party_id")).toBeInTheDocument());
  expect(screen.getByText("customer_id")).toBeInTheDocument();
  expect(screen.getByText("0.91")).toBeInTheDocument();
  expect(screen.getByText(/inclusion ratio: 0.97/)).toBeInTheDocument();
});

it("renders a relationship directionally, unlike a same-object pair", async () => {
  // A foreign-key-like relationship points one way; rendering it as `≡` would
  // misstate which side is the reference.
  fetchRelationships.mockResolvedValue([relationship()]);
  render(<CrossSourceScreen />);
  await selectDomain();

  await waitFor(() => expect(screen.getByLabelText("references")).toBeInTheDocument());
});

it("decides a relationship through its own endpoint, not the resolution one", async () => {
  fetchRelationships.mockResolvedValue([relationship()]);
  decideRelationship.mockResolvedValue(relationship({ status: "APPROVED" }));
  render(<CrossSourceScreen />);
  await selectDomain();

  await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Approve" }));

  await waitFor(() => expect(decideRelationship).toHaveBeenCalledWith("xrc_1", "APPROVE", null));
  expect(decide).not.toHaveBeenCalled();
});

it("counts pending work across both queues, not just one", async () => {
  fetchRelationships.mockResolvedValue([relationship()]);
  fetchCandidates.mockResolvedValue([candidate()]);
  render(<CrossSourceScreen />);
  await selectDomain();

  await waitFor(() => expect(screen.getByText("2")).toBeInTheDocument());
});

it("points same-source candidates at the screen that owns them", async () => {
  fetchRelationships.mockResolvedValue([]);
  render(<CrossSourceScreen />);
  await selectDomain();

  await waitFor(() =>
    expect(screen.getByText("No cross-source relationships")).toBeInTheDocument(),
  );
  expect(
    screen.getByText(/Same-source candidates are reviewed on the Relationships screen/),
  ).toBeInTheDocument();
});
