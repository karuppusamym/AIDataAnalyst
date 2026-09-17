import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "../lib/api";
import type {
  RoutineDefinitionHistoryRead,
  RoutineDefinitionVersionRead,
} from "../lib/types";

/* ---------------------------------------------------------------------------
   R11-FP03. `DefinitionHistoryPanel` states four rules in its own header, and
   each one is a decision a later edit could quietly undo. These are the tests
   that make undoing them fail:

   1. the limitation leads -- the derivation basis renders before any table
      name it qualifies, so an unreviewed parse is never read as approved
      lineage;
   2. absent is not unchanged -- a version whose footprint was not derived says
      which reason, and the reasons stay distinguishable, because an empty
      "what changed" line would read as "this capture touched the same tables";
   3. withheld is not missing -- a version the screening gate withholds keeps
      its row, with its marker;
   4. the body is not here, and the panel says so before a reader goes looking
      for a control that would reveal it -- and no rendering of any state puts
      definition text on screen.

   Plus the case the whole feature exists for: "started writing public.audit",
   said without a line of SQL.
--------------------------------------------------------------------------- */

const fetchRoutineDefinitionHistory =
  vi.fn<
    (
      routineId: string,
      options?: { limit?: number; offset?: number },
      signal?: AbortSignal,
    ) => Promise<RoutineDefinitionHistoryRead>
  >();

vi.mock("../lib/api/definitionHistory", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/definitionHistory")>();
  return {
    ...actual,
    fetchRoutineDefinitionHistory: (
      routineId: string,
      options?: { limit?: number; offset?: number },
      signal?: AbortSignal,
    ) => fetchRoutineDefinitionHistory(routineId, options, signal),
  };
});

function version(
  overrides: Partial<RoutineDefinitionVersionRead> = {},
): RoutineDefinitionVersionRead {
  return {
    version_id: `v-${overrides.version_number ?? 1}`,
    version_number: 1,
    captured_at: "2026-09-12T09:30:00Z",
    analysis_run_id: "run-1",
    availability: "AVAILABLE",
    unavailable_reason: null,
    truncated: false,
    change_class: "STRUCTURAL",
    redaction_status: "LEXICAL",
    screening_status: "CLEAN",
    definition_digest: "aaaaaaaabbbbbbbb",
    previous_definition_digest: "ccccccccdddddddd",
    body_released: false,
    withheld_marker: null,
    withheld_reason_code: null,
    footprint_state: "COMPUTED",
    parse_completed: true,
    unparsed_reason_codes: [],
    reads_table_names: ["public.postings"],
    writes_table_names: ["public.audit", "public.ledger"],
    reads_added: [],
    reads_removed: [],
    writes_added: [],
    writes_removed: [],
    ...overrides,
  };
}

function historyOf(
  versions: RoutineDefinitionVersionRead[],
  overrides: Partial<RoutineDefinitionHistoryRead> = {},
): RoutineDefinitionHistoryRead {
  return {
    routine_id: "r1",
    routine_qualified_name: "bank.public.settle",
    routine_type: "PROCEDURE",
    signature: "()",
    status: "ACTIVE",
    dialect: "postgres",
    footprint_basis: "REPARSED_STORED_DEFINITION",
    footprint_parse_budget: 25,
    versions,
    limit: 20,
    offset: 0,
    total: versions.length,
    ...overrides,
  };
}

/* Imported statically, and the module registry is deliberately NOT reset
   between tests: `classifyDefinitionHistoryError` narrows on `instanceof
   ApiError`, and a reset registry hands the component a second copy of the
   `../http` module -- so every `ApiError` this file constructs would fail the
   check and every classified branch would silently fall through to UNKNOWN. */
import { DefinitionHistoryPanel } from "./DefinitionHistoryPanel";

async function open(qualifiedName = "bank.public.settle") {
  render(<DefinitionHistoryPanel routineId="r1" qualifiedName={qualifiedName} />);
  await userEvent.click(screen.getByRole("button", { name: /definition history/i }));
}

beforeEach(() => {
  fetchRoutineDefinitionHistory.mockReset();
});

describe("DefinitionHistoryPanel", () => {
  it("does not read anything until a steward asks for it", async () => {
    fetchRoutineDefinitionHistory.mockResolvedValue(historyOf([version()]));
    render(<DefinitionHistoryPanel routineId="r1" />);

    // Every expansion costs a parse on the server, and most gap rows are read
    // without anyone needing the history.
    expect(fetchRoutineDefinitionHistory).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /definition history/i }));
    await waitFor(() => expect(fetchRoutineDefinitionHistory).toHaveBeenCalledWith("r1", {}, expect.anything()));
  });

  it("answers what the feature exists for: which table it started writing, and when", async () => {
    fetchRoutineDefinitionHistory.mockResolvedValue(
      historyOf([
        version({
          version_number: 2,
          captured_at: "2026-09-12T09:30:00Z",
          writes_added: ["public.audit"],
        }),
        version({
          version_number: 1,
          change_class: null,
          previous_definition_digest: null,
          writes_table_names: ["public.ledger"],
          reads_added: ["public.postings"],
          writes_added: ["public.ledger"],
        }),
      ]),
    );
    await open();

    await waitFor(() => expect(screen.getByText("Started writing public.audit")).toBeTruthy());
    expect(screen.getByText(/first capture/i)).toBeTruthy();
    expect(screen.getByText(/structural change/i)).toBeTruthy();
  });

  it("rule 1: the derivation basis renders before any table name it qualifies", async () => {
    fetchRoutineDefinitionHistory.mockResolvedValue(
      historyOf([version({ writes_added: ["public.audit"] })]),
    );
    await open();

    const basis = await waitFor(() =>
      screen.getByText(/not the reviewed lineage edges/i),
    );
    const claim = screen.getByText("Started writing public.audit");
    // Source order, not styling: a reader who meets the tables first has
    // already read an unreviewed parse as approved lineage.
    expect(basis.compareDocumentPosition(claim) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(basis.textContent).toMatch(/do not\s+follow calls into other routines/i);
  });

  it("rule 2: a footprint nobody derived says which reason, and the reasons differ", async () => {
    fetchRoutineDefinitionHistory.mockResolvedValue(
      historyOf([
        version({ version_number: 4, footprint_state: "NOT_COMPUTED" }),
        version({
          version_number: 3,
          change_class: "LITERAL_ONLY",
          footprint_state: "UNCHANGED_LITERALS_ONLY",
        }),
        version({ version_number: 2, footprint_state: "COMPUTED_NO_BASELINE" }),
        version({
          version_number: 1,
          change_class: null,
          availability: "UNAVAILABLE",
          unavailable_reason: "the login may not read this body",
          definition_digest: null,
          previous_definition_digest: null,
          withheld_marker: "[withheld by screening]",
          withheld_reason_code: "ROUTINE_BODY_UNAVAILABLE",
          footprint_state: "UNAVAILABLE",
          parse_completed: null,
          reads_table_names: [],
          writes_table_names: [],
        }),
      ]),
    );
    await open();

    await waitFor(() => expect(screen.getByText(/parse budget/i)).toBeTruthy());
    expect(screen.getByText(/Only literal values moved/i)).toBeTruthy();
    expect(screen.getByText(/version before this one was not derived/i)).toBeTruthy();
    expect(screen.getByText(/No definition to derive from/i)).toBeTruthy();
    // And the source's own words about why, which is what a steward acts on.
    expect(screen.getByText(/the login may not read this body/)).toBeTruthy();
  });

  it("rule 3: a quarantined version keeps its row, with its marker", async () => {
    fetchRoutineDefinitionHistory.mockResolvedValue(
      historyOf([
        version({
          version_number: 2,
          definition_digest: "2222222moved",
          previous_definition_digest: "1111111first",
          withheld_marker: "[withheld by screening]",
          withheld_reason_code: "ROUTINE_BODY_QUARANTINED",
          footprint_state: "WITHHELD",
          parse_completed: null,
          reads_table_names: [],
          writes_table_names: [],
        }),
        version({
          version_number: 1,
          change_class: null,
          definition_digest: "1111111first",
          previous_definition_digest: null,
        }),
      ]),
    );
    await open();

    await waitFor(() => expect(screen.getByText("[withheld by screening]")).toBeTruthy());
    // The row is there, not dropped: v2 is on screen with its ordinal.
    expect(screen.getByText("v2")).toBeTruthy();
    expect(screen.getByText(/quarantined it, so nothing is derived/i)).toBeTruthy();
    // The digest survives the withholding, which is the point of reporting it:
    // a steward who may not read the body can still see that the definition
    // moved -- the pair says so without either version's text.
    expect(screen.getByText(/definition 2222222 · previous 1111111/)).toBeTruthy();
  });

  it("rule 4: the panel says the text is never served, and no state renders any", async () => {
    fetchRoutineDefinitionHistory.mockResolvedValue(
      historyOf([
        version({ version_number: 3, footprint_state: "NOT_COMPUTED" }),
        version({
          version_number: 2,
          withheld_marker: "[withheld by screening]",
          withheld_reason_code: "ROUTINE_BODY_QUARANTINED",
          footprint_state: "WITHHELD",
          parse_completed: null,
        }),
        version({ version_number: 1, change_class: null, previous_definition_digest: null }),
      ]),
    );
    const { container } = render(<DefinitionHistoryPanel routineId="r1" />);
    await userEvent.click(screen.getByRole("button", { name: /definition history/i }));

    await waitFor(() =>
      expect(screen.getByText(/definition text itself is never served/i)).toBeTruthy(),
    );
    // Nothing on screen is SQL, in any state. A future "show the body" affordance
    // fails here as well as at the route, which is the point of asserting it
    // over the whole rendering rather than field by field.
    expect(container.textContent).not.toMatch(/INSERT INTO|SELECT |CREATE PROCEDURE/);
  });

  it("an unparsed statement qualifies the tables rather than hiding them", async () => {
    fetchRoutineDefinitionHistory.mockResolvedValue(
      historyOf([
        version({
          parse_completed: false,
          unparsed_reason_codes: ["DYNAMIC_SQL"],
          writes_added: ["public.audit"],
        }),
      ]),
    );
    await open();

    await waitFor(() =>
      expect(screen.getByText(/Not every statement in this definition could be read/i)).toBeTruthy(),
    );
    expect(screen.getByText(/DYNAMIC_SQL/)).toBeTruthy();
    expect(screen.getByText("Started writing public.audit")).toBeTruthy();
  });

  it("a routine with no captured definition is not an error and not 'no changes'", async () => {
    fetchRoutineDefinitionHistory.mockResolvedValue(historyOf([]));
    await open();

    await waitFor(() =>
      expect(screen.getByText(/No definition has been captured for this routine/i)).toBeTruthy(),
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a missing routine reads differently from a routine with nothing captured", async () => {
    fetchRoutineDefinitionHistory.mockRejectedValue(new ApiError(404, "routine not found"));
    await open();

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByText(/This routine no longer exists/i)).toBeTruthy();
  });

  it("a refusal names the routine it was refused for", async () => {
    fetchRoutineDefinitionHistory.mockRejectedValue(new ApiError(403, "not authorized"));
    await open();

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByText(/bank\.public\.settle/)).toBeTruthy();
  });
});
