import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type { GlossaryLinkProposalRead } from "../lib/api";
import type { GlossaryLinkProposalGenerate, GovernanceReviewRead, MeRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import type { Session, SessionState } from "../lib/session";
import { resetLocationCacheForTests } from "../lib/location";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";
import { GlossaryLinkProposals } from "./GlossaryLinkProposals";

/* ---------------------------------------------------------------------------
   Glossary review -> Link proposals (R11-AUD08).

   The properties, and why each was a real way to get this screen wrong:

     1. WHO SEES WHAT. Every role the list admits reads proposals; only the four
        write roles are offered Generate and Submit. A session outside the read
        list is not asked, and nothing is asked while `/v1/me` is in flight.
     2. EVIDENCE AND CONFIDENCE AS THE API RETURNS THEM. The confidence is the
        number on the wire; the evidence is its own keys and values, with a
        sentence only where the keys it needs are present.
     3. NOTHING FIRES ON A CLICK, AND THE DIALOGS SAY WHERE IT GOES. Generate
        says it creates drafts and links nothing; Submit says a different
        reviewer decides and what approving and rejecting do.
     4. ONLY A DRAFT IS SUBMITTABLE. A row waiting for a reviewer, or decided,
        offers no Submit -- and says who decides it.
     5. A REFUSAL IS AN ANSWER, IN THE SERVER'S WORDS. The dialog stays open; a
        409 re-reads the list behind it.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

type Query = { status?: string | null; limit?: number; offset?: number };
const fetchGlossaryLinkProposals =
  vi.fn<(organizationId: string, query: Query, signal?: AbortSignal) => Promise<PageOf<GlossaryLinkProposalRead>>>();
const generateGlossaryLinkProposals =
  vi.fn<(organizationId: string, body: GlossaryLinkProposalGenerate, signal?: AbortSignal) => Promise<PageOf<GlossaryLinkProposalRead>>>();
const submitGlossaryLinkProposal =
  vi.fn<(proposalId: string, signal?: AbortSignal) => Promise<GovernanceReviewRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchGlossaryLinkProposals: (organizationId: string, query: Query, signal?: AbortSignal) =>
      fetchGlossaryLinkProposals(organizationId, query, signal),
    generateGlossaryLinkProposals: (organizationId: string, body: GlossaryLinkProposalGenerate, signal?: AbortSignal) =>
      generateGlossaryLinkProposals(organizationId, body, signal),
    submitGlossaryLinkProposal: (proposalId: string, signal?: AbortSignal) =>
      submitGlossaryLinkProposal(proposalId, signal),
  };
});

let sessionMe: MeRead | null = null;
let sessionState: SessionState = "connected";
vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  return {
    ...actual,
    useSession: (): Session => ({
      state: sessionState,
      me: sessionMe,
      lapsed: false,
      lastSuccessAt: null,
      error: null,
      dataMode: "live",
      authMode: "development",
      authModeInferred: false,
      reload: () => undefined,
    }),
  };
});

const asRoles = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});

const proposal = (overrides: Partial<GlossaryLinkProposalRead> = {}): GlossaryLinkProposalRead => ({
  id: "e1000000-0000-4000-8000-000000000001",
  organization_id: ORG,
  table_id: "f1000000-0000-4000-8000-000000000001",
  term_id: "a1000000-0000-4000-8000-000000000001",
  term_display_name: "Customer",
  table_name: "customers",
  source_annotation_id: "b1000000-0000-4000-8000-000000000001",
  confidence: 1,
  evidence: {
    strategy: "APPROVED_LABEL_EXACT_MATCH",
    matched_label: "Customer",
    term_label_kind: "DISPLAY_NAME",
    annotation_version: 2,
  },
  status: "DRAFT",
  governance_review_id: null,
  created_by: "priya.steward",
  reviewed_by: null,
  reviewed_at: null,
  created_at: "2026-09-19T08:00:00Z",
  updated_at: "2026-09-19T08:00:00Z",
  ...overrides,
});

const DRAFT = proposal();
const SYNONYM = proposal({
  id: "e1000000-0000-4000-8000-000000000002",
  table_id: "f1000000-0000-4000-8000-000000000002",
  table_name: "acct_master",
  term_display_name: "Account",
  confidence: 0.92,
  evidence: { strategy: "APPROVED_LABEL_EXACT_MATCH", matched_label: "account", term_label_kind: "SYNONYM", annotation_version: 1 },
});
const IN_REVIEW = proposal({
  id: "e1000000-0000-4000-8000-000000000003",
  table_name: "card_transactions",
  term_display_name: "Card transaction",
  status: "REVIEW_REQUIRED",
  governance_review_id: "9e000000-0000-4000-8000-000000000001",
  created_by: "agent:steward",
});
const APPROVED = proposal({
  id: "e1000000-0000-4000-8000-000000000004",
  table_name: "loan_applications",
  term_display_name: "Loan application",
  status: "APPROVED",
  governance_review_id: "9e000000-0000-4000-8000-000000000002",
  reviewed_by: "riya.reviewer",
  reviewed_at: "2026-09-11T16:45:00Z",
});
const REJECTED = proposal({
  id: "e1000000-0000-4000-8000-000000000005",
  table_name: "stg_ledger",
  term_display_name: "General ledger",
  confidence: 0.92,
  status: "REJECTED",
  governance_review_id: "9e000000-0000-4000-8000-000000000003",
  reviewed_by: "riya.reviewer",
  reviewed_at: "2026-09-11T16:50:00Z",
});
const UNFAMILIAR = proposal({
  id: "e1000000-0000-4000-8000-000000000006",
  table_name: "trades",
  term_display_name: "Trade",
  confidence: 0.81,
  // The keys the exact-match sentence needs are all here; only the strategy differs, so the
  // sentence is withheld for the strategy and not for a missing key.
  evidence: { strategy: "EMBEDDING_NEIGHBOUR", matched_label: "trade", term_label_kind: "SYNONYM", distance: 0.19, model: "e5" },
});
const NO_EVIDENCE = proposal({ id: "e1000000-0000-4000-8000-000000000007", table_name: "orphans", term_display_name: "Orphan", evidence: {} });

const page = (items: GlossaryLinkProposalRead[], extra: Partial<PageOf<GlossaryLinkProposalRead>> = {}): PageOf<GlossaryLinkProposalRead> => ({
  items, limit: 25, offset: 0, total: items.length, ...extra,
});

const REVIEW: GovernanceReviewRead = {
  id: "9e000000-0000-4000-8000-000000000009", organization_id: ORG, object_type: "GLOSSARY_LINK_PROPOSAL",
  object_id: DRAFT.id, requested_action: "APPROVE_LINK", status: "PENDING", requested_by: "someone",
  decided_by: null, decision_reason: null, decided_at: null,
  created_at: "2026-09-21T08:00:00Z", updated_at: "2026-09-21T08:00:00Z",
};

const WRITE_ROLES = ["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"];
const READ_ONLY_ROLES = ["Analyst", "Auditor", "DataAdmin", "Reviewer", "Viewer"];
const OUTSIDE_ROLES = ["AgentDeveloper", "ToolDeveloper", "Operations", "OrganizationAdmin", "MetadataReviewer"];

const generateButton = () => screen.queryByRole("button", { name: "Generate proposals" });
const submitButtons = () => screen.queryAllByRole("button", { name: /^Submit .* for review$/ });
const itemFor = (title: string) =>
  within(screen.getByRole("list", { name: "Term-link proposals" }))
    .getAllByRole("listitem")
    .find((item) => item.textContent?.includes(title))!;

function mount(url = "/#/steward/glossary-review") {
  history.replaceState(null, "", url);
  resetLocationCacheForTests();
  return render(<GlossaryLinkProposals />);
}

beforeEach(() => {
  fetchGlossaryLinkProposals.mockReset();
  fetchGlossaryLinkProposals.mockResolvedValue(page([DRAFT, SYNONYM, IN_REVIEW, APPROVED, REJECTED]));
  generateGlossaryLinkProposals.mockReset();
  submitGlossaryLinkProposal.mockReset();
  sessionMe = asRoles("DataSteward");
  sessionState = "connected";
  history.replaceState(null, "", "/");
  resetLocationCacheForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Link proposals: who sees what", () => {
  it.each(READ_ONLY_ROLES)("shows the list to %s and offers no way to change it", async (role) => {
    sessionMe = asRoles(role);
    mount();

    expect(await screen.findByRole("list", { name: "Term-link proposals" })).toBeInTheDocument();
    expect(fetchGlossaryLinkProposals).toHaveBeenCalledWith(ORG, { status: null, limit: 25, offset: 0 }, expect.any(AbortSignal));
    expect(generateButton()).not.toBeInTheDocument();
    expect(submitButtons()).toHaveLength(0);
    expect(screen.getByText(/You can read proposals\. Generating and submitting them is for sessions holding DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin\./)).toBeInTheDocument();
  });

  it.each(WRITE_ROLES)("offers %s Generate, and Submit on a draft only", async (role) => {
    sessionMe = asRoles(role);
    mount();

    expect(await screen.findByRole("list", { name: "Term-link proposals" })).toBeInTheDocument();
    expect(generateButton()).toBeEnabled();
    // Two drafts in the fixture; the one in review and the two decided offer none.
    expect(submitButtons().map((button) => button.textContent)).toEqual([
      "Submit customers → Customer for review",
      "Submit acct_master → Account for review",
    ]);
    expect(screen.queryByText(/You can read proposals/)).not.toBeInTheDocument();
  });

  it.each(OUTSIDE_ROLES)("asks for nothing as %s, and says it does not apply rather than showing an error", async (role) => {
    sessionMe = asRoles(role);
    mount();

    expect(await screen.findByText(/Not applicable to your roles/)).toBeInTheDocument();
    expect(fetchGlossaryLinkProposals).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(generateButton()).not.toBeInTheDocument();
  });

  it("holds the read while identity is in flight, and offers no control until it has answered", async () => {
    sessionState = "connecting";
    sessionMe = null;
    mount();

    expect(await screen.findByText(/Loading link proposals/)).toBeInTheDocument();
    expect(fetchGlossaryLinkProposals).not.toHaveBeenCalled();
    expect(generateButton()).not.toBeInTheDocument();
    expect(screen.queryByText(/You can read proposals/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Not applicable to your roles/)).not.toBeInTheDocument();
  });

  it("never sends the read when identity then says the session may not read it", async () => {
    sessionState = "connecting";
    sessionMe = null;
    const view = mount();
    await screen.findByText(/Loading link proposals/);

    sessionState = "connected";
    sessionMe = asRoles("ToolDeveloper");
    view.rerender(<GlossaryLinkProposals />);

    expect(await screen.findByText(/Not applicable to your roles/)).toBeInTheDocument();
    expect(fetchGlossaryLinkProposals).not.toHaveBeenCalled();
  });

  it("sends the read once identity says the session may, and offers the controls it is admitted to", async () => {
    sessionState = "connecting";
    sessionMe = null;
    const view = mount();
    expect(fetchGlossaryLinkProposals).not.toHaveBeenCalled();

    sessionState = "connected";
    sessionMe = asRoles("PlatformAdmin");
    view.rerender(<GlossaryLinkProposals />);

    expect(await screen.findByRole("list", { name: "Term-link proposals" })).toBeInTheDocument();
    expect(fetchGlossaryLinkProposals).toHaveBeenCalledTimes(1);
    expect(generateButton()).toBeEnabled();
  });

  it("still reads when identity will not answer: the server stays the authority, and no write is offered", async () => {
    sessionState = "disconnected";
    sessionMe = null;
    mount();

    expect(await screen.findByRole("list", { name: "Term-link proposals" })).toBeInTheDocument();
    expect(fetchGlossaryLinkProposals).toHaveBeenCalledTimes(1);
    expect(generateButton()).not.toBeInTheDocument();
    expect(submitButtons()).toHaveLength(0);
  });
});

describe("Link proposals: the list", () => {
  it("names the table and the term, the status, the confidence, and who proposed it", async () => {
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });

    const first = itemFor("customers → Customer");
    expect(first).toHaveTextContent("draft");
    expect(first).toHaveTextContent("confidence 1.00");
    expect(first).toHaveTextContent("Proposed by priya.steward at 2026-09-19 08:00 UTC");
    expect(itemFor("acct_master → Account")).toHaveTextContent("confidence 0.92");
    // A proposal the steward agent made is attributed to the agent, not to a person.
    expect(itemFor("card_transactions → Card transaction")).toHaveTextContent("Proposed by agent:steward");
    expect(screen.getByText("1–5 of 5 proposals")).toBeInTheDocument();
  });

  it("shows the evidence as the API returned it, with a sentence where the keys allow one", async () => {
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });

    const evidence = within(itemFor("customers → Customer")).getByRole("group", { name: "Evidence for customers → Customer" });
    expect(evidence).toHaveTextContent(
      "An approved business annotation on customers (version 2) says “Customer”, which equals the Customer term's display name.",
    );
    expect(evidence).toHaveTextContent("Strategy");
    expect(evidence).toHaveTextContent("APPROVED_LABEL_EXACT_MATCH");
    expect(evidence).toHaveTextContent("Matched label");
    expect(evidence).toHaveTextContent("Term label kind");
    expect(evidence).toHaveTextContent("DISPLAY_NAME");
    expect(evidence).toHaveTextContent("Annotation version");
    const synonym = within(itemFor("acct_master → Account")).getByRole("group", { name: /Evidence for/ });
    expect(synonym).toHaveTextContent("which equals the Account term's synonym.");
  });

  it("writes no sentence for a strategy it does not know, and still shows every key it was given", async () => {
    fetchGlossaryLinkProposals.mockResolvedValue(page([UNFAMILIAR]));
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });

    const evidence = within(itemFor("trades → Trade")).getByRole("group", { name: /Evidence for/ });
    expect(evidence).not.toHaveTextContent("An approved business annotation");
    expect(evidence).toHaveTextContent("Strategy");
    expect(evidence).toHaveTextContent("EMBEDDING_NEIGHBOUR");
    expect(evidence).toHaveTextContent("Distance");
    expect(evidence).toHaveTextContent("0.19");
    expect(evidence).toHaveTextContent("Model");
    expect(evidence).toHaveTextContent("e5");
    expect(itemFor("trades → Trade")).toHaveTextContent("confidence 0.81");
  });

  it("says when there is no evidence, rather than leaving a blank", async () => {
    fetchGlossaryLinkProposals.mockResolvedValue(page([NO_EVIDENCE]));
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });

    expect(itemFor("orphans → Orphan")).toHaveTextContent("The API returned no evidence for this proposal.");
  });

  it("opens the table in the Catalog and the term in Business meaning", async () => {
    // A role the Catalog list admits (`CATALOG_ROWS_ROLES`); a DataSteward is not one -- below.
    sessionMe = asRoles("MetadataAdmin");
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });
    const row = within(itemFor("customers → Customer"));

    fireEvent.click(row.getByRole("button", { name: "Open table customers" }));
    expect(location.hash).toBe("#/analyst/catalog");
    expect(new URLSearchParams(location.search).get("asset")).toBe(DRAFT.table_id);

    fireEvent.click(row.getByRole("button", { name: "Open term Customer" }));
    expect(location.hash).toBe("#/steward/meaning");
    expect(new URLSearchParams(location.search).get("q")).toBe("Customer");
    expect(new URLSearchParams(location.search).get("view")).toBe("glossary");
  });

  it.each(["DataSteward", "SemanticAdmin", "Auditor", "DataAdmin", "Reviewer"])(
    "does not send %s to a Catalog that would refuse them, and still offers the term",
    async (role) => {
      sessionMe = asRoles(role);
      mount();
      await screen.findByRole("list", { name: "Term-link proposals" });
      const row = within(itemFor("customers → Customer"));

      expect(row.queryByRole("button", { name: "Open table customers" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^Open table / })).not.toBeInTheDocument();
      expect(row.getByRole("button", { name: "Open term Customer" })).toBeInTheDocument();
    },
  );

  it.each(["Analyst", "PlatformAdmin", "Viewer"])("offers %s the table's Catalog link", async (role) => {
    sessionMe = asRoles(role);
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });

    expect(within(itemFor("customers → Customer")).getByRole("button", { name: "Open table customers" })).toBeInTheDocument();
  });

  it("says a proposal in review is waiting on a different reviewer, and links to that review for a role the queue admits", async () => {
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });
    const row = itemFor("card_transactions → Card transaction");

    expect(row).toHaveTextContent("Waiting for a decision in the Review queue (review 9e000000).");
    expect(row).toHaveTextContent("A different reviewer approves or rejects it; nobody can decide a review they opened.");
    fireEvent.click(within(row).getByRole("button", { name: "Open the review" }));
    expect(location.hash).toBe("#/reviewer/governance");
    expect(new URLSearchParams(location.search).get("review")).toBe("9e000000-0000-4000-8000-000000000001");
  });

  it("does not send a MetadataAdmin to a review queue that would refuse them", async () => {
    sessionMe = asRoles("MetadataAdmin");
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });

    expect(itemFor("card_transactions → Card transaction")).toHaveTextContent("Waiting for a decision in the Review queue");
    expect(screen.queryByRole("button", { name: "Open the review" })).not.toBeInTheDocument();
  });

  it("says what an approval and a rejection each did, and who decided", async () => {
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });

    const approved = itemFor("loan_applications → Loan application");
    expect(approved).toHaveTextContent("Approved by riya.reviewer at 2026-09-11 16:45 UTC. The table and the term are linked (an inferred link, at confidence 1.00).");
    const rejected = itemFor("stg_ledger → General ledger");
    expect(rejected).toHaveTextContent("Rejected by riya.reviewer at 2026-09-11 16:50 UTC.");
    expect(rejected).toHaveTextContent("No link was made, and this match is not proposed again.");
    // Neither offers another Submit.
    expect(within(approved).queryByRole("button", { name: /Submit/ })).not.toBeInTheDocument();
    expect(within(rejected).queryByRole("button", { name: /Submit/ })).not.toBeInTheDocument();
  });

  it("filters by status on the server, in the URL, and reads an unknown status as every status", async () => {
    const first = mount();
    await screen.findByRole("list", { name: "Term-link proposals" });

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "DRAFT" } });

    await waitFor(() =>
      expect(fetchGlossaryLinkProposals).toHaveBeenLastCalledWith(ORG, { status: "DRAFT", limit: 25, offset: 0 }, expect.any(AbortSignal)),
    );
    expect(new URLSearchParams(location.search).get("status")).toBe("DRAFT");
    expect(within(screen.getByLabelText("Status")).getAllByRole("option").map((option) => (option as HTMLOptionElement).value)).toEqual([
      "", "DRAFT", "REVIEW_REQUIRED", "APPROVED", "REJECTED",
    ]);

    first.unmount();
    fetchGlossaryLinkProposals.mockClear();
    mount("/?view=proposals&status=NOPE#/steward/glossary-review");
    await screen.findByRole("list", { name: "Term-link proposals" });
    expect(fetchGlossaryLinkProposals).toHaveBeenLastCalledWith(ORG, { status: null, limit: 25, offset: 0 }, expect.any(AbortSignal));
  });

  it("pages by the API's total", async () => {
    fetchGlossaryLinkProposals.mockImplementation(async (_org, query) => {
      const offset = query.offset ?? 0;
      const rows = Array.from({ length: Math.min(25, 30 - offset) }, (_, i) =>
        proposal({ id: `e-${offset + i}`, table_name: `table_${offset + i}` }),
      );
      return { items: rows, limit: 25, offset, total: 30 };
    });
    mount();

    expect(await screen.findByText("1–25 of 30 proposals")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("26–30 of 30 proposals")).toBeInTheDocument();
    expect(fetchGlossaryLinkProposals).toHaveBeenLastCalledWith(ORG, { status: null, limit: 25, offset: 25 }, expect.any(AbortSignal));
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    expect(await screen.findByText("1–25 of 30 proposals")).toBeInTheDocument();
  });

  it("says there are no proposals, and how to make some, to a steward; and just that, to a reader", async () => {
    fetchGlossaryLinkProposals.mockResolvedValue(page([]));
    const steward = mount();
    expect(await screen.findByText("No link proposals")).toBeInTheDocument();
    expect(screen.getByText(/Generate proposals to look for tables whose approved business names match/)).toBeInTheDocument();
    steward.unmount();

    sessionMe = asRoles("Viewer");
    mount();
    expect(await screen.findByText("No link proposals")).toBeInTheDocument();
    expect(screen.getByText("No link proposal has been generated in this organization.")).toBeInTheDocument();
  });

  it("says a filter matched nothing, rather than that there are no proposals", async () => {
    fetchGlossaryLinkProposals.mockResolvedValue(page([]));
    mount("/?view=proposals&status=REJECTED#/steward/glossary-review");

    expect(await screen.findByText("No proposals with status rejected")).toBeInTheDocument();
    expect(screen.queryByText("No link proposals")).not.toBeInTheDocument();
  });

  it("says the list could not be loaded, with the server's words, and retries", async () => {
    fetchGlossaryLinkProposals.mockRejectedValueOnce(new ApiError(503, "database unavailable"));
    mount();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Link proposals could not be loaded");
    expect(alert).toHaveTextContent("database unavailable");
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("list", { name: "Term-link proposals" })).toBeInTheDocument();
    expect(fetchGlossaryLinkProposals).toHaveBeenCalledTimes(2);
  });
});

describe("Link proposals: generating", () => {
  async function openGenerate() {
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });
    fireEvent.click(screen.getByRole("button", { name: "Generate proposals" }));
    return await screen.findByRole("dialog", { name: "Generate link proposals?" });
  }
  const confirm = (dialog: HTMLElement) => within(dialog).getByRole("button", { name: "Generate proposals" });

  it("states the effect before it does anything: drafts only, nothing linked, attributed to the caller", async () => {
    const dialog = await openGenerate();

    expect(dialog).toHaveTextContent("Matches each approved business annotation's name and synonyms, exactly and ignoring case");
    expect(dialog).toHaveTextContent("creates a DRAFT proposal for each pair that is not already linked or proposed before, in any status");
    expect(dialog).toHaveTextContent("so a rejected link is not proposed again");
    expect(dialog).toHaveTextContent("The proposals are attributed to you.");
    expect(dialog).toHaveTextContent("Nothing is linked: each must be submitted for review, and a different reviewer must approve it.");
    // What the two confidence numbers mean, so a minimum is chosen knowingly.
    expect(dialog).toHaveTextContent("Confidence is 1.00 when the annotation’s business name equals the term’s display name and 0.92 for every other exact match");
    expect(within(dialog).getByLabelText("Minimum confidence")).toHaveValue(0.75);
    expect(within(dialog).getByLabelText("Most proposals to create")).toHaveValue(200);
    expect(generateGlossaryLinkProposals).not.toHaveBeenCalled();
  });

  it("sends the API's own defaults untouched", async () => {
    generateGlossaryLinkProposals.mockResolvedValue(page([DRAFT, SYNONYM]));
    const dialog = await openGenerate();

    fireEvent.click(confirm(dialog));

    await waitFor(() => expect(generateGlossaryLinkProposals).toHaveBeenCalledTimes(1));
    expect(generateGlossaryLinkProposals).toHaveBeenCalledWith(ORG, { minimum_confidence: 0.75, limit: 200 }, undefined);
  });

  it("sends the bounds the steward chose, says what was created, and re-reads the list", async () => {
    generateGlossaryLinkProposals.mockResolvedValue(page([DRAFT, SYNONYM], { limit: 50 }));
    const dialog = await openGenerate();

    fireEvent.change(within(dialog).getByLabelText("Minimum confidence"), { target: { value: "0.93" } });
    fireEvent.change(within(dialog).getByLabelText("Most proposals to create"), { target: { value: "50" } });
    fireEvent.click(confirm(dialog));

    await waitFor(() =>
      expect(generateGlossaryLinkProposals).toHaveBeenCalledWith(ORG, { minimum_confidence: 0.93, limit: 50 }, undefined),
    );
    expect(await screen.findByText(/^Generated 2 draft proposals\. Nothing is linked yet: each must be submitted and approved by a different reviewer\.$/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetchGlossaryLinkProposals).toHaveBeenCalledTimes(2);
  });

  it("says one proposal in the singular, and no new matches as no new matches", async () => {
    generateGlossaryLinkProposals.mockResolvedValueOnce(page([DRAFT])).mockResolvedValueOnce(page([]));
    let dialog = await openGenerate();
    fireEvent.click(confirm(dialog));
    expect(await screen.findByText(/^Generated 1 draft proposal\./)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Generate proposals" }));
    dialog = await screen.findByRole("dialog", { name: "Generate link proposals?" });
    fireEvent.click(confirm(dialog));
    expect(await screen.findByText(/^Generation found no new matches: every match is already linked or was proposed before, or none reaches the minimum confidence\.$/)).toBeInTheDocument();
  });

  it("says when a run stopped at the limit the steward set", async () => {
    generateGlossaryLinkProposals.mockResolvedValue(page(Array.from({ length: 3 }, (_, i) => proposal({ id: `e-${i}` }))));
    const dialog = await openGenerate();
    fireEvent.change(within(dialog).getByLabelText("Most proposals to create"), { target: { value: "3" } });

    fireEvent.click(confirm(dialog));

    expect(await screen.findByText(/It stopped at its limit of 3: run it again for more\./)).toBeInTheDocument();
  });

  it.each([
    ["a confidence below the API's minimum", "Minimum confidence", "0.49"],
    ["a confidence above one", "Minimum confidence", "1.01"],
    ["a confidence that is not a number", "Minimum confidence", ""],
    ["a limit of zero", "Most proposals to create", "0"],
    ["a limit above the API's maximum", "Most proposals to create", "501"],
    ["a limit that is not whole", "Most proposals to create", "2.5"],
    ["an empty limit", "Most proposals to create", ""],
  ])("will not confirm %s", async (_name, label, value) => {
    const dialog = await openGenerate();

    fireEvent.change(within(dialog).getByLabelText(label), { target: { value } });

    expect(confirm(dialog)).toBeDisabled();
    expect(within(dialog).getByLabelText(label)).toHaveAttribute("aria-invalid", "true");
    expect(generateGlossaryLinkProposals).not.toHaveBeenCalled();
  });

  it.each([["0.5", "1"], ["1", "500"], ["0.92", "1"]])("confirms the API's own edges: confidence %s, limit %s", async (confidence, limit) => {
    const dialog = await openGenerate();

    fireEvent.change(within(dialog).getByLabelText("Minimum confidence"), { target: { value: confidence } });
    fireEvent.change(within(dialog).getByLabelText("Most proposals to create"), { target: { value: limit } });

    expect(confirm(dialog)).toBeEnabled();
  });

  it("keeps the dialog open and shows a refusal in the server's own words, claiming nothing", async () => {
    generateGlossaryLinkProposals.mockRejectedValue(new ApiError(403, "requires DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin"));
    const dialog = await openGenerate();

    fireEvent.click(confirm(dialog));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^requires DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByText(/Generated/)).not.toBeInTheDocument();
    expect(fetchGlossaryLinkProposals).toHaveBeenCalledTimes(1);
  });

  it("shows the server's validation sentence for a bound it refused", async () => {
    generateGlossaryLinkProposals.mockRejectedValue(new ApiError(422, "body.minimum_confidence: Input should be greater than or equal to 0.5"));
    const dialog = await openGenerate();

    fireEvent.click(confirm(dialog));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("body.minimum_confidence: Input should be greater than or equal to 0.5");
  });

  it("admits one run at a time and shows the dialog busy meanwhile", async () => {
    let settle: (result: PageOf<GlossaryLinkProposalRead>) => void = () => undefined;
    generateGlossaryLinkProposals.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const dialog = await openGenerate();

    fireEvent.click(confirm(dialog));
    const working = await within(dialog).findByRole("button", { name: "Working…" });
    expect(working).toBeDisabled();
    fireEvent.click(working);
    expect(generateGlossaryLinkProposals).toHaveBeenCalledTimes(1);
    settle(page([]));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("cancels without sending anything", async () => {
    const dialog = await openGenerate();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(generateGlossaryLinkProposals).not.toHaveBeenCalled();
  });
});

describe("Link proposals: submitting for review", () => {
  async function openSubmit(title = "customers → Customer") {
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });
    fireEvent.click(screen.getByRole("button", { name: `Submit ${title} for review` }));
    return await screen.findByRole("dialog", { name: "Submit this proposal for review?" });
  }
  const confirm = (dialog: HTMLElement) => within(dialog).getByRole("button", { name: "Submit for review" });

  it("says where it goes and who decides it, and what each outcome does, and sends nothing on the click", async () => {
    const dialog = await openSubmit();

    expect(dialog).toHaveTextContent("customers → Customer: this moves the proposal from draft to review and opens a governance review in the Review queue.");
    expect(dialog).toHaveTextContent("A different reviewer approves or rejects it, and you cannot decide your own submission.");
    expect(dialog).toHaveTextContent("Approving links the table to the term (an inferred link, at confidence 1.00)");
    expect(dialog).toHaveTextContent("rejecting closes the proposal, and the same match is not proposed again.");
    expect(submitGlossaryLinkProposal).not.toHaveBeenCalled();
  });

  it("names the confidence of the proposal it is about", async () => {
    const dialog = await openSubmit("acct_master → Account");

    expect(dialog).toHaveTextContent("at confidence 0.92");
  });

  it("submits that proposal once, says where it went, links to the review, and re-reads the list", async () => {
    submitGlossaryLinkProposal.mockResolvedValue(REVIEW);
    const dialog = await openSubmit();

    fireEvent.click(confirm(dialog));

    await waitFor(() => expect(submitGlossaryLinkProposal).toHaveBeenCalledTimes(1));
    expect(submitGlossaryLinkProposal).toHaveBeenCalledWith(DRAFT.id, undefined);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const notice = await screen.findByText(/^customers → Customer was submitted\. A governance review is waiting in the Review queue; someone other than you approves or rejects it/);
    expect(notice).toHaveTextContent("Approving links the table to the term; rejecting closes the proposal for good.");
    expect(fetchGlossaryLinkProposals).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Open the Review queue" }));
    expect(location.hash).toBe("#/reviewer/governance");
    expect(new URLSearchParams(location.search).get("review")).toBe(REVIEW.id);
  });

  it("does not offer a MetadataAdmin the review queue, which would refuse them", async () => {
    sessionMe = asRoles("MetadataAdmin");
    submitGlossaryLinkProposal.mockResolvedValue(REVIEW);
    const dialog = await openSubmit();

    fireEvent.click(confirm(dialog));

    expect(await screen.findByText(/was submitted\. A governance review is waiting/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open the Review queue" })).not.toBeInTheDocument();
  });

  it("keeps the dialog open on a refusal and shows it in the server's own words", async () => {
    submitGlossaryLinkProposal.mockRejectedValue(new ApiError(403, "requires DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin"));
    const dialog = await openSubmit();

    fireEvent.click(confirm(dialog));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^requires DataSteward, MetadataAdmin, PlatformAdmin or SemanticAdmin$/);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByText(/was submitted/)).not.toBeInTheDocument();
    // A 403 changes nothing on the server, so the list is not re-read on its account.
    expect(fetchGlossaryLinkProposals).toHaveBeenCalledTimes(1);
  });

  it("shows the server's 409 verbatim and re-reads, because somebody submitted it first", async () => {
    submitGlossaryLinkProposal.mockRejectedValue(new ApiError(409, "only draft link proposals can be submitted"));
    const dialog = await openSubmit();
    fetchGlossaryLinkProposals.mockResolvedValue(page([{ ...DRAFT, status: "REVIEW_REQUIRED", governance_review_id: REVIEW.id }, SYNONYM]));

    fireEvent.click(confirm(dialog));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^only draft link proposals can be submitted$/);
    await waitFor(() => expect(fetchGlossaryLinkProposals).toHaveBeenCalledTimes(2));
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // The list behind it is now the truth: that one is in review, and offers no Submit.
    await waitFor(() => expect(itemFor("customers → Customer")).toHaveTextContent("Waiting for a decision in the Review queue"));
    expect(submitButtons().map((button) => button.textContent)).toEqual(["Submit acct_master → Account for review"]);
  });

  it("admits one submission at a time", async () => {
    let settle: (review: GovernanceReviewRead) => void = () => undefined;
    submitGlossaryLinkProposal.mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const dialog = await openSubmit();

    fireEvent.click(confirm(dialog));
    const working = await within(dialog).findByRole("button", { name: "Working…" });
    fireEvent.click(working);
    expect(submitGlossaryLinkProposal).toHaveBeenCalledTimes(1);
    settle(REVIEW);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("cancels without sending anything", async () => {
    const dialog = await openSubmit();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(submitGlossaryLinkProposal).not.toHaveBeenCalled();
  });
});

describe("Link proposals: accessibility", () => {
  it("has no WCAG A/AA violations with every status on screen, and names every control", async () => {
    fetchGlossaryLinkProposals.mockResolvedValue(page([DRAFT, SYNONYM, IN_REVIEW, APPROVED, REJECTED, UNFAMILIAR, NO_EVIDENCE]));
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });

    await expectNoAxeViolations(document.body);
    expect(
      unnamedFocusableElements(document.body).map((element) => `${element.tagName.toLowerCase()}.${(element as HTMLElement).className}`),
    ).toEqual([]);
  });

  it("has no violations in the not-applicable and empty states", async () => {
    sessionMe = asRoles("AgentDeveloper");
    const outside = mount();
    await screen.findByText(/Not applicable to your roles/);
    await expectNoAxeViolations(outside.container);
    outside.unmount();

    sessionMe = asRoles("Viewer");
    fetchGlossaryLinkProposals.mockResolvedValue(page([]));
    const empty = mount();
    await screen.findByText("No link proposals");
    await expectNoAxeViolations(empty.container);
  });

  it("has no violations with each dialog open", async () => {
    mount();
    await screen.findByRole("list", { name: "Term-link proposals" });

    fireEvent.click(screen.getByRole("button", { name: "Generate proposals" }));
    await screen.findByRole("dialog", { name: "Generate link proposals?" });
    fireEvent.change(screen.getByLabelText("Minimum confidence"), { target: { value: "0.2" } });
    await expectNoAxeViolations(document.body);
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Submit customers → Customer for review" }));
    await screen.findByRole("dialog", { name: "Submit this proposal for review?" });
    await expectNoAxeViolations(document.body);
  });
});
