import { TaskAgentConsole } from "../components/TaskAgentConsole";

/* ---------------------------------------------------------------------------
   Quality agent — ADR-0029.

   Proposes DQ-4 threshold rules from profile history: a floor under a table's
   row count, a ceiling over a normally-complete column's null rate. A failing
   rule gates governed tools, so every proposal is T2 and a person decides it —
   the reviewer agent cannot. Proposals open in the shared review queue.
--------------------------------------------------------------------------- */

const CAPABILITY_LABELS: Record<string, string> = {
  ROW_COUNT_FLOOR: "Row-count floors",
  NULL_RATE_CEILING: "Null-rate ceilings",
};

const SKIP_LABELS: Record<string, string> = {
  rule_or_proposal_exists: "a rule or a proposal already covers it",
};

export function QualityAgentScreen() {
  return (
    <TaskAgentConsole
      kind="quality"
      title="Quality agent"
      description="Proposes value-free quality rules from profile history — a floor under a table's row count, a ceiling over a normally-complete column's null rate. A failing rule gates governed tools, so a person decides every proposal; the agent creates no rule itself, and it calls no model."
      capabilityLabels={CAPABILITY_LABELS}
      skipLabels={SKIP_LABELS}
      supervisorPersona="STEWARD"
      emptyRunHint="No table or column has enough recent profile history without a rule or a proposal already covering it."
    />
  );
}
