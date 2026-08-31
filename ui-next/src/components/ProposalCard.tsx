import type { ReactNode } from "react";
import { Button, Pill } from "./primitives";
import "./ProposalCard.css";

/* ---------------------------------------------------------------------------
   A proposal is the unit of governed change in this product, so it is a shell
   primitive rather than a feature of one screen.

   ADR-0001 says models propose and deterministic services decide. A proposal
   that shows only its outcome is not reviewable — the reviewer is being asked
   to rubber-stamp. So a proposal ALWAYS carries four things, and the type makes
   three of them non-optional:

     1. the diff        what would change, exactly
     2. confidence      how sure the proposer is, as a number
     3. the rationale   WHY, citing the evidence it was derived from
     4. accept / reject an explicit, symmetric decision

   `rationale` is required for the same reason `esc()` should never have been
   optional in the old portal: the one that gets skipped is the one that
   matters. A reviewer approving 200 descriptions needs the "why" more than
   the reviewer approving one.
--------------------------------------------------------------------------- */

export interface DiffLine {
  kind: "add" | "remove" | "context";
  text: string;
}

export interface Proposal {
  id: string;
  title: string;
  /** Subject of the change — the asset, metric or policy it applies to. */
  subject: string;
  /** Who proposed it. `agent` renders with an explicit machine attribution. */
  proposedBy: { kind: "agent" | "human"; name: string };
  confidence: number;
  diff: DiffLine[];
  /** Required. Cites the evidence, not a restatement of the change. */
  rationale: string;
  /** Where the rationale's evidence came from, so it can be checked. */
  evidence: string;
  state: "needs_review" | "auto_applied" | "rejected" | "approved";
}

const pct = (n: number) => `${Math.round(n * 100)}%`;

/** Confidence is shown as a number AND a bar, because "91%" and "high" are not
 *  the same claim and a steward tuning an auto-apply threshold needs the number. */
function Confidence({ value }: { value: number }) {
  const tone = value >= 0.9 ? "ok" : value >= 0.75 ? "warn" : "bad";
  return (
    <span className="conf" title={`Proposer confidence ${pct(value)}`}>
      <span className="conf__bar">
        <span className={`conf__fill conf__fill--${tone}`} style={{ width: pct(value) }} />
      </span>
      <span className="conf__n tnum">{pct(value)}</span>
    </span>
  );
}

export function ProposalCard({
  proposal,
  onApprove,
  onReject,
  footer,
}: {
  proposal: Proposal;
  onApprove?: (id: string) => void;
  onReject?: (id: string) => void;
  footer?: ReactNode;
}) {
  const { state } = proposal;
  const decided = state === "approved" || state === "rejected";

  return (
    <article className={`prop prop--${state}`} aria-label={proposal.title}>
      <header className="prop__head">
        <div className="prop__lead">
          <div className="prop__badges">
            {state === "needs_review" ? <Pill tone="warn">review needed</Pill> : null}
            {state === "auto_applied" ? <Pill tone="ok">auto-applied</Pill> : null}
            {state === "approved" ? <Pill tone="ok">approved</Pill> : null}
            {state === "rejected" ? <Pill tone="mute">rejected</Pill> : null}
            <Pill tone={proposal.proposedBy.kind === "agent" ? "info" : "mute"}>
              {proposal.proposedBy.kind === "agent" ? "proposed by " : "raised by "}
              {proposal.proposedBy.name}
            </Pill>
          </div>
          <h3 className="prop__title">{proposal.title}</h3>
          <div className="prop__subject">{proposal.subject}</div>
        </div>
        <Confidence value={proposal.confidence} />
      </header>

      <div className="prop__diff" role="group" aria-label="Proposed change">
        {proposal.diff.map((l, i) => (
          <div key={i} className={`dl dl--${l.kind}`}>
            <span className="dl__g" aria-hidden="true">
              {l.kind === "add" ? "+" : l.kind === "remove" ? "−" : " "}
            </span>
            <span className="dl__t">{l.text}</span>
          </div>
        ))}
      </div>

      <div className="prop__why">
        <span className="prop__whyk">Why</span>
        <div>
          <p className="prop__whyt">{proposal.rationale}</p>
          <p className="prop__ev">{proposal.evidence}</p>
        </div>
      </div>

      {footer ?? (
        <div className="prop__act">
          {decided ? (
            <span className="prop__done">
              {state === "approved" ? "Approved — applied under maker-checker" : "Rejected"}
            </span>
          ) : state === "auto_applied" ? (
            <>
              <span className="prop__done">
                Applied automatically — above this tenant's threshold
              </span>
              <Button onClick={() => onReject?.(proposal.id)}>Revert</Button>
            </>
          ) : (
            <>
              <Button variant="primary" onClick={() => onApprove?.(proposal.id)}>
                Approve
              </Button>
              <Button onClick={() => onReject?.(proposal.id)}>Reject</Button>
            </>
          )}
        </div>
      )}
    </article>
  );
}
