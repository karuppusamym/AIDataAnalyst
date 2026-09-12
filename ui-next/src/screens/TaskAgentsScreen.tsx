import { TaskAgentConsole } from "../components/TaskAgentConsole";
import type { ReviewLink } from "../components/TaskAgentConsole";
import type { TaskAgentKind } from "../lib/api";
import { buildRelativeLink } from "../lib/routes";
import { useUrlState } from "../lib/useUrlState";
import "./TaskAgentsScreen.css";

/* ---------------------------------------------------------------------------
   Task agents — ADR-0029. One console, R11-S10.

   WHAT THIS MERGE REMOVED. `steward-agent`, `lineage-agent` and
   `quality-agent` were three routes, three nav entries, three lazy chunks and
   three screen files that between them contained no behaviour: each was a
   thirty-line wrapper that rendered `TaskAgentConsole` with a different
   `kind`, a different label map and a different paragraph of copy. The
   component's own docstring already said why -- "they share one runtime
   server-side, so they share one control surface here" -- and the three
   routes were the one place that had not caught up.

   A steward supervising the agents had to know that three separate pages
   existed and visit each to see whether anything was waiting. The agent is now
   a FILTER on one page (`?agent=steward`), which is what it always was in the
   data, and the three consoles are one destination with a selector.

   WHAT IT DID NOT REMOVE. Every per-agent word on the screen -- the
   description, the capability labels, the skip reasons, the empty-run hint,
   and the lineage agent's link into the parsed-lineage queue -- is preserved
   verbatim below. They were the only thing that differed, so they are the only
   thing this file carries. The old routes still resolve
   (`RETIRED_SCREEN_ALIASES` in `lib/routes.ts`), landing on this screen with
   that agent selected.
--------------------------------------------------------------------------- */

/** The edge type each lineage capability writes, so a proposal opens the
 *  per-edge queue already filtered to it: a view's edges, or a captured
 *  routine's. */
const QUEUE_EDGE_TYPE: Record<string, string> = {
  VIEW_LINEAGE: "VIEW",
  PROCEDURE_LINEAGE: "ROUTINE",
};

/* The lineage agent's edges are decided one by one in the parsed-lineage
   queue, never activated by the agent, so its proposals link there rather than
   to the governance queue. That queue is now a queue OF the review surface, so
   the link carries `queue=parsed-lineage` -- the same page, named the way the
   merged route names it. */
const parsedLineageQueue: ReviewLink = (item) =>
  item.action === "PROPOSED"
    ? buildRelativeLink({
        screen: "governance",
        params: {
          queue: "parsed-lineage",
          type: QUEUE_EDGE_TYPE[item.capability] ?? null,
        },
      })
    : null;

interface TaskAgentDefinition {
  readonly kind: TaskAgentKind;
  readonly label: string;
  readonly title: string;
  readonly description: string;
  readonly capabilityLabels: Record<string, string>;
  readonly skipLabels: Record<string, string>;
  readonly supervisorPersona: string;
  readonly emptyRunHint: string;
  readonly reviewLink?: ReviewLink;
}

/** The three agents, and the only thing that ever differed between them. */
export const TASK_AGENTS: readonly TaskAgentDefinition[] = [
  {
    kind: "steward",
    label: "Steward",
    title: "Steward agent",
    description:
      "Works the documentation backlog in the order the worklist ranks it: drafts table and column descriptions and glossary links from catalog evidence and puts each one in the review queue as its own request. It decides nothing, and it calls no model.",
    capabilityLabels: {
      TABLE_DESCRIPTION: "Table descriptions",
      COLUMN_DESCRIPTION: "Column descriptions",
      GLOSSARY_LINK: "Glossary links",
    },
    skipLabels: {
      open_draft_exists: "a draft is already open",
      identical_text_rejected: "identical text was rejected before",
      below_evidence_bar: "too little evidence to submit",
    },
    supervisorPersona: "STEWARD",
    emptyRunHint:
      "Every undocumented table on the worklist is already in review, or there is no exact label match waiting for a link.",
  },
  {
    kind: "lineage",
    label: "Lineage",
    title: "Lineage agent",
    description:
      "Parses the view definitions and stored procedure bodies captured at ingestion and proposes the column lineage it finds. Every edge waits in the parsed-lineage review queue for a person; the agent activates nothing, and it calls no model.",
    capabilityLabels: {
      VIEW_LINEAGE: "View lineage",
      PROCEDURE_LINEAGE: "Procedure lineage",
    },
    skipLabels: {
      unsupported_dialect: "the source's SQL dialect is not supported",
      unparseable_definition: "the definition could not be parsed",
      no_resolvable_lineage: "no source the parser could resolve",
      lineage_already_known: "every edge is already known",
    },
    supervisorPersona: "STEWARD",
    emptyRunHint:
      "Every eligible view and procedure already has lineage, or what was captured was examined and could not be used.",
    reviewLink: parsedLineageQueue,
  },
  {
    kind: "quality",
    label: "Quality",
    title: "Quality agent",
    description:
      "Proposes value-free quality rules from profile history — a floor under a table's row count, a ceiling over a normally-complete column's null rate. A failing rule gates governed tools, so a person decides every proposal; the agent creates no rule itself, and it calls no model.",
    capabilityLabels: {
      ROW_COUNT_FLOOR: "Row-count floors",
      NULL_RATE_CEILING: "Null-rate ceilings",
    },
    skipLabels: {
      rule_or_proposal_exists: "a rule or a proposal already covers it",
    },
    supervisorPersona: "STEWARD",
    emptyRunHint:
      "No table or column has enough recent profile history without a rule or a proposal already covering it.",
  },
];

const DEFAULT_AGENT = TASK_AGENTS[0]!;

export function TaskAgentsScreen() {
  /* The selected agent lives in the URL, so a console is shareable, survives
   * Back/Forward, and -- the reason the aliases work -- a bookmark to one of
   * the three retired routes names the agent it used to open. */
  const [params, setParams] = useUrlState();
  const requested = params.get("agent");
  const active = TASK_AGENTS.find((agent) => agent.kind === requested) ?? DEFAULT_AGENT;

  return (
    <section className="taskagents">
      {/* A tablist, not three links: these are three views of one page, and
          announcing them as navigation would tell a screen-reader user they
          are leaving the screen when they are not. */}
      <div className="taskagents__tabs" role="tablist" aria-label="Task agent">
        {TASK_AGENTS.map((agent) => (
          <button
            key={agent.kind}
            type="button"
            role="tab"
            id={`task-agent-tab-${agent.kind}`}
            className="taskagents__tab"
            aria-selected={agent.kind === active.kind}
            aria-controls={`task-agent-panel-${agent.kind}`}
            onClick={() => setParams({ agent: agent.kind })}
          >
            {agent.label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id={`task-agent-panel-${active.kind}`}
        aria-labelledby={`task-agent-tab-${active.kind}`}
      >
        {/* Keyed on the agent so switching remounts the console rather than
            letting one agent's run results sit under another agent's name
            while its own request is still in flight. */}
        <TaskAgentConsole
          key={active.kind}
          kind={active.kind}
          title={active.title}
          description={active.description}
          capabilityLabels={active.capabilityLabels}
          skipLabels={active.skipLabels}
          supervisorPersona={active.supervisorPersona}
          emptyRunHint={active.emptyRunHint}
          reviewLink={active.reviewLink}
        />
      </div>
    </section>
  );
}
