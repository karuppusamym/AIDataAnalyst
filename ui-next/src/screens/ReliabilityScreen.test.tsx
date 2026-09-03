import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type {
  ArchiveStatusRead,
  EvaluationResponse,
  NotificationRuleRead,
  SlaStatusResponse,
  SloBudgetRead,
  SloDefinitionRead,
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
const fetchSloDefinitions = vi.fn<
  (organizationId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<SloDefinitionRead>>
>();
const createSloDefinition = vi.fn<
  (organizationId: string, body: unknown, signal?: AbortSignal) => Promise<SloDefinitionRead>
>();
const fetchSloBudget = vi.fn<(sloId: string, signal?: AbortSignal) => Promise<SloBudgetRead>>();
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
    fetchSloDefinitions: (organizationId: string, query: unknown, signal?: AbortSignal) =>
      fetchSloDefinitions(organizationId, query, signal),
    createSloDefinition: (organizationId: string, body: unknown, signal?: AbortSignal) =>
      createSloDefinition(organizationId, body, signal),
    fetchSloBudget: (sloId: string, signal?: AbortSignal) => fetchSloBudget(sloId, signal),
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

const SLO: SloDefinitionRead = {
  id: "slo_1", organization_id: "org1", slo_key: "agent-answer-latency-p95",
  name: "Agent answer latency (p95)", target: 99, window_days: 30, threshold: 95,
  status: "ACTIVE", created_by: "local-ui-admin",
  created_at: "2026-07-01T00:00:00Z", updated_at: "2026-08-15T00:00:00Z",
};

const BUDGET: SloBudgetRead = {
  slo_id: "slo_1", slo_key: "agent-answer-latency-p95", name: "Agent answer latency (p95)",
  target: 99, current_value: 99.4, budget_remaining: 0.62, window_days: 30, status: "HEALTHY",
};

const RULE: NotificationRuleRead = {
  id: "ntf_1", organization_id: "org1", name: "SLO breach — page on-call",
  conditions: { event_type: "slo.breached" }, channel: "ITSM",
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
  fetchSloDefinitions.mockReset();
  createSloDefinition.mockReset();
  fetchSloBudget.mockReset();
  fetchNotificationRules.mockReset();
  createNotificationRule.mockReset();
  evaluateDataContract.mockReset();
  fetchContractViolations.mockReset();
  fetchContractSlaStatus.mockReset();

  fetchArchiveStatus.mockResolvedValue(ARCHIVE);
  fetchSloDefinitions.mockResolvedValue(pageOf([SLO]));
  fetchNotificationRules.mockResolvedValue(pageOf([RULE]));
  fetchSloBudget.mockResolvedValue(BUDGET);
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
  it("loads archive tiles, the SLO list, and the notification rule list on mount", async () => {
    const ReliabilityScreen = await loadScreen();
    render(<ReliabilityScreen />);

    await waitFor(() => expect(screen.getByText("Agent answer latency (p95)")).toBeInTheDocument());
    expect(screen.getByText("legal hold active")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("48,213")).toBeInTheDocument();

    expect(screen.getByText("SLO breach — page on-call")).toBeInTheDocument();
    expect(screen.getByText("oncall@tenant.example")).toBeInTheDocument();
  });

  it("viewing an SLO's budget fetches it by id and renders its live status", async () => {
    const ReliabilityScreen = await loadScreen();
    render(<ReliabilityScreen />);
    await waitFor(() => expect(screen.getByText("Agent answer latency (p95)")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "View budget" }));

    await waitFor(() => expect(fetchSloBudget).toHaveBeenCalledWith("slo_1", undefined));
    await waitFor(() => expect(screen.getByText("healthy")).toBeInTheDocument());
    expect(screen.getByText("99.40%")).toBeInTheDocument();
  });

  it("creating an SLO posts the typed body and prepends the result to the list", async () => {
    createSloDefinition.mockResolvedValue({
      id: "slo_2", organization_id: "org1", slo_key: "ingestion-freshness",
      name: "Metadata ingestion freshness", target: 99.5, window_days: 7, threshold: 97,
      status: "ACTIVE", created_by: "local-ui-admin",
      created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
    });
    const ReliabilityScreen = await loadScreen();
    render(<ReliabilityScreen />);
    await waitFor(() => expect(screen.getByText("Agent answer latency (p95)")).toBeInTheDocument());

    const form = screen.getByRole("form", { name: "Create SLO" });
    fireEvent.change(within(form).getByPlaceholderText("agent-answer-latency-p95"), { target: { value: "ingestion-freshness" } });
    fireEvent.change(within(form).getByPlaceholderText("Agent answer latency (p95)"), { target: { value: "Metadata ingestion freshness" } });
    fireEvent.change(within(form).getByLabelText("Target %"), { target: { value: "99.5" } });
    fireEvent.change(within(form).getByLabelText("Threshold %"), { target: { value: "97" } });
    fireEvent.change(within(form).getByLabelText("Window (days)"), { target: { value: "7" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create SLO" }));

    await waitFor(() =>
      expect(createSloDefinition).toHaveBeenCalledWith(
        DEFAULT_ORG_ID,
        { slo_key: "ingestion-freshness", name: "Metadata ingestion freshness", target: 99.5, window_days: 7, threshold: 97 },
        undefined,
      ),
    );
    await waitFor(() => expect(screen.getByText("Metadata ingestion freshness")).toBeInTheDocument());
  });

  it("creating a notification rule parses JSON conditions and CSV recipients before posting", async () => {
    createNotificationRule.mockResolvedValue({
      ...RULE, id: "ntf_2", name: "Contract violation digest", channel: "EMAIL",
      recipients: ["a@tenant.example", "b@tenant.example"], escalation_after_minutes: null,
    });
    const ReliabilityScreen = await loadScreen();
    render(<ReliabilityScreen />);
    await waitFor(() => expect(screen.getByText("Agent answer latency (p95)")).toBeInTheDocument());

    const form = screen.getByRole("form", { name: "Create notification rule" });
    fireEvent.change(within(form).getByPlaceholderText("SLO breach — page on-call"), { target: { value: "Contract violation digest" } });
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
    await waitFor(() => expect(screen.getByText("Agent answer latency (p95)")).toBeInTheDocument());

    const form = screen.getByRole("form", { name: "Create notification rule" });
    fireEvent.change(within(form).getByPlaceholderText("SLO breach — page on-call"), { target: { value: "Bad conditions rule" } });
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
    await waitFor(() => expect(screen.getByText("Agent answer latency (p95)")).toBeInTheDocument());

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
