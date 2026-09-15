import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type {
  DataSourceRead,
  DiscoverySelection,
  DiscoverySelectionPreviewRead,
  DiscoverySelectionRead,
} from "../lib/types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   Discovery scope (R11-FP01) against its three real endpoints, mocked at
   `../lib/api` with real payload shapes.

   The properties worth a test, each a way to mislead an operator:
     1. A preview stores nothing, and a preview of an edited form is discarded.
     2. Saving confirms first, says narrowing never retires anything, and
        re-reads the scope from the server afterwards.
     3. Unticking every kind is refused before the request -- the server reads
        an empty kind list as "everything", the opposite of what was meant.
     4. "Not supported" and "n/a" stay two different answers.
--------------------------------------------------------------------------- */

type ReadArgs = [string, (AbortSignal | undefined)?];
type BodyArgs = [string, DiscoverySelection, (AbortSignal | undefined)?];

const fetchDiscoverySelection = vi.fn<(...args: ReadArgs) => Promise<DiscoverySelectionRead>>();
const previewDiscoverySelection =
  vi.fn<(...args: BodyArgs) => Promise<DiscoverySelectionPreviewRead>>();
const putDiscoverySelection = vi.fn<(...args: BodyArgs) => Promise<DiscoverySelectionRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchDiscoverySelection: (...args: ReadArgs) => fetchDiscoverySelection(...args),
    previewDiscoverySelection: (...args: BodyArgs) => previewDiscoverySelection(...args),
    putDiscoverySelection: (...args: BodyArgs) => putDiscoverySelection(...args),
  };
});

const SOURCE: DataSourceRead = {
  id: "ds_mssql", organization_id: "org1", line_of_business_id: "lob1",
  data_domain_id: "dom1", project_id: "proj1", name: "bank_mssql",
  connector_type: "sqlserver", dialect: "tsql", environment: "PRODUCTION",
  network_zone: "default", credential_reference: "vault://x", max_concurrency: 4,
  status: "ACTIVE", capabilities: {}, created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
};

const UNRESTRICTED: DiscoverySelectionRead = {
  datasource_id: "ds_mssql",
  selection: {},
  restricted: false,
  fingerprint: null,
  capabilities: [
    { kind: "TABLE", inventory: "SUPPORTED", definition: "NOT_APPLICABLE" },
    { kind: "VIEW", inventory: "SUPPORTED", definition: "SUPPORTED" },
    { kind: "MATERIALIZED_VIEW", inventory: "NOT_APPLICABLE", definition: "NOT_APPLICABLE" },
    { kind: "PROCEDURE", inventory: "SUPPORTED", definition: "UNSUPPORTED" },
    { kind: "FUNCTION", inventory: "SUPPORTED", definition: "SUPPORTED" },
    { kind: "PACKAGE", inventory: "NOT_APPLICABLE", definition: "NOT_APPLICABLE" },
  ],
  capability_source: "CONNECTOR_DEFAULT",
};

const PREVIEW: DiscoverySelectionPreviewRead = {
  datasource_id: "ds_mssql",
  restricted: true,
  fingerprint: "ab12cd34ef56aa",
  basis: "LAST_SCAN",
  schemas: { kind: "SCHEMA", in_scope: 3, excluded: 1 },
  kinds: [
    { kind: "TABLE", in_scope: 40, excluded: 7 },
    { kind: "VIEW", in_scope: 0, excluded: 5 },
  ],
  unmatched_include_patterns: ["retial.*"],
  truncated: false,
  capabilities: UNRESTRICTED.capabilities,
  capability_source: "CONNECTOR_DEFAULT",
};

async function mount(mayEdit = true) {
  const { DiscoveryScope } = await import("./SourcesScreenDiscoveryScope");
  render(<DiscoveryScope source={SOURCE} mayEdit={mayEdit} />);
  await screen.findByText("every kind the connector reports");
}

async function openAndNarrow() {
  fireEvent.click(screen.getByRole("button", { name: "Edit discovery scope" }));
  fireEvent.click(screen.getByLabelText("Views"));
  fireEvent.change(screen.getByLabelText("Exclude schemas"), { target: { value: "scratch" } });
  fireEvent.change(screen.getByLabelText("Include objects (schema.object)"), {
    target: { value: "retail.*\nretial.*" },
  });
}

const NARROWED_BODY: DiscoverySelection = {
  object_kinds: ["TABLE", "MATERIALIZED_VIEW", "PROCEDURE", "FUNCTION", "PACKAGE"],
  include_schemas: [],
  exclude_schemas: ["scratch"],
  include_objects: ["retail.*", "retial.*"],
  exclude_objects: [],
};

beforeEach(() => {
  fetchDiscoverySelection.mockReset();
  previewDiscoverySelection.mockReset();
  putDiscoverySelection.mockReset();
  fetchDiscoverySelection.mockResolvedValue(UNRESTRICTED);
  vi.resetModules();
});

describe("DiscoveryScope — what the server reports", () => {
  it("reads the scope and keeps 'not supported' apart from 'n/a' per kind", async () => {
    await mount();

    expect(fetchDiscoverySelection).toHaveBeenCalledWith("ds_mssql", expect.anything());
    const table = screen.getByRole("table", { name: "What this connector discovers" });
    const materialized = screen.getByRole("row", { name: /Materialized views/ });
    expect(materialized).toHaveTextContent("n/a");
    expect(screen.getByRole("row", { name: /Procedures/ })).toHaveTextContent("not supported");
    expect(table).toBeInTheDocument();
    expect(screen.getByText(/declared defaults/)).toBeInTheDocument();
    expect(screen.getByText("none (unrestricted)")).toBeInTheDocument();
  });

  it("renders a read failure as an error with a retry", async () => {
    fetchDiscoverySelection.mockRejectedValue(new ApiError(403, "scope_denied"));
    const { DiscoveryScope } = await import("./SourcesScreenDiscoveryScope");
    render(<DiscoveryScope source={SOURCE} mayEdit />);

    expect(await screen.findByText("Discovery scope could not be read")).toBeInTheDocument();
    expect(screen.getByText("scope_denied")).toBeInTheDocument();
  });
});

describe("DiscoveryScope — preview", () => {
  it("previews the edited scope without saving it, and names the include that matches nothing", async () => {
    previewDiscoverySelection.mockResolvedValue(PREVIEW);
    await mount();
    await openAndNarrow();

    fireEvent.click(screen.getByRole("button", { name: "Preview" }));

    await waitFor(() =>
      expect(previewDiscoverySelection).toHaveBeenCalledWith("ds_mssql", NARROWED_BODY),
    );
    const region = await screen.findByRole("region", { name: "Scope preview" });
    expect(region).toHaveTextContent("Counted over the last completed scan");
    expect(screen.getByRole("row", { name: /Views 0 5/ })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("retial.*");
    expect(putDiscoverySelection).not.toHaveBeenCalled();
  });

  it("discards a preview once the form it described is edited", async () => {
    previewDiscoverySelection.mockResolvedValue(PREVIEW);
    await mount();
    await openAndNarrow();
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByRole("region", { name: "Scope preview" });

    fireEvent.change(screen.getByLabelText("Exclude schemas"), { target: { value: "scratch\ntmp" } });

    expect(screen.queryByRole("region", { name: "Scope preview" })).not.toBeInTheDocument();
  });
});

describe("DiscoveryScope — saving", () => {
  it("confirms first, says nothing is retired, sends the body and re-reads the scope", async () => {
    putDiscoverySelection.mockResolvedValue({ ...UNRESTRICTED, restricted: true });
    await mount();
    await openAndNarrow();

    fireEvent.click(screen.getByRole("button", { name: "Save discovery scope" }));
    expect(putDiscoverySelection).not.toHaveBeenCalled();
    expect(await screen.findByText(/nothing earlier scans found is retired/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Save scope" }));

    await waitFor(() =>
      expect(putDiscoverySelection).toHaveBeenCalledWith("ds_mssql", NARROWED_BODY),
    );
    await waitFor(() => expect(fetchDiscoverySelection).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/applies from the next scan/)).toBeInTheDocument();
  });

  it("keeps the dialog open with the server's own refusal", async () => {
    putDiscoverySelection.mockRejectedValue(new ApiError(422, "a pattern may not contain control characters"));
    await mount();
    await openAndNarrow();
    fireEvent.click(screen.getByRole("button", { name: "Save discovery scope" }));
    fireEvent.click(await screen.findByRole("button", { name: "Save scope" }));

    expect(
      await screen.findByText("a pattern may not contain control characters"),
    ).toBeInTheDocument();
    expect(screen.getByText("Save this discovery scope")).toBeInTheDocument();
  });

  it("refuses a scope with no kind ticked before any request", async () => {
    await mount();
    fireEvent.click(screen.getByRole("button", { name: "Edit discovery scope" }));
    for (const label of ["Tables", "Views", "Materialized views", "Procedures", "Functions", "Packages"]) {
      fireEvent.click(screen.getByLabelText(label));
    }

    fireEvent.click(screen.getByRole("button", { name: "Save discovery scope" }));
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));

    expect(await screen.findByText(/Tick at least one object kind/)).toBeInTheDocument();
    expect(putDiscoverySelection).not.toHaveBeenCalled();
    expect(previewDiscoverySelection).not.toHaveBeenCalled();
  });

  it("lets a principal without the write roles read the scope but not edit it", async () => {
    await mount(false);

    expect(screen.getByRole("button", { name: "Edit discovery scope" })).toBeDisabled();
    expect(
      screen.getByText(/Changing the discovery scope requires Data Admin/),
    ).toBeInTheDocument();
  });
});

describe("scope form helpers", () => {
  it("sends every kind ticked as no restriction, and never an empty list for none", async () => {
    const { SCOPE_KINDS, scopeFormToBody, scopeToForm, validateScopeForm } = await import(
      "./SourcesScreenDiscoveryScope"
    );
    const everything = scopeToForm(undefined);
    expect(everything.kinds).toEqual([...SCOPE_KINDS]);
    expect(scopeFormToBody(everything).object_kinds).toEqual([]);
    expect(validateScopeForm({ ...everything, kinds: [] })).toMatch(/at least one object kind/);
  });

  it("splits patterns on lines and commas and removes case-insensitive duplicates", async () => {
    const { MAX_PATTERNS, parsePatterns, scopeToForm, validateScopeForm } = await import(
      "./SourcesScreenDiscoveryScope"
    );
    expect(parsePatterns(" Sales \nsales, finance\n\n")).toEqual(["Sales", "finance"]);
    const tooMany = Array.from({ length: MAX_PATTERNS + 1 }, (_, i) => `s${i}`).join("\n");
    expect(validateScopeForm({ ...scopeToForm(undefined), excludeSchemas: tooMany })).toMatch(
      /at most 100 patterns/,
    );
  });
});
