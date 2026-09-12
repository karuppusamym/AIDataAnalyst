import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type {
  AnalysisRunRead,
  DataSourceRead,
  MeRead,
  ScanPolicyRead,
  ScanPolicyUpsert,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";
import type { Session } from "../lib/session";

/* ---------------------------------------------------------------------------
   Source administration (R11-B7) against the five real endpoints it wires.

   API boundary mocked at `../lib/api`, the pattern `SourcesScreen.test.tsx`
   and `EvidencePane.test.tsx` already use: real payload shapes, asserting the
   exact arguments each endpoint is called with rather than that a button
   exists.

   WHAT THESE TESTS ARE ACTUALLY FOR. Three properties, each of which was a
   real way to get this panel wrong:

     1. NOTHING FIRES ON THE CLICK. Every state-changing action opens a
        confirmation first, and the endpoint is not called until it is
        confirmed. Asserted by clicking the action and then asserting the
        endpoint was NOT called.

     2. A REFUSAL IS AN ANSWER. The scan and resume endpoints both answer 409
        with a sentence from a state machine ("only interrupted or failed runs
        can resume", whatever run admission says). That sentence has to reach
        the operator verbatim, in the dialog they are looking at, and the
        dialog has to stay open.

     3. THE SERVER IS THE AUTHORITY. After a write, the screen re-reads rather
        than editing the row in place. A resume in particular returns a
        DIFFERENT run id from the one retried, so an in-place edit would be
        wrong twice.
--------------------------------------------------------------------------- */

/* Arguments are forwarded with a rest tuple, not re-listed positionally: a
   wrapper that names `signal` passes an explicit `undefined` to the spy for
   every call that omitted it, and `toHaveBeenCalledWith(id, body)` then fails
   against `[id, body, undefined]` -- an assertion failing for the wrapper's
   shape rather than the component's. */
type PolicyArgs = [string, (AbortSignal | undefined)?];
type RunsArgs = [string, ({ limit?: number } | undefined)?, (AbortSignal | undefined)?];
type ScanArgs = [string, ({ mode?: string } | undefined)?, (AbortSignal | undefined)?];

const fetchScanPolicy = vi.fn<(...args: PolicyArgs) => Promise<ScanPolicyRead>>();
const upsertScanPolicy =
  vi.fn<
    (...args: [string, ScanPolicyUpsert, (AbortSignal | undefined)?]) => Promise<ScanPolicyRead>
  >();
const testDatasourceConnection = vi.fn<(...args: PolicyArgs) => Promise<DataSourceRead>>();
const resumeAnalysisRun = vi.fn<(...args: PolicyArgs) => Promise<AnalysisRunRead>>();
const createAnalysisRun = vi.fn<(...args: ScanArgs) => Promise<AnalysisRunRead>>();
const fetchDatasourceAnalysisRuns =
  vi.fn<(...args: RunsArgs) => Promise<PageOf<AnalysisRunRead>>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchScanPolicy: (...args: PolicyArgs) => fetchScanPolicy(...args),
    upsertScanPolicy: (...args: [string, ScanPolicyUpsert, (AbortSignal | undefined)?]) =>
      upsertScanPolicy(...args),
    testDatasourceConnection: (...args: PolicyArgs) => testDatasourceConnection(...args),
    resumeAnalysisRun: (...args: PolicyArgs) => resumeAnalysisRun(...args),
    createAnalysisRun: (...args: ScanArgs) => createAnalysisRun(...args),
    fetchDatasourceAnalysisRuns: (...args: RunsArgs) => fetchDatasourceAnalysisRuns(...args),
  };
});

let sessionMe: MeRead | null = null;
vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  return {
    ...actual,
    useSession: (): Session => ({
      state: "demo",
      me: sessionMe,
      lastSuccessAt: null,
      error: null,
      dataMode: "fixtures",
      authMode: "development",
      authModeInferred: false,
      reload: () => undefined,
    }),
  };
});

const SOURCE: DataSourceRead = {
  id: "ds_snowflake_prod", organization_id: "org1", line_of_business_id: "lob1",
  data_domain_id: "dom1", project_id: "proj1", name: "snowflake_prod",
  connector_type: "SNOWFLAKE", dialect: "snowflake", environment: "PRODUCTION",
  network_zone: "default", credential_reference: "vault://x", max_concurrency: 8,
  status: "ACTIVE", capabilities: {}, created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
};

const POLICY: ScanPolicyRead = {
  id: "pol1", organization_id: "org1", datasource_id: "ds_snowflake_prod",
  enabled: true, interval_minutes: 360, mode: "INCREMENTAL",
  // The scheduler-visible value is boosted; `base_priority` is what the admin
  // actually chose, and it is the one an editor must round-trip (ADR-0017 SS8).
  priority: 71, usage_boost_enabled: true, base_priority: 40,
  computed_usage_boost: 31, usage_boost_updated_at: "2026-09-09T04:00:00Z",
  maintenance_start_hour_utc: 2, maintenance_end_hour_utc: 5,
  next_run_at: "2026-09-13T02:30:00Z", last_triggered_at: "2026-09-12T02:30:00Z",
  created_by: "p1", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-09-12T02:30:00Z",
};

function run(overrides: Partial<AnalysisRunRead> = {}): AnalysisRunRead {
  return {
    id: "run-aaaaaaaa-1111-2222-3333-444444444444",
    organization_id: "org1", datasource_id: "ds_snowflake_prod",
    resumed_from_run_id: null, mode: "INCREMENTAL", trigger_type: "SCHEDULED",
    priority: 50, status: "COMPLETED", temporal_workflow_id: "wf-1",
    discovered_catalogs: 1, discovered_schemas: 3, discovered_tables: 42,
    discovered_columns: 310, discovered_constraints: 12,
    created_objects: 4, changed_objects: 2, deprecated_objects: 0,
    profiled_tables: 40, profiled_columns: 300,
    error_class: null, error_message: null,
    created_at: "2026-09-12T02:30:00Z", updated_at: "2026-09-12T02:41:00Z",
    ...overrides,
  };
}

const FAILED = run({
  id: "run-bbbbbbbb-1111-2222-3333-444444444444",
  status: "FAILED", error_class: "ConnectorTimeout",
  error_message: "read timed out after 600s",
});

const page = (items: AnalysisRunRead[]): PageOf<AnalysisRunRead> => ({
  items, limit: 5, offset: 0, total: items.length,
});

const onSourceChanged = vi.fn();

async function mount(source: DataSourceRead = SOURCE) {
  const { SourceAdministration } = await import("./SourcesScreenAdmin");
  render(<SourceAdministration source={source} onSourceChanged={onSourceChanged} />);
}

beforeEach(() => {
  fetchScanPolicy.mockReset();
  upsertScanPolicy.mockReset();
  testDatasourceConnection.mockReset();
  resumeAnalysisRun.mockReset();
  createAnalysisRun.mockReset();
  fetchDatasourceAnalysisRuns.mockReset();
  onSourceChanged.mockReset();
  sessionMe = null;
  fetchScanPolicy.mockResolvedValue(POLICY);
  fetchDatasourceAnalysisRuns.mockResolvedValue(page([run()]));
  vi.resetModules();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SourceAdministration — what the server reports", () => {
  it("reads the scan policy and the run history for the selected source, and renders the server's own schedule", async () => {
    await mount();

    await waitFor(() =>
      expect(fetchScanPolicy).toHaveBeenCalledWith("ds_snowflake_prod", expect.anything()),
    );
    expect(fetchDatasourceAnalysisRuns).toHaveBeenCalledWith(
      "ds_snowflake_prod",
      { limit: 5 },
      expect.anything(),
    );

    const panel = await screen.findByLabelText("Source administration");
    expect(panel).toHaveTextContent("every 6h");
    expect(panel).toHaveTextContent("02:00–05:00 UTC");
    // next_run_at and last_triggered_at are facts no client can compute.
    expect(panel).toHaveTextContent("2026-09-13 02:30 UTC");
    expect(panel).toHaveTextContent("2026-09-12 02:30 UTC");
    // The admin's own priority and the boost, told apart rather than merged.
    expect(panel).toHaveTextContent("40 as set");
    expect(panel).toHaveTextContent("71 after a +31 usage boost");
  });

  it("treats a 404 scan policy as 'never scheduled', not as an error", async () => {
    fetchScanPolicy.mockRejectedValue(new ApiError(404, "scan policy not found"));
    await mount();

    expect(await screen.findByText("This source has no scan policy")).toBeInTheDocument();
    expect(screen.queryByText("Scan policy could not be read")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create a scan policy" })).toBeInTheDocument();
  });

  it("surfaces a real scan-policy read failure as an error with a retry, not as 'never scheduled'", async () => {
    fetchScanPolicy.mockRejectedValue(new ApiError(403, "policy_denied"));
    await mount();

    expect(await screen.findByText("Scan policy could not be read")).toBeInTheDocument();
    expect(screen.getByText("policy_denied")).toBeInTheDocument();
    expect(screen.queryByText("This source has no scan policy")).not.toBeInTheDocument();
  });
});

describe("SourceAdministration — connection test", () => {
  it("confirms before probing, then reports the status the server committed", async () => {
    testDatasourceConnection.mockResolvedValue({ ...SOURCE, status: "CONNECTION_VERIFIED" });
    await mount();
    await screen.findByLabelText("Source administration");

    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    // Nothing has happened yet: the dialog is the action, the button is not.
    expect(testDatasourceConnection).not.toHaveBeenCalled();
    expect(await screen.findByText("Test this connection")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Run the test" }));

    await waitFor(() => expect(testDatasourceConnection).toHaveBeenCalledWith("ds_snowflake_prod"));
    expect(
      await screen.findByText(/This source now reports CONNECTION_VERIFIED/),
    ).toBeInTheDocument();
    // The fleet row holds this status too; it is re-read, never patched here.
    expect(onSourceChanged).toHaveBeenCalled();
  });

  it("renders a failed probe (424) in the server's own words and keeps the dialog open", async () => {
    testDatasourceConnection.mockRejectedValue(
      new ApiError(424, "datasource connection test failed"),
    );
    await mount();
    await screen.findByLabelText("Source administration");

    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    fireEvent.click(await screen.findByRole("button", { name: "Run the test" }));

    expect(await screen.findByText("datasource connection test failed")).toBeInTheDocument();
    expect(screen.getByText("Test this connection")).toBeInTheDocument();
    expect(onSourceChanged).not.toHaveBeenCalled();
  });
});

describe("SourceAdministration — re-scan", () => {
  it("confirms, sends the chosen mode, and re-reads the run list instead of adding a row", async () => {
    createAnalysisRun.mockResolvedValue(run({ status: "QUEUED", trigger_type: "MANUAL" }));
    await mount();
    await screen.findByLabelText("Source administration");

    fireEvent.change(screen.getByLabelText("Mode"), { target: { value: "FULL" } });
    fireEvent.click(screen.getByRole("button", { name: "Re-scan now" }));
    expect(createAnalysisRun).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByRole("button", { name: "Start the scan" }));

    await waitFor(() =>
      expect(createAnalysisRun).toHaveBeenCalledWith("ds_snowflake_prod", { mode: "FULL" }),
    );
    // A 202 is an accepted request; the list is re-read rather than guessed at.
    await waitFor(() => expect(fetchDatasourceAnalysisRuns).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/not a finished scan/)).toBeInTheDocument();
  });

  it("renders a 409 run-admission refusal verbatim and leaves the dialog open", async () => {
    createAnalysisRun.mockRejectedValue(
      new ApiError(409, "a run is already in flight for this datasource"),
    );
    await mount();
    await screen.findByLabelText("Source administration");

    fireEvent.click(screen.getByRole("button", { name: "Re-scan now" }));
    fireEvent.click(await screen.findByRole("button", { name: "Start the scan" }));

    expect(
      await screen.findByText("a run is already in flight for this datasource"),
    ).toBeInTheDocument();
    expect(screen.getByText("Start a scan now")).toBeInTheDocument();
    expect(fetchDatasourceAnalysisRuns).toHaveBeenCalledTimes(1);
  });
});

describe("SourceAdministration — retry a failed run", () => {
  it("offers retry only when the latest run is one the server would resume", async () => {
    await mount();
    await screen.findByLabelText("Source administration");
    expect(screen.queryByRole("button", { name: /Retry the/ })).not.toBeInTheDocument();
  });

  it("resumes the failed run by id and says a NEW run was started", async () => {
    fetchDatasourceAnalysisRuns.mockResolvedValue(page([FAILED, run()]));
    resumeAnalysisRun.mockResolvedValue(
      run({ id: "run-cccccccc", status: "QUEUED", trigger_type: "RESUME", resumed_from_run_id: FAILED.id }),
    );
    await mount();
    await screen.findByLabelText("Source administration");
    // The failed run's own reason is shown, not swallowed.
    expect(screen.getByText(/read timed out after 600s/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry the failed run" }));
    expect(resumeAnalysisRun).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByRole("button", { name: "Retry the run" }));

    await waitFor(() => expect(resumeAnalysisRun).toHaveBeenCalledWith(FAILED.id));
    expect(await screen.findByText(/started a new run resumed from/)).toBeInTheDocument();
    expect(screen.getByText(/the failed run stays as it is/)).toBeInTheDocument();
    await waitFor(() => expect(fetchDatasourceAnalysisRuns).toHaveBeenCalledTimes(2));
  });

  it("surfaces the 409 the resume gate answers with, rather than a generic failure", async () => {
    fetchDatasourceAnalysisRuns.mockResolvedValue(page([FAILED]));
    resumeAnalysisRun.mockRejectedValue(
      new ApiError(409, "only interrupted or failed runs can resume"),
    );
    await mount();
    await screen.findByLabelText("Source administration");

    fireEvent.click(screen.getByRole("button", { name: "Retry the failed run" }));
    fireEvent.click(await screen.findByRole("button", { name: "Retry the run" }));

    expect(
      await screen.findByText("only interrupted or failed runs can resume"),
    ).toBeInTheDocument();
    expect(screen.getByText("Retry this run")).toBeInTheDocument();
  });
});

describe("SourceAdministration — scan policy editing", () => {
  it("seeds the editor from the server's policy and sends the WHOLE document, untouched fields included", async () => {
    upsertScanPolicy.mockResolvedValue({ ...POLICY, interval_minutes: 720 });
    await mount();
    await screen.findByLabelText("Source administration");

    fireEvent.click(screen.getByRole("button", { name: "Edit scan policy" }));

    // The admin's own priority round-trips, not the boosted scheduler value.
    expect(screen.getByLabelText("Priority (0–100)")).toHaveValue(40);
    expect(screen.getByLabelText("Window start hour (UTC)")).toHaveValue(2);
    expect(screen.getByLabelText("Window end hour (UTC)")).toHaveValue(5);

    fireEvent.change(screen.getByLabelText("Interval (minutes)"), { target: { value: "720" } });
    fireEvent.click(screen.getByRole("button", { name: "Save scan policy" }));
    expect(upsertScanPolicy).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByRole("button", { name: "Replace policy" }));

    await waitFor(() =>
      expect(upsertScanPolicy).toHaveBeenCalledWith("ds_snowflake_prod", {
        enabled: true,
        interval_minutes: 720,
        mode: "INCREMENTAL",
        priority: 40,
        usage_boost_enabled: true,
        // The window nobody touched survives the edit, because a PUT that
        // omitted it would silently clear it back to the schema default.
        maintenance_start_hour_utc: 2,
        maintenance_end_hour_utc: 5,
      }),
    );
    await waitFor(() => expect(fetchScanPolicy).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Scan policy updated.")).toBeInTheDocument();
  });

  it("refuses a half-set maintenance window before the request, in the rule's own terms", async () => {
    await mount();
    await screen.findByLabelText("Source administration");
    fireEvent.click(screen.getByRole("button", { name: "Edit scan policy" }));

    fireEvent.change(screen.getByLabelText("Window end hour (UTC)"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save scan policy" }));

    expect(
      await screen.findByText("Give both maintenance-window hours, or neither."),
    ).toBeInTheDocument();
    expect(upsertScanPolicy).not.toHaveBeenCalled();
  });

  it("creates a policy for a source that has never been scheduled, from the schema's own defaults", async () => {
    fetchScanPolicy.mockRejectedValueOnce(new ApiError(404, "scan policy not found"));
    fetchScanPolicy.mockResolvedValue(POLICY);
    upsertScanPolicy.mockResolvedValue(POLICY);
    await mount();
    await screen.findByText("This source has no scan policy");

    fireEvent.click(screen.getByRole("button", { name: "Create a scan policy" }));
    fireEvent.click(screen.getByRole("button", { name: "Create scan policy" }));
    fireEvent.click(await screen.findByRole("button", { name: "Create policy" }));

    await waitFor(() =>
      expect(upsertScanPolicy).toHaveBeenCalledWith("ds_snowflake_prod", {
        enabled: true,
        interval_minutes: 1440,
        mode: "INCREMENTAL",
        priority: 50,
        usage_boost_enabled: false,
        maintenance_start_hour_utc: null,
        maintenance_end_hour_utc: null,
      }),
    );
    expect(await screen.findByText("Scan policy created.")).toBeInTheDocument();
  });

  it("surfaces a 409/422 the policy endpoint refuses with, without closing the dialog", async () => {
    upsertScanPolicy.mockRejectedValue(
      new ApiError(422, "maintenance-window hours cannot be equal"),
    );
    await mount();
    await screen.findByLabelText("Source administration");
    fireEvent.click(screen.getByRole("button", { name: "Edit scan policy" }));
    fireEvent.click(screen.getByRole("button", { name: "Save scan policy" }));
    fireEvent.click(await screen.findByRole("button", { name: "Replace policy" }));

    expect(
      await screen.findByText("maintenance-window hours cannot be equal"),
    ).toBeInTheDocument();
    expect(screen.getByText("Replace this scan policy")).toBeInTheDocument();
  });
});

describe("SourceAdministration — who may act", () => {
  it("disables the writes a principal's roles do not carry, and says which role is needed", async () => {
    sessionMe = {
      principal_id: "p1", principal_type: "USER", organization_id: "org1",
      roles: ["Viewer"], persona: null, identity_provider: "development",
    };
    await mount();
    await screen.findByLabelText("Source administration");

    expect(screen.getByRole("button", { name: "Test connection" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Re-scan now" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Edit scan policy" })).toBeDisabled();
    expect(
      screen.getByText("Testing a connection requires Data Admin or Platform Admin."),
    ).toBeInTheDocument();
    // A Viewer may still READ the schedule — that is what the endpoint's own
    // role list says, and hiding it would claim otherwise.
    expect(screen.getByText("every 6h · incremental")).toBeInTheDocument();
  });
});

describe("policy form helpers", () => {
  it("rejects exactly what the server's own validator rejects", async () => {
    const { POLICY_DEFAULTS, validatePolicyForm } = await import("./SourcesScreenAdmin");
    expect(validatePolicyForm(POLICY_DEFAULTS)).toBeNull();
    expect(validatePolicyForm({ ...POLICY_DEFAULTS, intervalMinutes: "4" })).toMatch(/between 5/);
    expect(validatePolicyForm({ ...POLICY_DEFAULTS, intervalMinutes: "525601" })).toMatch(/between 5/);
    expect(validatePolicyForm({ ...POLICY_DEFAULTS, priority: "101" })).toMatch(/between 0 and 100/);
    expect(
      validatePolicyForm({ ...POLICY_DEFAULTS, windowStart: "3", windowEnd: "3" }),
    ).toMatch(/same hour/);
    expect(validatePolicyForm({ ...POLICY_DEFAULTS, windowStart: "3" })).toMatch(/both/);
  });

  it("sends `start_at` with a timezone, because the handler 422s a naive datetime", async () => {
    const { POLICY_DEFAULTS, policyFormToBody } = await import("./SourcesScreenAdmin");
    const bare = policyFormToBody(POLICY_DEFAULTS);
    expect(bare.start_at).toBeUndefined();

    const scheduled = policyFormToBody({ ...POLICY_DEFAULTS, startAt: "2026-10-01T03:15" });
    expect(scheduled.start_at).toMatch(/Z$/);
    expect(new Date(scheduled.start_at!).getTime()).toBe(new Date("2026-10-01T03:15").getTime());
  });

  it("round-trips the admin's chosen priority, never the boosted one", async () => {
    const { policyToForm } = await import("./SourcesScreenAdmin");
    expect(policyToForm(POLICY).priority).toBe("40");
    expect(policyToForm(null).priority).toBe("50");
  });
});
