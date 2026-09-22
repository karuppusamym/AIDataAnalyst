import { useMemo } from "react";
import { fetchOwnershipOperations } from "../lib/api";
import type { BulkStewardshipOperationRead } from "../lib/types";
import { readDecision } from "../lib/roles";
import { useSession } from "../lib/session";
import { Button, Empty, ErrorState, Pill } from "../components/primitives";
import { useAsyncResource } from "../components/screenState";
import { NotApplicable, describeOperationStatus, openReview, stamp, stringParameter } from "./OwnershipParts";

/* ---------------------------------------------------------------------------
   Requests -- what happened to the reviews a rule or a leaver opened
   (R11-AUD08, part 2).

   WHY THIS EXISTS. `apply` and the leaver request each answer 202 with an
   operation that has moved nothing (see `lib/api/ownership.ts`). The steward who
   asked is then owed the second half of the answer -- "how many were actually
   moved, and how many were left" -- and the only place the platform keeps it is
   the operation row, once a reviewer has decided: `applied_count` against how
   many subjects it was given. A screen that showed only the 202 would end the
   story at "review requested" and leave a leaver's tables owned by a leaver.

   WHAT IT READS. `GET .../stewardship/bulk-operations`, newest first, the most the
   route allows (500). The route has no type filter, so the two kinds that change
   ownership are picked out here, and the panel says it is looking at the newest
   500 -- an organization with a great many other bulk operations could push an old
   request out of that window, and a list that claimed to be "all" would then be
   wrong. Rule requests are the `ASSIGN_OWNERSHIP` operations that carry a
   `source_rule_id`; leaver requests are `REASSIGN_LEAVER`.

   THE NUMBERS ARE THE SERVER'S. "Applied" is `applied_count`, and the rest are
   described the way `apply_bulk_operation` describes them: every branch skips a
   subject that is already in the requested state or went stale between request and
   decision. Not "failed" -- a skip is not an error, and calling it one would send a
   steward looking for a fault that is not there.

   Read roles are the bulk-operations list's, copied from the matrix below. Nothing
   here writes.
--------------------------------------------------------------------------- */

/**
 * The roles `GET /v1/organizations/{organization_id}/stewardship/bulk-operations` admits.
 *
 * Copied from the surface-control matrix row for
 * `aida.stewardship_api.list_bulk_stewardship_operations`
 * (`Docs/50-security/surface-control-matrix.md`): Analyst, Auditor, DataAdmin,
 * DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer.
 */
const OPERATIONS_READ_ROLES = [
  "Analyst",
  "Auditor",
  "DataAdmin",
  "DataSteward",
  "MetadataAdmin",
  "PlatformAdmin",
  "Reviewer",
  "SemanticAdmin",
  "Viewer",
];

/** The newest operations the route will return in one page. */
const WINDOW = 500;

export type RequestKind = "rules" | "leaver";

const isRuleRequest = (operation: BulkStewardshipOperationRead): boolean =>
  operation.operation_type === "ASSIGN_OWNERSHIP" && stringParameter(operation, "source_rule_id") !== null;

const isLeaverRequest = (operation: BulkStewardshipOperationRead): boolean =>
  operation.operation_type === "REASSIGN_LEAVER";

function Outcome({ operation, noun }: { operation: BulkStewardshipOperationRead; noun: string }) {
  const count = operation.subject_ids.length;
  if (operation.status === "APPLIED") {
    const left = Math.max(0, count - operation.applied_count);
    return (
      <p className="own__opline">
        <strong>
          {operation.applied_count} of {count} {noun} changed
        </strong>
        {left > 0
          ? `; the other ${left} were already as requested or had changed since the request, and were left as they were.`
          : "."}{" "}
        Applied by {operation.applied_by ?? "an unknown reviewer"} at {stamp(operation.applied_at)}.
      </p>
    );
  }
  if (operation.status === "REJECTED") {
    return <p className="own__opline">Rejected by the reviewer. Nothing was changed.</p>;
  }
  return (
    <div className="own__opline">
      <span>Nothing has changed yet: a different reviewer has to approve this.</span>{" "}
      <Button onClick={() => openReview(operation)}>Open this review</Button>
    </div>
  );
}

function RequestRow({
  operation,
  kind,
  ruleNames,
}: {
  operation: BulkStewardshipOperationRead;
  kind: RequestKind;
  ruleNames: ReadonlyMap<string, string>;
}) {
  const status = describeOperationStatus(operation.status);
  const count = operation.subject_ids.length;
  let title: string;
  let detail: string;
  if (kind === "rules") {
    const ruleId = stringParameter(operation, "source_rule_id") ?? "";
    title = `Rule “${ruleNames.get(ruleId) ?? "no longer listed"}”`;
    detail = `owner ${stringParameter(operation, "owner_principal") ?? "unknown"} (${(stringParameter(operation, "owner_type") ?? "").toLowerCase()})`;
  } else {
    title = `${stringParameter(operation, "leaving_principal") ?? "unknown"} → ${stringParameter(operation, "successor_principal") ?? "unknown"}`;
    detail = `${stringParameter(operation, "owner_type")?.toLowerCase() ?? "individual"} ownerships${
      operation.parameters.selection_truncated === true
        ? ", more than 500 held: only the first 500 are in this request"
        : ""
    }`;
  }
  return (
    <li className="own__op">
      <div className="own__ophead">
        <Pill tone={status.tone}>{status.label}</Pill>
        <strong>{title}</strong>
      </div>
      <div className="own__opmeta">
        <span>{detail}</span>
        <span>
          {count} {kind === "rules" ? (count === 1 ? "table" : "tables") : count === 1 ? "ownership" : "ownerships"}{" "}
          in the request
        </span>
        <span>
          requested by {operation.requested_by} at {stamp(operation.created_at)}
        </span>
      </div>
      <Outcome operation={operation} noun={kind === "rules" ? "tables" : "ownerships"} />
    </li>
  );
}

export function OwnershipRequests({
  organizationId,
  kind,
  ruleNames,
  refreshKey,
}: {
  organizationId: string;
  kind: RequestKind;
  /** Rule id -> display name, for naming the rule a request came from. */
  ruleNames?: ReadonlyMap<string, string>;
  /** Bumped by the parent after it opens a request, so the list is re-read. */
  refreshKey: number;
}) {
  const session = useSession();
  // Held while `/v1/me` is in flight and never sent for a session outside the list.
  const read = readDecision(session, OPERATIONS_READ_ROLES);
  const operations = useAsyncResource<{ items: BulkStewardshipOperationRead[]; total: number }>(
    (signal) => fetchOwnershipOperations(organizationId, { limit: WINDOW }, signal),
    [organizationId, refreshKey],
    { enabled: read === "ask" },
  );
  const rows = useMemo(
    () => (operations.data?.items ?? []).filter(kind === "rules" ? isRuleRequest : isLeaverRequest),
    [operations.data, kind],
  );
  const names = ruleNames ?? new Map<string, string>();
  const heading = kind === "rules" ? "Rule requests" : "Leaver requests";

  return (
    <section className="own__panel" aria-label={heading}>
      <div className="own__panelhead">
        <div>
          <p className="own__eyebrow">REQUESTS</p>
          <h3 className="own__h2">{heading}</h3>
        </div>
        {read === "ask" && operations.data ? <Pill tone="mute">{rows.length}</Pill> : null}
      </div>
      {read === "skip" ? (
        <NotApplicable what="the requests that have been made" roles={OPERATIONS_READ_ROLES} />
      ) : read === "wait" || (operations.loading && !operations.data) ? (
        <p className="own__note" role="status">
          Loading requests…
        </p>
      ) : operations.error ? (
        <ErrorState title="Requests could not be loaded" detail={operations.error} onRetry={operations.reload} />
      ) : rows.length === 0 ? (
        <Empty
          title={kind === "rules" ? "No rule has been applied yet" : "No leaver reassignment has been requested yet"}
          hint={`Looked at the newest ${WINDOW} stewardship operations. A request you make appears here as soon as it is opened.`}
        />
      ) : (
        <>
          <ul className="own__oplist" aria-label={heading}>
            {rows.map((operation) => (
              <RequestRow key={operation.id} operation={operation} kind={kind} ruleNames={names} />
            ))}
          </ul>
          {operations.data && operations.data.total > operations.data.items.length ? (
            <p className="own__note">
              Showing the newest {operations.data.items.length} of {operations.data.total} stewardship operations;
              an older request may not be listed.
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}
