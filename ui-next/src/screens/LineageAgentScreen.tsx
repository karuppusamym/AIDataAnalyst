import { TaskAgentConsole } from "../components/TaskAgentConsole";
import type { ReviewLink } from "../components/TaskAgentConsole";
import { buildRelativeLink } from "../lib/routes";

/* ---------------------------------------------------------------------------
   Lineage agent — ADR-0029.

   Parses the view definitions and routine bodies ingestion captured — nothing
   else ever did — and proposes the lineage it finds. Its edges are decided one
   by one in the parsed-lineage review queue (ADR-0026), never activated by the
   agent, so a proposal links there rather than to the shared review queue.
--------------------------------------------------------------------------- */

const CAPABILITY_LABELS: Record<string, string> = {
  VIEW_LINEAGE: "View lineage",
  PROCEDURE_LINEAGE: "Procedure lineage",
};

const SKIP_LABELS: Record<string, string> = {
  unsupported_dialect: "the source's SQL dialect is not supported",
  unparseable_definition: "the definition could not be parsed",
  no_resolvable_lineage: "no source the parser could resolve",
  lineage_already_known: "every edge is already known",
};

/* The edge type each capability writes, so a proposal opens the per-edge queue
   already filtered to it: a view's edges, or a captured routine's. */
const QUEUE_EDGE_TYPE: Record<string, string> = {
  VIEW_LINEAGE: "VIEW",
  PROCEDURE_LINEAGE: "ROUTINE",
};

const parsedLineageQueue: ReviewLink = (item) =>
  item.action === "PROPOSED"
    ? buildRelativeLink({
        screen: "parsed-lineage-review",
        params: { type: QUEUE_EDGE_TYPE[item.capability] ?? null },
      })
    : null;

export function LineageAgentScreen() {
  return (
    <TaskAgentConsole
      kind="lineage"
      title="Lineage agent"
      description="Parses the view definitions and stored procedure bodies captured at ingestion and proposes the column lineage it finds. Every edge waits in the parsed-lineage review queue for a person; the agent activates nothing, and it calls no model."
      capabilityLabels={CAPABILITY_LABELS}
      skipLabels={SKIP_LABELS}
      supervisorPersona="STEWARD"
      emptyRunHint="Every eligible view and procedure already has lineage, or what was captured was examined and could not be used."
      reviewLink={parsedLineageQueue}
    />
  );
}
