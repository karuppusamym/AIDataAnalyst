import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  LEAVER_REASSIGNMENT_MAX_ITEMS,
  OWNERSHIP_OWNER_TYPES,
  fetchOwnershipPortfolio,
  requestLeaverReassignment,
} from "../lib/api";
import type { OwnershipOwnerType, OwnershipPortfolio } from "../lib/api";
import type { BulkStewardshipOperationRead, LeaverReassignmentRequest } from "../lib/types";
import { useOrgId } from "../lib/org";
import { roleHolds } from "../lib/roles";
import { useSession } from "../lib/session";
import { Button, Field, Pill, useUnsavedChanges } from "../components/primitives";
import { FormError, useSubmitAction } from "../components/screenState";
import { HintedField, OwnershipConfirm, RequestedReview, describeExpiry, listOr } from "./OwnershipParts";
import { OwnershipRequests } from "./OwnershipRequests";

/* ---------------------------------------------------------------------------
   Ownership -> Leaver reassignment (R11-AUD08, part 2; GL-7).

   Someone leaves. Every table and glossary term they own now has an owner who is
   not there. This is the one governed request that moves ALL of it to a
   successor -- and it is a REQUEST. `request_leaver_reassignment` opens one
   `REASSIGN_LEAVER` operation and one review (status `REVIEW_REQUIRED`) and moves
   nothing; a different principal has to approve it. Only then is each of the
   leaver's ACTIVE assignments marked REASSIGNED and the successor recorded as its
   owner (a row the successor already held is made active again), and any that
   changed between request and approval are skipped, not failed.

   The confirmation says that, and says what is NOT touched: certifications the
   leaver granted (`AssetCertification.certified_by`) are a historical attestation
   of who certified and are deliberately out of scope, so a reassignment never
   rewrites who vouched for a table.

   THE PREVIEW is worked out here, not asked of the server: the assignments listing
   has no owner filter, so `fetchOwnershipPortfolio` pages it and keeps the leaver's
   rows. It is a preview and says so -- its bounded read reports when it stopped
   early, and the request does not depend on it. Two ways to send:

     * everything: `assignment_ids` omitted, and the server takes the leaver's whole
       active portfolio itself, first 500, recording `selection_truncated` when there
       was more. This is what a steward gets by leaving every row ticked.
     * a chosen subset: the ticked ids, at most 500 (the schema's own limit), each of
       which the server re-checks is ACTIVE, held by the leaver, and of this owner type
       -- a 409 if any is not.

   A steward who has not previewed can still send: the server discovers the portfolio
   either way, and the confirmation says the list was not seen.

   THE RESULT is the 202's operation, and it is reported for what it is: how many
   ownerships are IN the request, that `applied_count` is 0, and -- from the preview,
   when there was one, and from `selection_truncated` -- how many were left out. The
   after-the-decision numbers (how many moved, how many were skipped) are the
   Requests list's job, beneath.

   The successor and leaver are free text: neither is checked against identity by the
   server, so a typo in the successor would name a principal who does not exist. The
   preview is the guard on the leaver (zero found says so); the successor is shown
   back, in full, in the confirmation.
--------------------------------------------------------------------------- */

/**
 * The roles `POST /v1/organizations/{organization_id}/stewardship/leaver-reassignment` admits.
 *
 * Copied from the surface-control matrix row for
 * `aida.stewardship_api.request_leaver_reassignment`
 * (`Docs/50-security/surface-control-matrix.md`): DataSteward, MetadataAdmin,
 * PlatformAdmin, SemanticAdmin.
 */
const LEAVER_WRITE_ROLES = ["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"];

/**
 * The roles `GET /v1/organizations/{organization_id}/ownership-assignments` admits -- what the
 * preview reads to find the leaver's ownerships.
 *
 * Copied from the matrix row for `aida.stewardship_api.list_ownership_assignments`:
 * Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer,
 * SemanticAdmin, Viewer. (The four write roles above are all in it, so a session offered this
 * form can always preview; the check is kept anyway so the preview never depends on that.)
 */
const PREVIEW_READ_ROLES = [
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

export const LEAVER_UNSAVED_MESSAGE = "Discard the leaver reassignment you have not requested?";

const OWNER_TYPE_LABEL: Record<OwnershipOwnerType, string> = { INDIVIDUAL: "Individual", GROUP: "Group" };

/** The sentence about what a request leaves out, or undefined when it leaves nothing the caller knows of. */
function describeRemainder(
  operation: BulkStewardshipOperationRead,
  leaving: string,
  preview: OwnershipPortfolio | null,
): string | undefined {
  const requested = operation.subject_ids.length;
  if (operation.parameters.selection_truncated === true) {
    return `${leaving} holds more than ${LEAVER_REASSIGNMENT_MAX_ITEMS} ownerships. This request covers the first ${LEAVER_REASSIGNMENT_MAX_ITEMS}; the rest stay with ${leaving}. Request again once this review is decided to move them.`;
  }
  if (preview && requested < preview.items.length) {
    const left = preview.items.length - requested;
    return `${left} of the ${preview.items.length} ownerships the preview found ${left === 1 ? "is" : "are"} not in this request and stay${left === 1 ? "s" : ""} with ${leaving}.`;
  }
  if (preview && !preview.complete) {
    return `The preview stopped after reading ${preview.scanned} of ${preview.total} assignments, so ${leaving} may hold more than it found. The server takes ${leaving}'s whole portfolio itself when no ids are named.`;
  }
  return undefined;
}

export function OwnershipLeaver() {
  const organizationId = useOrgId();
  const session = useSession();
  const roles = session.me?.roles;
  // A control that opens a governed request is offered only to a session KNOWN to hold a role (fail closed).
  const mayRequest = roleHolds(roles, LEAVER_WRITE_ROLES);
  const mayPreview = roleHolds(roles, PREVIEW_READ_ROLES);
  const identityKnown = roles !== undefined;

  const [leaving, setLeaving] = useState("");
  const [successor, setSuccessor] = useState("");
  const [ownerType, setOwnerType] = useState<OwnershipOwnerType>("INDIVIDUAL");
  const [rationale, setRationale] = useState("");
  const edited = leaving !== "" || successor !== "" || rationale !== "";
  useUnsavedChanges(edited, LEAVER_UNSAVED_MESSAGE);

  const leavingName = leaving.trim();
  const successorName = successor.trim();

  // ---- the preview -------------------------------------------------------------------------
  const previewAction = useSubmitAction<OwnershipPortfolio>();
  const previewAbort = useRef<AbortController | null>(null);
  useEffect(() => () => previewAbort.current?.abort(), []);
  // What the preview was run FOR: a preview of someone else, or of another owner type, is not this one's.
  const [previewedFor, setPreviewedFor] = useState<string | null>(null);
  const currentKey = JSON.stringify([leavingName, ownerType]);
  const preview = previewAction.result && previewedFor === currentKey ? previewAction.result : null;
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());

  const runPreview = async () => {
    previewAbort.current?.abort();
    const controller = new AbortController();
    previewAbort.current = controller;
    const key = currentKey;
    const portfolio = await previewAction.run(() =>
      fetchOwnershipPortfolio(organizationId, leavingName, ownerType, controller.signal),
    );
    if (portfolio === null) return; // the failure is in `previewAction.error`
    setPreviewedFor(key);
    setSelected(new Set(portfolio.items.map((row) => row.id)));
  };

  const toggle = (id: string) =>
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  // ---- what the request will send ----------------------------------------------------------
  const allChosen = preview !== null && selected.size === preview.items.length;
  // Ids are named only when the steward narrowed the preview; otherwise the server finds the portfolio.
  const explicitIds = preview !== null && !allChosen ? preview.items.filter((row) => selected.has(row.id)).map((row) => row.id) : null;

  const problems: string[] = [];
  if (leavingName === "") problems.push("Name the person or group who is leaving.");
  if (successorName === "") problems.push("Name the successor.");
  else if (successorName === leavingName) problems.push("The successor must be a different principal.");
  if (rationale.trim().length < 10) problems.push("Give a rationale of at least 10 characters.");
  if (explicitIds !== null && explicitIds.length === 0) problems.push("Tick at least one ownership, or preview again.");
  if (explicitIds !== null && explicitIds.length > LEAVER_REASSIGNMENT_MAX_ITEMS) {
    problems.push(
      `Tick at most ${LEAVER_REASSIGNMENT_MAX_ITEMS} ownerships, or keep every row ticked and let the server take the first ${LEAVER_REASSIGNMENT_MAX_ITEMS}.`,
    );
  }
  const valid = problems.length === 0;

  // ---- the request -------------------------------------------------------------------------
  const [confirming, setConfirming] = useState(false);
  const request = useSubmitAction<BulkStewardshipOperationRead>();
  const [refreshKey, setRefreshKey] = useState(0);
  const [outcome, setOutcome] = useState<{
    operation: BulkStewardshipOperationRead;
    leaving: string;
    successor: string;
    preview: OwnershipPortfolio | null;
  } | null>(null);

  const confirmRequest = async () => {
    const body: LeaverReassignmentRequest = {
      leaving_principal: leavingName,
      successor_principal: successorName,
      owner_type: ownerType,
      rationale: rationale.trim(),
      ...(explicitIds !== null ? { assignment_ids: explicitIds } : {}),
    };
    const operation = await request.run(() => requestLeaverReassignment(organizationId, body));
    if (operation === null) return; // the refusal is in `request.error`, shown in the dialog
    request.reset();
    setConfirming(false);
    setOutcome({ operation, leaving: leavingName, successor: successorName, preview });
    // The request is made. Clearing the form is what stops the same one being sent twice: the server
    // does not notice a duplicate, and each would be a second review for someone to decide.
    setLeaving("");
    setSuccessor("");
    setRationale("");
    setSelected(new Set());
    setPreviewedFor(null);
    previewAction.reset();
    setRefreshKey((key) => key + 1);
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!valid) return;
    request.reset();
    setConfirming(true);
  };

  // ---- the confirmation --------------------------------------------------------------------
  const moves =
    explicitIds !== null && preview
      ? `${explicitIds.length} of the ${preview.items.length} ownerships the preview found for ${leavingName}`
      : preview
        ? `all ${preview.items.length} ownerships the preview found for ${leavingName}${preview.complete ? "" : ", and any more the server finds beyond what the preview read"}`
        : `every active ${ownerType.toLowerCase()} ownership ${leavingName} holds when the server reads them (you have not previewed the list)`;

  return (
    <div className="own__body">
      {mayRequest ? (
        <section className="own__panel" aria-label="Request a leaver reassignment">
          <div className="own__panelhead">
            <div>
              <p className="own__eyebrow">LEAVER REASSIGNMENT</p>
              <h2 className="own__h2">Move a leaver&rsquo;s ownerships to a successor</h2>
            </div>
          </div>
          <p className="own__note">
            This is a request, not a transfer. It asks a reviewer to make the successor the owner of what the leaver owns:
            nothing moves until a different reviewer approves it. Certifications the leaver granted are not changed.
          </p>
          <form className="own__form" onSubmit={onSubmit} noValidate>
            <div className="own__grid">
              <HintedField label="Leaving principal" hint="The person or group who is leaving. Matched exactly as it is stored.">
                {(describedBy) => (
                  <input
                    value={leaving}
                    onChange={(event) => setLeaving(event.target.value)}
                    placeholder="priya@tenant.example"
                    maxLength={255}
                    aria-describedby={describedBy}
                    autoComplete="off"
                  />
                )}
              </HintedField>
              <div className="own__fieldwrap">
                <Field label="Owner type they hold">
                  <select value={ownerType} onChange={(event) => setOwnerType(event.target.value as OwnershipOwnerType)}>
                    {OWNERSHIP_OWNER_TYPES.map((value) => (
                      <option key={value} value={value}>
                        {OWNER_TYPE_LABEL[value]}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              <HintedField
                label="Successor principal"
                hint={
                  successorName !== "" && successorName === leavingName
                    ? "The successor must be a different principal."
                    : "Who takes over. It is not checked against your identity provider, so type it carefully."
                }
              >
                {(describedBy) => (
                  <input
                    value={successor}
                    onChange={(event) => setSuccessor(event.target.value)}
                    placeholder="morgan@tenant.example"
                    maxLength={255}
                    aria-invalid={successorName !== "" && successorName === leavingName}
                    aria-describedby={describedBy}
                    autoComplete="off"
                  />
                )}
              </HintedField>
            </div>
            <HintedField label="Rationale" hint="At least 10 characters. It is recorded with the request and shown to the reviewer.">
              {(describedBy) => (
                <textarea
                  value={rationale}
                  rows={3}
                  maxLength={2000}
                  onChange={(event) => setRationale(event.target.value)}
                  aria-describedby={describedBy}
                />
              )}
            </HintedField>

            <div className="own__actions">
              {mayPreview ? (
                <Button
                  onClick={() => void runPreview()}
                  disabled={leavingName === "" || previewAction.submitting}
                  title="Reads the assignments listing and keeps what this principal holds. Nothing is requested."
                >
                  {previewAction.submitting ? "Reading assignments…" : "Preview what they hold"}
                </Button>
              ) : null}
            </div>
            {previewAction.error ? <FormError detail={previewAction.error} /> : null}

            {preview ? (
              <div className="own__preview" aria-label="Preview of the leaver's ownerships">
                <div className="own__resulthead">
                  <strong>
                    {preview.items.length === 0
                      ? `${leavingName} holds no active ${ownerType.toLowerCase()} ownerships${preview.complete ? "" : " among the assignments read"}`
                      : `${leavingName} holds ${preview.items.length} active ${ownerType.toLowerCase()} ownership${preview.items.length === 1 ? "" : "s"}`}
                  </strong>
                  <Pill tone={preview.complete ? "ok" : "warn"}>
                    {preview.complete
                      ? `read all ${preview.total} assignments`
                      : `read ${preview.scanned} of ${preview.total} assignments`}
                  </Pill>
                </div>
                {!preview.complete ? (
                  <p className="own__note">
                    The preview stops at {preview.scanned} assignments, so {leavingName} may hold more than this list. A request
                    that names no ids takes the whole portfolio from the server.
                  </p>
                ) : null}
                {preview.items.length === 0 ? (
                  <p className="own__note">
                    Check the spelling of the principal and the owner type. The server would refuse a request with nothing to
                    move.
                  </p>
                ) : (
                  <>
                    <p className="own__note">
                      Every row is ticked, which sends no ids and lets the server take everything. Untick a row to move only the
                      ones you leave ticked.
                    </p>
                    <div className="own__tablewrap" role="region" aria-label="Ownerships to move, scrollable" tabIndex={0}>
                      <table className="own__table" aria-label="Ownerships the leaver holds">
                        <thead>
                          <tr>
                            <th scope="col" className="own__check">
                              <input
                                type="checkbox"
                                checked={allChosen}
                                onChange={() =>
                                  setSelected(allChosen ? new Set() : new Set(preview.items.map((row) => row.id)))
                                }
                                aria-label="Move every ownership listed"
                              />
                            </th>
                            <th scope="col">Subject</th>
                            <th scope="col">How assigned</th>
                            <th scope="col">Expiry</th>
                          </tr>
                        </thead>
                        <tbody>
                          {preview.items.map((row) => {
                            const expiry = describeExpiry(row.expires_at);
                            return (
                              <tr key={row.id}>
                                <td className="own__check">
                                  <input
                                    type="checkbox"
                                    checked={selected.has(row.id)}
                                    onChange={() => toggle(row.id)}
                                    aria-label={`Move ${row.subject_type} ${row.subject_id}`}
                                  />
                                </td>
                                <td>
                                  <Pill tone="mute">{row.subject_type.toLowerCase()}</Pill>{" "}
                                  <code className="own__code">{row.subject_id}</code>
                                </td>
                                <td>{row.assignment_kind.toLowerCase()}</td>
                                <td>
                                  <Pill tone={expiry.tone}>{expiry.label}</Pill>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                    <p className="own__note" aria-live="polite">
                      {selected.size} of {preview.items.length} selected.
                    </p>
                  </>
                )}
              </div>
            ) : null}

            {problems.length > 0 && edited ? (
              <ul className="own__problems" aria-label="What is still needed">
                {problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            ) : null}
            <div className="own__actions">
              <Button type="submit" variant="primary" disabled={!valid}>
                Request reassignment…
              </Button>
            </div>
          </form>
        </section>
      ) : (
        <section className="own__panel" aria-label="Request a leaver reassignment">
          <div className="own__panelhead">
            <div>
              <p className="own__eyebrow">LEAVER REASSIGNMENT</p>
              <h2 className="own__h2">Move a leaver&rsquo;s ownerships to a successor</h2>
            </div>
          </div>
          <p className="own__note">
            This is a request, not a transfer: it asks a reviewer to make a successor the owner of what a leaver owns, and
            nothing moves until a different reviewer approves it.
          </p>
          {identityKnown ? (
            <p className="own__note">
              Requesting one needs {listOr(LEAVER_WRITE_ROLES)}. Your roles can read the requests below.
            </p>
          ) : null}
        </section>
      )}

      {outcome ? (
        <RequestedReview
          operation={outcome.operation}
          noun="ownerships"
          headline={`Review requested: ${outcome.leaving} → ${outcome.successor}`}
          remainder={describeRemainder(outcome.operation, outcome.leaving, outcome.preview)}
          onDismiss={() => setOutcome(null)}
        />
      ) : null}

      <OwnershipRequests organizationId={organizationId} kind="leaver" refreshKey={refreshKey} />

      {confirming ? (
        <OwnershipConfirm
          title={`Request that ${successorName} take over from ${leavingName}?`}
          summary="This asks for a review. It does not move anything yet."
          facts={[
            <>
              It asks to move {moves}. One request carries at most {LEAVER_REASSIGNMENT_MAX_ITEMS} ownerships: if there are
              more, the first {LEAVER_REASSIGNMENT_MAX_ITEMS} are included, the request records that there were more, and
              the rest stay where they are.
            </>,
            <>
              <strong>Nothing moves yet.</strong> A different reviewer has to approve it in the Review queue; you cannot
              approve your own request.
            </>,
            <>
              On approval each ownership is marked reassigned and {successorName} is recorded as its {ownerType.toLowerCase()}{" "}
              owner (an ownership {successorName} already held is made active again). Anything that changed between now and
              the approval is skipped.
            </>,
            <>
              Certifications {leavingName} granted are not changed: they record who certified a table, not who owns it.
            </>,
            <>The request is recorded in the audit ledger with your rationale.</>,
          ]}
          confirmLabel="Request review"
          busy={request.submitting}
          error={request.error}
          onConfirm={() => void confirmRequest()}
          onCancel={() => {
            request.reset();
            setConfirming(false);
          }}
        />
      ) : null}
    </div>
  );
}
