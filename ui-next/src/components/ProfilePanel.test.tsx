import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ApiError } from "../lib/api";
import type { ColumnProfileRead, TableProfileRead } from "../lib/types";

/* ---------------------------------------------------------------------------
   R11-FP04. `ProfilePanel` states three rules in its own header, each of which
   is a decision that a later edit could quietly undo. These are the tests that
   make undoing them fail:

   1. the limitation leads -- a SAMPLE profile must say its numbers describe
      the sample, before it shows any of them;
   2. absent is not zero -- a facet nobody computed renders its reason, and the
      four reasons stay distinguishable;
   3. withheld is not missing -- a column whose facets a policy withheld stays
      in the list with its marker.

   Plus the two error branches that are deliberately different from each other:
   a never-profiled table is not a fault, an unauthorized one is.
--------------------------------------------------------------------------- */

const fetchTableProfile = vi.fn<(tableId: string, signal?: AbortSignal) => Promise<TableProfileRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchTableProfile: (tableId: string, signal?: AbortSignal) => fetchTableProfile(tableId, signal),
  };
});

function column(overrides: Partial<ColumnProfileRead> = {}): ColumnProfileRead {
  return {
    column_id: "c1",
    column_name: "customer_id",
    classification: "UNCLASSIFIED",
    null_count: 0,
    non_null_count: 1000,
    approximate_distinct_count: 1000,
    min_length: 3,
    max_length: 12,
    ...overrides,
  };
}

function profileOf(overrides: Partial<TableProfileRead> = {}): TableProfileRead {
  return {
    id: "p1",
    analysis_run_id: "r1",
    table_id: "t1",
    row_count_estimate: 10_000_000,
    sampled_row_count: 1000,
    profile_version: "safe-v1",
    status: "COMPLETED",
    created_at: "2026-09-16T00:00:00Z",
    columns: [column()],
    observation_scope: "FULL",
    ...overrides,
  };
}

async function loadPanel() {
  const { ProfilePanel } = await import("./ProfilePanel");
  return ProfilePanel;
}

beforeEach(() => {
  fetchTableProfile.mockReset();
  vi.resetModules();
});

describe("ProfilePanel", () => {
  it("rule 1: a bounded sample says its statistics describe the sample, not the table", async () => {
    fetchTableProfile.mockResolvedValue(
      profileOf({ observation_scope: "SAMPLE", sampled_row_count: 1000 }),
    );
    const ProfilePanel = await loadPanel();
    render(<ProfilePanel tableId="t1" />);

    await waitFor(() =>
      expect(screen.getByText(/describes the sample, not the table/i)).toBeInTheDocument(),
    );
    // The scope tag is the qualifier, and it is present rather than implied.
    expect(screen.getByText("SAMPLE")).toBeInTheDocument();
  });

  it("rule 1: a profile taken before scope was recorded refuses to imply it saw everything", async () => {
    fetchTableProfile.mockResolvedValue(profileOf({ observation_scope: null }));
    const ProfilePanel = await loadPanel();
    render(<ProfilePanel tableId="t1" />);

    await waitFor(() => expect(screen.getByText("NOT RECORDED")).toBeInTheDocument());
    expect(screen.getByText(/predates observation-scope recording/i)).toBeInTheDocument();
  });

  it("rule 2: an uncomputed facet renders its reason, and the reasons stay distinguishable", async () => {
    fetchTableProfile.mockResolvedValue(
      profileOf({
        columns: [
          column({
            unavailable_facets: [
              { facet: "ENTROPY", status: "UNSUPPORTED", reason_code: "CONNECTOR_CANNOT" },
              { facet: "LENGTH", status: "NOT_APPLICABLE", reason_code: "TYPE_HAS_NO_TEXT_FORM" },
            ],
          }),
        ],
      }),
    );
    const ProfilePanel = await loadPanel();
    render(<ProfilePanel tableId="t1" />);

    await waitFor(() => expect(screen.getByText(/No distribution entropy/i)).toBeInTheDocument());
    expect(screen.getByText(/No value length/i)).toBeInTheDocument();
    // Two different answers to "should I ask again" must not collapse into one.
    const entropy = screen.getByText(/No distribution entropy/i).textContent ?? "";
    const length = screen.getByText(/No value length/i).textContent ?? "";
    expect(entropy).not.toEqual(length);
  });

  it("rule 3: a withheld column stays in the list rather than being dropped", async () => {
    fetchTableProfile.mockResolvedValue(
      profileOf({
        withheld_column_count: 1,
        columns: [
          column({ column_id: "c2", column_name: "national_id", facets_withheld: true }),
        ],
      }),
    );
    const ProfilePanel = await loadPanel();
    render(<ProfilePanel tableId="t1" />);

    // Dropping it would let a reader draw a conclusion about the data from a
    // fact about their own entitlement.
    await waitFor(() => expect(screen.getByText("national_id")).toBeInTheDocument());
    expect(screen.getByText(/withheld by an access policy/i)).toBeInTheDocument();
    expect(screen.getByText(/not removed/i)).toBeInTheDocument();
  });

  it("a table that was never profiled is answered as such, not as a failure", async () => {
    fetchTableProfile.mockRejectedValue(new ApiError(404, "not_found"));
    const ProfilePanel = await loadPanel();
    render(<ProfilePanel tableId="t1" />);

    await waitFor(() =>
      expect(screen.getByText(/has not been profiled yet/i)).toBeInTheDocument(),
    );
    // Saying "could not load" would send a steward looking for a fault.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("a refused profile says so, and says it as an alert", async () => {
    fetchTableProfile.mockRejectedValue(new ApiError(403, "policy_denied"));
    const ProfilePanel = await loadPanel();
    render(<ProfilePanel tableId="t1" />);

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/not authorized/i),
    );
  });
});
