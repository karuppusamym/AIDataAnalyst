import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type {
  ArchiveStatusRead,
  EvaluationResponse,
  NotificationRuleRead,
  SlaStatusResponse,
} from "../lib/types";
import type { PageOf, ViolationRead } from "../lib/ui-types";
import { DEFAULT_ORG_ID } from "../lib/org";

/* ---------------------------------------------------------------------------
   Reliability against the real `observability_api.py` / `notification_api.py`
   / `runtime_contracts_api.py` endpoints. Mocks the API boundary, matching
   `QualityScreen.test.tsx`/`ContextProductsScreen.test.tsx`'s established
   pattern.
--------------------------------------------------------------------------- */

const fetchArchiveStatus = vi.fn<(signal?: AbortSignal) => Promise<ArchiveStatusRead>>();
const fetchNotificationRules = vi.fn<
  (organizationId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<NotificationRuleRead>>
>();
const createNotificationRule = vi.fn<
  (organizationId: string, body: unknown, signal?: AbortSignal) => Promise<NotificationRuleRead>
>();
const evaluateDataContract = vi.fn<(contractId: string, signal?: AbortSignal) => Promise<EvaluationResponse>>();
const fetchContractViolations = vi.fn<
  (contractId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<ViolationRead>>
>();
const fetchContractSlaStatus = vi.fn<
  (contractId: string, periodDays?: number, signal?: AbortSignal) => Promise<SlaStatusResponse>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchArchiveStatus: (signal?: AbortSignal) => fetchArchiveStatus(signal),
    fetchNotificationRules: (organizationId: string, query: unknown, signal?: AbortSignal) =>
      fetchNotificationRules(organizationId, query, signal),
    createNotificationRule: (organizationId: string, body: unknown, signal?: AbortSignal) =>
      createNotificationRule(organizationId, body, signal),
    evaluateDataContract: (contractId: string, signal?: AbortSignal) => evaluateDataContract(contractId, signal),
    fetchContractViolations: (contractId: string, query: unknown, signal?: AbortSignal) =>
      fetchContractViolations(contractId, query, signal),
    fetchContractSlaStatus: (contractId: string, periodDays?: number, signal?: AbortSignal) =>
      fetchContractSlaStatus(contractId, periodDays, signal),
  };
});

const ARCHIVE: ArchiveStatusRead = {
  total_archives: 12,
  total_events_archived: 48213,
  latest_archive_id: "arch_2026_09_02",
  latest_checksum: "abcdef0123456789abcdef0123456789",
  legal_hold_count: 1,
  status: "LEGAL_HOLD_ACTIVE",
};

const RULE: NotificationRuleRead = {
  id: "ntf_1", organization_id: "org1", name: "Contract breach — page on-call",
  conditions: { event_type: "contract.violated" }, channel: "ITSM",
  recipients: ["oncall@tenant.example"], escalation_after_minutes: 15, enabled: true,
  created_by: "local-ui-admin", created_at: "2026-07-10T00:00:00Z", updated_at: "2026-07-10T00:00:00Z",
};

const VIOLATION: ViolationRead = {
  id: "viol_1", organization_id: "org1", contract_id: "contract-abc",
  violation_type: "SCHEMA_DRIFT", severity: "CRITICAL",
  evidence: { column: "amount" }, detected_at: "2026-09-02T00:00:00Z",
  resolved_at: null, resolved_by: null,
  created_at: "2026-09-02T00:00:00Z", updated_at: "2026-09-02T00:00:00Z",
};

const EVALUATION: EvaluationResponse = {
  contract_id: "contract-abc",
  violations: [{ violation_type: "SCHEMA_DRIFT", severity: "CRITICAL", evidence: { column: "amount" }, detected_at: "2026-09-02T00:00:00Z" }],
  enforcement_action: "BLOCK",
  allowed: false,
  reason: "critical contract violation — query blocked pending remediation",
};

const SLA_STATUS: SlaStatusResponse = {
  contract_id: "contract-abc", compliant: false, uptime_percent: 96.2,
  violations_in_period: 1, breach_minutes: 45,
  period_start: "2026-08-03T00:00:00Z", period_end: "2026-09-02T00:00:00Z",
};

function pageOf<T>(items: T[]): PageOf<T> {
  return { items, limit: 200, offset: 0, total: items.length };
}

async function loadScreen() {
  const { ReliabilityScreen } = await import("./ReliabilityScreen");
  return ReliabilityScreen;
}

beforeEach(() => {
  fetchArchiveStatus.mockReset();
  fetchNotificationRules.mockReset();
  createNotificationRule.mockReset();
  evaluateDataContract.mockReset();
  fetchContractViolations.mockReset();
  fetchContractSlaStatus.mockReset();

  fetchArchiveStatus.mockResolvedValue(ARCHIVE);
  fetchNotificationRules.mockResolvedValue(pageOf([RULE]));
  evaluateDataContract.mockResolvedValue(EVALUATION);
  fetchContractSlaStatus.mockResolvedValue(SLA_STATUS);
  fetchContractViolations.mockResolvedValue(pageOf([VIOLATION]));

  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ReliabilityScreen against the real observability/notification/runtime-contracts endpoints", () => {
  it("loads archive tiles and the notification rule list on mount", async () => {
    const ReliabilityScreen = await loadScreen();
    render(<ReliabilityScreen />);

    await waitFor(() => expect(screen.getByText("legal hold active")).toBeInTheDocument());
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("48,213")).toBeInTheDocument();

    expect(screen.getByText("Contract breach — page on-call")).toBeInTheDocument();
    expect(screen.getByText("oncall@tenant.example")).toBeInTheDocument();
  });

  it("no longer offers an SLO panel or a create-SLO form", async () => {
    /* R11-D10 retired the SLO surface: `slo_measurement` never had a writer and
       no indicator source existed to build one from, so the budget could only
       ever answer NO_DATA. Pinned as an assertion rather than left as an
       absence, because the failure this row closed was a screen that looked
       like it supervised something. */
    const ReliabilityScreen = await loadScreen();
    render(<ReliabilityScreen />);
    await waitFor(() => expect(screen.getByText("legal hold active")).toBeInTheDocument());

    expect(screen.queryByRole("form", { name: "Create SLO" })).toBeNull();
    expect(screen.queryByRole("button", { name: "View budget" })).toBeNull();
    expect(screen.queryByText("SLOs")).toBeNull();
  });

  it("creating a notification rule parses JSON conditions and CSV recipients before posting", async () => {
    createNotificationRule.mockResolvedValue({
      ...RULE, id: "ntf_2", name: "Contract violation digest", channel: "EMAIL",
      recipients: ["a@tenant.example", "b@tenant.example"], escalation_after_minutes: null,
    });
    const ReliabilityScreen = await loadScreen();
    render(<ReliabilityScreen />);
    await waitFor(() => expect(screen.getByText("legal hold active")).toBeInTheDocument());

    const form = screen.getByRole("form", { name: "Create notification rule" });
    fireEvent.change(within(form).getByPlaceholderText("Contract breach — page on-call"), { target: { value: "Contract violation digest" } });
    fireEvent.change(within(form).getByLabelText("Channel"), { target: { value: "EMAIL" } });
    fireEvent.change(within(form).getByPlaceholderText("oncall@tenant.example, steward@tenant.example"), {
      target: { value: "a@tenant.example, b@tenant.example" },
    });
    fireEvent.change(within(form).getByLabelText("Conditions (JSON matcher)"), {
      target: { value: '{"event_type":"contract.violations_detected"}' },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Create rule" }));

    await waitFor(() =>
      expect(createNotificationRule).toHaveBeenCalledWith(
        DEFAULT_ORG_ID,
        {
          name: "Contract violation digest",
          conditions: { event_type: "contract.violations_detected" },
          channel: "EMAIL",
          recipients: ["a@tenant.example", "b@tenant.example"],
          escalation_after_minutes: null,
          enabled: true,
        },
        undefined,
      ),
    );
    await waitFor(() => expect(screen.getByText("Contract violation digest")).toBeInTheDocument());
  });

  it("rejects invalid JSON conditions client-side without calling the API", async () => {
    const ReliabilityScreen = await loadScreen();
    render(<ReliabilityScreen />);
    await waitFor(() => expect(screen.getByText("legal hold active")).toBeInTheDocument());

    const form = screen.getByRole("form", { name: "Create notification rule" });
    fireEvent.change(within(form).getByPlaceholderText("Contract breach — page on-call"), { target: { value: "Bad conditions rule" } });
    fireEvent.change(within(form).getByPlaceholderText("oncall@tenant.example, steward@tenant.example"), {
      target: { value: "a@tenant.example" },
    });
    fireEvent.change(within(form).getByLabelText("Conditions (JSON matcher)"), {
      target: { value: "{not json" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Create rule" }));

    await waitFor(() => expect(screen.getByText("Conditions must be valid JSON.")).toBeInTheDocument());
    expect(createNotificationRule).not.toHaveBeenCalled();
  });

  it("evaluating a contract id fires evaluate/violations/sla-status together and renders the combined evidence", async () => {
    const ReliabilityScreen = await loadScreen();
    render(<ReliabilityScreen />);
    await waitFor(() => expect(screen.getByText("legal hold active")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Contract ID"), {
      target: { value: "contract-abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Evaluate" }));

    await waitFor(() => expect(evaluateDataContract).toHaveBeenCalledWith("contract-abc", undefined));
    expect(fetchContractSlaStatus).toHaveBeenCalledWith("contract-abc", 30, undefined);
    expect(fetchContractViolations).toHaveBeenCalledWith("contract-abc", { limit: 100, offset: 0 }, undefined);

    await waitFor(() => expect(screen.getByText("BLOCK")).toBeInTheDocument());
    expect(screen.getByText("blocked")).toBeInTheDocument();
    expect(screen.getByText(/critical contract violation/)).toBeInTheDocument();
    expect(screen.getByText("96.20%")).toBeInTheDocument();
    expect(screen.getByText("schema drift")).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("contract")).toBe("contract-abc");
  });
});
