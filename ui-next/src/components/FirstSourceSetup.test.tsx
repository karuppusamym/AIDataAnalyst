import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import type { AnalysisRunRead, DataSourceRead } from "../lib/types";

import {
  currentStepOf,
  deriveSetupSteps,
  type SetupSignals,
  type SetupStep,
} from "./FirstSourceSetup";

/* ---------------------------------------------------------------------------
   T15 — readiness is a question the server answers.

   These assert the three things the review says must not happen: a step marked
   done from a local flag, a signal nobody may read reported as zero, and a
   failed scan reported as one that has not happened yet.
--------------------------------------------------------------------------- */

const SOURCE: DataSourceRead = {
  id: "ds_1",
  organization_id: "org1",
  line_of_business_id: "lob",
  data_domain_id: "dom",
  project_id: "proj",
  name: "warehouse",
  connector_type: "POSTGRES",
  dialect: "postgresql",
  environment: "PRODUCTION",
  credential_reference: "vault://ds/warehouse",
  status: "ACTIVE",
  capabilities: {},
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

function run(overrides: Partial<AnalysisRunRead>): AnalysisRunRead {
  return {
    id: "run_1",
    organization_id: "org1",
    datasource_id: "ds_1",
    resumed_from_run_id: null,
    mode: "FULL",
    trigger_type: "MANUAL",
    priority: 50,
    status: "COMPLETED",
    temporal_workflow_id: null,
    discovered_catalogs: 1,
    discovered_schemas: 2,
    discovered_tables: 12,
    discovered_columns: 90,
    discovered_constraints: 4,
    created_objects: 12,
    changed_objects: 0,
    deprecated_objects: 0,
    profiled_tables: 12,
    profiled_columns: 90,
    error_class: null,
    error_message: null,
    created_at: "2026-09-02T00:00:00Z",
    updated_at: "2026-09-02T00:10:00Z",
    ...overrides,
  };
}

function signals(overrides: Partial<SetupSignals> = {}): SetupSignals {
  return {
    workspaces: { kind: "ok", value: { total: 1, active: 1 } },
    datasources: { kind: "ok", value: [SOURCE] },
    runs: { kind: "ok", value: [run({})] },
    catalog: { kind: "ok", value: { rows: 1, total: 12 } },
    consumption: { kind: "ok", value: { runs: 0 } },
    subject: SOURCE,
    ...overrides,
  };
}

const byId = (steps: SetupStep[], id: SetupStep["id"]): SetupStep => {
  const step = steps.find((candidate) => candidate.id === id);
  if (!step) throw new Error(`no step ${id}`);
  return step;
};

describe("deriveSetupSteps", () => {
  it("reports a failed scan as failed, with the server's reason, not as pending", () => {
    const steps = deriveSetupSteps(
      signals({
        runs: {
          kind: "ok",
          value: [
            run({
              status: "FAILED",
              error_class: "ConnectorAuthError",
              error_message: "password authentication failed",
            }),
          ],
        },
      }),
    );

    const scan = byId(steps, "scan");
    expect(scan.state).toBe("failed");
    expect(scan.detail).toContain("ConnectorAuthError");
    expect(scan.detail).toContain("password authentication failed");
    // And it offers the retry, rather than "start the first scan".
    expect(scan.action?.label).toBe("Run the scan again");
  });

  it("keeps a queued or running scan distinct from a finished one", () => {
    expect(byId(deriveSetupSteps(signals({ runs: { kind: "ok", value: [run({ status: "RUNNING" })] } })), "scan").state).toBe("running");
    expect(byId(deriveSetupSteps(signals({ runs: { kind: "ok", value: [run({ status: "QUEUED" })] } })), "scan").state).toBe("running");
    expect(byId(deriveSetupSteps(signals({ runs: { kind: "ok", value: [] } })), "scan").state).toBe("todo");
  });

  it("reports a signal this principal may not read as unknown, never as zero", () => {
    const steps = deriveSetupSteps(
      signals({ runs: { kind: "denied", message: "cross-organization access denied" } }),
    );

    const scan = byId(steps, "scan");
    expect(scan.state).toBe("unknown");
    expect(scan.detail).toContain("do not have permission");
    expect(scan.detail).toContain("unknown");
    // Crucially it is neither "never scanned" nor "done".
    expect(scan.detail).not.toContain("never been scanned");
    expect(scan.action).toBeUndefined();
  });

  it("names a source that connected and returned nothing as a failure path", () => {
    const steps = deriveSetupSteps(signals({ catalog: { kind: "ok", value: { rows: 0, total: 0 } } }));

    const catalog = byId(steps, "catalog");
    expect(catalog.state).toBe("failed");
    expect(catalog.detail).toContain("received no rows");
  });

  it("does not call an empty catalog a failure while the scan has not finished", () => {
    const steps = deriveSetupSteps(
      signals({
        runs: { kind: "ok", value: [run({ status: "RUNNING" })] },
        catalog: { kind: "ok", value: { rows: 0, total: 0 } },
      }),
    );
    expect(byId(steps, "catalog").state).toBe("todo");
  });

  it("treats a registered but non-ACTIVE source as needing attention", () => {
    const disabled = { ...SOURCE, status: "DISABLED" };
    const steps = deriveSetupSteps(
      signals({ datasources: { kind: "ok", value: [disabled] }, subject: disabled }),
    );
    const source = byId(steps, "source");
    expect(source.state).toBe("failed");
    expect(source.detail).toContain("DISABLED");
  });

  it("withholds the first-consumer action until assets are actually visible", () => {
    const steps = deriveSetupSteps(signals({ catalog: { kind: "ok", value: { rows: 0, total: 0 } } }));
    const consume = byId(steps, "consume");
    expect(consume.action).toBeUndefined();
    expect(consume.prerequisite).toContain("visible in the catalog");
  });

  it("resumes on the first step that is not done", () => {
    // Everything but the scan is satisfied -> that is where a returning user
    // lands, computed here rather than remembered anywhere.
    const steps = deriveSetupSteps(
      signals({
        runs: { kind: "ok", value: [] },
        consumption: { kind: "ok", value: { runs: 3 } },
      }),
    );
    expect(currentStepOf(steps)?.id).toBe("scan");

    const finished = deriveSetupSteps(signals({ consumption: { kind: "ok", value: { runs: 3 } } }));
    expect(currentStepOf(finished)).toBeNull();
  });
});

/* --------------------------------------------------------------------------
   The component half: the same derivation, driven by real reads, and immune
   to whatever the browser remembers.
-------------------------------------------------------------------------- */

const fetchOrgWorkspaces = vi.fn();
const listOrgDatasources = vi.fn();
const fetchDatasourceAnalysisRuns = vi.fn();
const fetchCatalogRows = vi.fn();
const fetchAgentRuns = vi.fn();
const createAnalysisRun = vi.fn();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgWorkspaces: (...args: unknown[]) => fetchOrgWorkspaces(...args),
    listOrgDatasources: (...args: unknown[]) => listOrgDatasources(...args),
    fetchDatasourceAnalysisRuns: (...args: unknown[]) => fetchDatasourceAnalysisRuns(...args),
    fetchCatalogRows: (...args: unknown[]) => fetchCatalogRows(...args),
    fetchAgentRuns: (...args: unknown[]) => fetchAgentRuns(...args),
    createAnalysisRun: (...args: unknown[]) => createAnalysisRun(...args),
  };
});

beforeEach(() => {
  for (const mock of [
    fetchOrgWorkspaces,
    listOrgDatasources,
    fetchDatasourceAnalysisRuns,
    fetchCatalogRows,
    fetchAgentRuns,
    createAnalysisRun,
  ]) {
    mock.mockReset();
  }
  fetchOrgWorkspaces.mockResolvedValue({ items: [{ status: "ACTIVE" }], total: 1 });
  listOrgDatasources.mockResolvedValue({ items: [SOURCE], total: 1 });
  fetchDatasourceAnalysisRuns.mockResolvedValue({ items: [], total: 0 });
  fetchCatalogRows.mockResolvedValue({ items: [], total: 0 });
  fetchAgentRuns.mockResolvedValue({ items: [], total: 0 });
  localStorage.clear();
  history.replaceState(null, "", "/");
  vi.resetModules();
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

async function loadPanel() {
  const module = await import("./FirstSourceSetup");
  return module.FirstSourceSetup;
}

describe("FirstSourceSetup", () => {
  it("recomputes each step from the server even when local storage claims setup is finished", async () => {
    /* The onboarding checklist is per-browser and proves nothing. Seed it as
       fully ticked, and seed this panel's own key too: neither may promote a
       step whose evidence the server has not produced. */
    localStorage.setItem(
      "atlas.onboarding.00000000-0000-0000-0000-000000000001.anonymous.Operator.done",
      JSON.stringify(["sources", "operations", "catalog", "administration"]),
    );
    fetchDatasourceAnalysisRuns.mockResolvedValue({
      items: [run({ status: "FAILED", error_class: "ConnectorAuthError" })],
      total: 1,
    });

    const FirstSourceSetup = await loadPanel();
    render(<FirstSourceSetup onNavigate={vi.fn()} />);

    const scan = await screen.findByText("Scan the source");
    const item = scan.closest("li") as HTMLElement;
    expect(item).not.toBeNull();
    await waitFor(() =>
      expect(within(item).getByText("needs attention")).toBeInTheDocument(),
    );
    expect(within(item).getByText(/ConnectorAuthError/)).toBeInTheDocument();
    // Nothing about completion was written; only the collapse convenience key
    // may ever be stored by this panel.
    expect(
      Object.keys(localStorage).filter((k) => k.startsWith("atlas.setup")),
    ).toEqual([]);
  });

  it("starts a scan and re-reads the run rather than reporting the click as success", async () => {
    createAnalysisRun.mockResolvedValue(run({ status: "QUEUED" }));
    const FirstSourceSetup = await loadPanel();
    render(<FirstSourceSetup onNavigate={vi.fn()} />);

    const button = await screen.findByRole("button", { name: "Start the first scan" });
    fetchDatasourceAnalysisRuns.mockResolvedValue({ items: [run({ status: "QUEUED" })], total: 1 });
    button.click();

    await waitFor(() => expect(createAnalysisRun).toHaveBeenCalledWith("ds_1", { mode: "FULL" }));
    // A 202 is an accepted request. The step's state comes from the re-read,
    // and says "in progress", not "done".
    await waitFor(() => expect(fetchDatasourceAnalysisRuns).toHaveBeenCalledTimes(2));
    const item = (await screen.findByText("Scan the source")).closest("li") as HTMLElement;
    await waitFor(() => expect(within(item).getByText("in progress")).toBeInTheDocument());
  });

  it("says a source has not been registered rather than showing an empty step", async () => {
    listOrgDatasources.mockResolvedValue({ items: [], total: 0 });
    const FirstSourceSetup = await loadPanel();
    render(<FirstSourceSetup onNavigate={vi.fn()} />);

    expect(
      await screen.findByText("No data source is registered in this organization."),
    ).toBeInTheDocument();
    // With no source, the per-source signals are never requested -- and their
    // steps say "no source has been chosen", not "nothing found".
    expect(fetchDatasourceAnalysisRuns).not.toHaveBeenCalled();
    expect(screen.getAllByText(/No source has been chosen/).length).toBeGreaterThan(0);
  });
});
