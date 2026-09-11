import { TaskAgentConsole } from "../components/TaskAgentConsole";

/* ---------------------------------------------------------------------------
   Steward agent — ADR-0029.

   Works the documentation backlog in the order the worklist ranks it: drafts
   table descriptions and glossary links from catalog evidence and puts each in
   the review queue as its own request. The control surface is every task
   agent's (`TaskAgentConsole`); this screen names the agent and its words.
--------------------------------------------------------------------------- */

const CAPABILITY_LABELS: Record<string, string> = {
  TABLE_DESCRIPTION: "Table descriptions",
  GLOSSARY_LINK: "Glossary links",
};

const SKIP_LABELS: Record<string, string> = {
  open_draft_exists: "a draft is already open",
  identical_text_rejected: "identical text was rejected before",
  below_evidence_bar: "too little evidence to submit",
};

export function StewardAgentScreen() {
  return (
    <TaskAgentConsole
      kind="steward"
      title="Steward agent"
      description="Works the documentation backlog in the order the worklist ranks it: drafts table descriptions and glossary links from catalog evidence and puts each one in the review queue as its own request. It decides nothing, and it calls no model."
      capabilityLabels={CAPABILITY_LABELS}
      skipLabels={SKIP_LABELS}
      supervisorPersona="STEWARD"
      emptyRunHint="Every undocumented table on the worklist is already in review, or there is no exact label match waiting for a link."
    />
  );
}
