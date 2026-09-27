import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  BULK_REAFFIRM_MAX_ITEMS,
  bulkReaffirmOwnershipAssignments,
  describeLoadMoreFailure,
  fetchOwnershipAssignments,
  reaffirmOwnershipAssignment,
} from "../lib/api";
import type { OwnershipAssignmentBulkReaffirmResult, OwnershipAssignmentRead } from "../lib/api";
import type { PageOf } from "../lib/ui-types";
import { navigateTo } from "../lib/navigate";
import { useOrgId } from "../lib/org";
import { readDecision, roleAllows, roleHolds } from "../lib/roles";
import { CATALOG_ROWS_ROLES } from "../lib/searchTargets";
import { useSession } from "../lib/session";
import { useUrlState } from "../lib/useUrlState";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import { StatusStrip, useAsyncResource, useStatusChannel, useSubmitAction } from "../components/screenState";
import { NotApplicable, OwnershipConfirm, describeExpiry, listOr, stamp } from "./OwnershipParts";

/* ---------------------------------------------------------------------------
   Ownership -> Assignments (R11-AUD08, part 2).

   Who owns what, in this organization, and the two things a steward does about an
   ownership that is running out: reaffirm one, or reaffirm many. The client for all
   three routes already existed (`lib/api/catalog.ts`, P2-07) and had one consumer,
   the Work queue's "your ownerships are expiring" banner, which shows only the
   CURRENT PRINCIPAL's assignments inside the next 14 days. This is the whole list.

   WHAT THE LIST IS. `GET .../ownership-assignments`: ACTIVE assignments only,
   newest first, filterable by `subject_type` (matched ignoring capitals) and
   `subject_id` (exact). There is no owner filter and no expiry filter, so this screen
   does not offer one: a client-side filter over a paged list would silently cover
   only the pages already fetched, which is exactly the kind of answer that is worse
   than none. "Load more" says how much of the list is on screen.

   WHO MAY REAFFIRM WHAT. The route admits DataSteward, MetadataAdmin, PlatformAdmin
   and SemanticAdmin -- and then `_caller_may_reaffirm` narrows it: an assignment may
   be reaffirmed by its own owner, or by a PlatformAdmin or MetadataAdmin for anyone.
   A SemanticAdmin or DataSteward who is not the owner is refused with 403. So the
   control is offered only where BOTH hold; every other row says why it has none
   ("not yours") rather than showing a button that will be refused. The bulk path
   refuses the same rows item by item (`FORBIDDEN`), and selection is restricted to
   the rows the session could reaffirm so the request does not carry rows it knows
   will be skipped.

   WHAT REAFFIRMING DOES. It stamps `reaffirmed_at`/`reaffirmed_by`, moves
   `expires_at` to now plus the organization's reaffirmation period (180 days unless
   configured, 30 to 730), clears the expiry-warning stamp and is audited. It does
   not change the owner. One reaffirm is one click -- it is the owner's own
   attestation and is cheap to repeat. The BULK reaffirm goes through a confirmation,
   because it attests on behalf of many rows at once, up to 100.

   THE BULK RESULT IS PER ITEM. One item failing does not roll back the others
   (each is its own SAVEPOINT), so the answer is `reaffirmed` and `skipped` plus one
   outcome per id. The skipped ones are listed with the server's own detail; a
   summary that said "97 of 100" and dropped the three would leave the steward not
   knowing which.
--------------------------------------------------------------------------- */

/**
 * The roles `GET /v1/organizations/{organization_id}/ownership-assignments` admits.
 *
 * Copied from the surface-control matrix row for
 * `aida.stewardship_api.list_ownership_assignments`
 * (`Docs/50-security/surface-control-matrix.md`): Analyst, Auditor, DataAdmin,
 * DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer.
 */
const ASSIGNMENTS_READ_ROLES = [
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

/**
 * The roles `POST /v1/ownership-assignments/{assignment_id}/reaffirm` and
 * `POST /v1/ownership-assignments/bulk-reaffirm` admit.
 *
 * Copied from the matrix rows for `aida.stewardship_api.reaffirm_ownership_assignment`
 * and `bulk_reaffirm_ownership_assignments`: DataSteward, MetadataAdmin,
 * PlatformAdmin, SemanticAdmin.
 */
const REAFFIRM_ROLES = ["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"];

/**
 * The roles that may reaffirm an assignment they do NOT own.
 *
 * Not a route dependency, so not in the matrix: `_OWNERSHIP_ADMIN_ROLES` in
 * `aida.stewardship_api`, consulted by `_caller_may_reaffirm` after the route's own
 * role check has passed. Kept narrow on purpose in the handler -- "broadening this
 * would defeat the 'owner must actively re-attest' property" -- so it is not
 * widened here to match the four roles above.
 */
const REAFFIRM_ANY_OWNER_ROLES = ["MetadataAdmin", "PlatformAdmin"];

const PAGE = 100;

const unwrap = (row: OwnershipAssignmentRead): string => `${row.subject_type} ${row.subject_id}`;

/** What the bulk result says about the rows that were not reaffirmed. */
function BulkResult({
  result,
  labels,
  onDismiss,
}: {
  result: OwnershipAssignmentBulkReaffirmResult;
  labels: ReadonlyMap<string, string>;
  onDismiss: () => void;
}) {
  const skipped = result.items.filter((item) => item.outcome !== "REAFFIRMED");
  return (
    <section className="own__result" role="status" aria-label="Bulk reaffirm result">
      <div className="own__resulthead">
        <strong>
          {result.reaffirmed} reaffirmed, {result.skipped} skipped
        </strong>
        <Pill tone={result.skipped === 0 ? "ok" : "warn"}>{result.skipped === 0 ? "all done" : "some skipped"}</Pill>
      </div>
      {skipped.length > 0 ? (
        <ul className="own__skipped" aria-label="Skipped assignments">
          {skipped.map((item) => (
            <li key={item.assignment_id}>
              <code className="own__code">{labels.get(item.assignment_id) ?? item.assignment_id}</code>{" "}
              <Pill tone="bad">{item.outcome.toLowerCase().replace(/_/g, " ")}</Pill>{" "}
              {item.detail ?? "The server gave no detail."}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="own__actions">
        <Button onClick={onDismiss}>Dismiss</Button>
      </div>
    </section>
  );
}

export function OwnershipAssignments() {
  const organizationId = useOrgId();
  const session = useSession();
  const roles = session.me?.roles;
  const principalId = session.me?.principal_id;
  // Held while `/v1/me` is in flight, and never sent for a session outside the list.
  const read = readDecision(session, ASSIGNMENTS_READ_ROLES);
  // Controls fail closed: offered only to a session KNOWN to hold a reaffirming role.
  const mayReaffirm = roleHolds(roles, REAFFIRM_ROLES);
  const mayReaffirmAny = roleHolds(roles, REAFFIRM_ANY_OWNER_ROLES);
  // A table's "Catalog" link is offered only where the Catalog list would answer: `CATALOG_ROWS_ROLES`
  // (Analyst, MetadataAdmin, PlatformAdmin, Viewer) is narrower than this list's nine roles, and a
  // DataSteward, Auditor, DataAdmin, Reviewer or SemanticAdmin who followed it would land on a refusal.
  // Decided exactly as Search decides it (`roleAllows`): a link is not a request, so it is not held
  // back while `/v1/me` is in flight.
  const mayOpenCatalog = roleAllows(roles, CATALOG_ROWS_ROLES);
  const identityKnown = roles !== undefined;

  const [params, setParams] = useUrlState();
  const subjectType = params.get("subject_type") ?? "";
  const subjectId = params.get("subject_id") ?? "";
  const [draftType, setDraftType] = useState(subjectType);
  const [draftId, setDraftId] = useState(subjectId);
  const filtered = subjectType !== "" || subjectId !== "";

  // Rows read so far, counted as the server sent them. Not `rows.length`: a row repeated across a page
  // boundary is dropped from `rows` (below), and paging by the deduplicated length would ask for the
  // same page again.
  const [fetched, setFetched] = useState(0);

  /* "Load more" continues ONE reading of the list: this filter, from this first page. A reply that
     arrives after the filter (or the organization) changed, or after the first page was read again,
     belongs to a list that is no longer on screen -- appended, it put the old filter's rows under the
     new one and counted them into the next offset. So each page request carries the generation it
     continues, and anything that starts a new reading moves the generation on and aborts the page
     request in flight (the same ticket-and-abort pair `useAsyncResource` uses for the first page). */
  const [moreBusy, setMoreBusy] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  const moreGeneration = useRef(0);
  const moreInflight = useRef<AbortController | null>(null);
  const supersedeMore = useCallback(() => {
    moreGeneration.current += 1;
    moreInflight.current?.abort();
    moreInflight.current = null;
    setMoreBusy(false);
    setMoreError(null);
  }, []);
  useEffect(() => supersedeMore(), [organizationId, subjectType, subjectId, supersedeMore]);
  useEffect(
    () => () => {
      moreGeneration.current += 1;
      moreInflight.current?.abort();
    },
    [],
  );

  const list = useAsyncResource<PageOf<OwnershipAssignmentRead>>(
    (signal) =>
      fetchOwnershipAssignments(
        organizationId,
        { subject_type: subjectType || null, subject_id: subjectId || null, limit: PAGE },
        signal,
      ),
    [organizationId, subjectType, subjectId],
    {
      enabled: read === "ask",
      onLoad: (page) => {
        // A fresh first page: whatever "Load more" was continuing is not what is on screen now.
        supersedeMore();
        setFetched(page.items.length);
      },
    },
  );
  const rows = useMemo(() => list.data?.items ?? [], [list.data]);
  const total = list.data?.total ?? 0;

  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());

  const loadMore = async () => {
    if (!list.data || moreInflight.current) return;
    const generation = moreGeneration.current;
    const controller = new AbortController();
    moreInflight.current = controller;
    setMoreBusy(true);
    setMoreError(null);
    try {
      const next = await fetchOwnershipAssignments(
        organizationId,
        {
          subject_type: subjectType || null,
          subject_id: subjectId || null,
          limit: PAGE,
          offset: fetched,
        },
        controller.signal,
      );
      if (generation !== moreGeneration.current) return; // a page of a list no longer on screen
      setFetched((count) => count + next.items.length);
      // The listing pages by creation time and rows created together share one, so a page
      // boundary can repeat a row. Each id is kept once.
      list.setData((previous) => {
        if (!previous) return previous;
        const seen = new Set(previous.items.map((row) => row.id));
        return { ...next, items: [...previous.items, ...next.items.filter((row) => !seen.has(row.id))] };
      });
    } catch (failure) {
      // Superseded (and so aborted): the failure is about a list no longer on screen.
      if (generation !== moreGeneration.current) return;
      setMoreError(describeLoadMoreFailure(failure));
    } finally {
      if (moreInflight.current === controller) moreInflight.current = null;
      if (generation === moreGeneration.current) setMoreBusy(false);
    }
  };

  const applyFilter = (event: FormEvent) => {
    event.preventDefault();
    setSelected(new Set());
    setParams({ subject_type: draftType.trim() || null, subject_id: draftId.trim() || null });
  };
  const clearFilter = () => {
    setDraftType("");
    setDraftId("");
    setSelected(new Set());
    setParams({ subject_type: null, subject_id: null });
  };

  // Whether THIS session could reaffirm this row: a reaffirming role AND (owner, or an admin role).
  const reaffirmable = (row: OwnershipAssignmentRead): boolean =>
    mayReaffirm && (mayReaffirmAny || (principalId !== undefined && row.owner_principal === principalId));

  const status = useStatusChannel();
  const [busyId, setBusyId] = useState<string | null>(null);
  const reaffirmOne = async (row: OwnershipAssignmentRead) => {
    if (busyId !== null) return;
    setBusyId(row.id);
    status.clear();
    try {
      const updated = await reaffirmOwnershipAssignment(row.id);
      // The server's own row replaces ours: the new expiry is its answer, not a guess of ours.
      list.setData((previous) =>
        previous ? { ...previous, items: previous.items.map((item) => (item.id === updated.id ? updated : item)) } : previous,
      );
      status.success(`Reaffirmed ${unwrap(row)}; ${describeExpiry(updated.expires_at).label}.`);
    } catch (failure) {
      status.failure(failure);
    } finally {
      setBusyId(null);
    }
  };

  const chosen = rows.filter((row) => selected.has(row.id) && reaffirmable(row));
  const eligibleOnPage = rows.filter(reaffirmable);
  const toggle = (id: string) =>
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allChosen = eligibleOnPage.length > 0 && eligibleOnPage.slice(0, BULK_REAFFIRM_MAX_ITEMS).every((row) => selected.has(row.id));
  const toggleAll = () =>
    setSelected(allChosen ? new Set() : new Set(eligibleOnPage.slice(0, BULK_REAFFIRM_MAX_ITEMS).map((row) => row.id)));

  const [confirming, setConfirming] = useState(false);
  const bulk = useSubmitAction<OwnershipAssignmentBulkReaffirmResult>();
  const [bulkResult, setBulkResult] = useState<{
    result: OwnershipAssignmentBulkReaffirmResult;
    labels: Map<string, string>;
  } | null>(null);
  const confirmBulk = async () => {
    const targets = chosen;
    const result = await bulk.run(() => bulkReaffirmOwnershipAssignments(targets.map((row) => row.id)));
    if (result === null) return; // the refusal is in `bulk.error`, shown in the dialog
    bulk.reset();
    setConfirming(false);
    setBulkResult({ result, labels: new Map(targets.map((row) => [row.id, unwrap(row)] as const)) });
    setSelected(new Set());
    // The server's rows, not our edit of them: the expiries just moved.
    list.reload();
  };

  return (
    <div className="own__body">
      <section className="own__panel" aria-label="Ownership assignments">
        <div className="own__panelhead">
          <div>
            <p className="own__eyebrow">OWNERSHIP ASSIGNMENTS</p>
            <h2 className="own__h2">Who owns what</h2>
          </div>
          {read === "ask" && list.data ? (
            <Pill tone="mute">
              {rows.length} of {total} shown
            </Pill>
          ) : null}
        </div>
        <p className="own__note">
          Active assignments only, newest first. A person or group can own a table beside others; an assignment past its
          expiry stays active until the daily sweep lapses it, unless its owner reaffirms it first.
        </p>

        <form className="own__filters" onSubmit={applyFilter}>
          <Field label="Subject type">
            <input
              type="text"
              list="own-subject-types"
              value={draftType}
              placeholder="TABLE"
              onChange={(event) => setDraftType(event.target.value)}
            />
          </Field>
          <datalist id="own-subject-types">
            <option value="TABLE" />
            <option value="TERM" />
          </datalist>
          <Field label="Subject id">
            <input
              type="text"
              value={draftId}
              placeholder="exact id"
              onChange={(event) => setDraftId(event.target.value)}
            />
          </Field>
          <Button type="submit" disabled={draftType.trim() === subjectType && draftId.trim() === subjectId}>
            Apply filter
          </Button>
          {filtered || draftType || draftId ? <Button onClick={clearFilter}>Clear filter</Button> : null}
        </form>

        <StatusStrip status={status.status} />
        {bulkResult ? (
          <BulkResult result={bulkResult.result} labels={bulkResult.labels} onDismiss={() => setBulkResult(null)} />
        ) : null}

        {read === "skip" ? (
          <NotApplicable what="ownership assignments" roles={ASSIGNMENTS_READ_ROLES} />
        ) : read === "wait" || (list.loading && !list.data) ? (
          <p className="own__note" role="status">
            Loading ownership assignments…
          </p>
        ) : list.error ? (
          <ErrorState title="Ownership assignments could not be loaded" detail={list.error} onRetry={list.reload} />
        ) : rows.length === 0 ? (
          <Empty
            title={filtered ? "No active ownership matches this filter" : "No active ownership assignments"}
            hint={
              filtered
                ? "The subject type is matched ignoring capitals; the subject id is matched exactly. Clear the filter to see every assignment."
                : "Nothing in this organization has an owner yet. An applied ownership rule or a bulk assignment creates them."
            }
          />
        ) : (
          <>
            {mayReaffirm && !mayReaffirmAny ? (
              <p className="own__note">
                You can reaffirm the assignments you own. A PlatformAdmin or MetadataAdmin can reaffirm any of them.
              </p>
            ) : null}
            {!mayReaffirm && identityKnown ? (
              <p className="own__note">
                Your roles can read ownership. Reaffirming needs {listOr(REAFFIRM_ROLES)}, and then only for an assignment you
                own unless you are a PlatformAdmin or MetadataAdmin.
              </p>
            ) : null}
            {mayReaffirm ? (
              <div className="own__bulkbar">
                <span aria-live="polite">
                  {chosen.length} selected
                  {eligibleOnPage.length > BULK_REAFFIRM_MAX_ITEMS
                    ? ` (the API takes at most ${BULK_REAFFIRM_MAX_ITEMS} at a time)`
                    : ""}
                </span>
                <Button
                  variant="primary"
                  disabled={chosen.length === 0}
                  onClick={() => {
                    bulk.reset();
                    setConfirming(true);
                  }}
                >
                  Reaffirm selected
                </Button>
              </div>
            ) : null}
            {/* A region that scrolls has to be reachable by keyboard (WCAG 2.1.1), including when nothing in it
                is focusable -- a list of glossary terms has no Catalog link and, for a reader, no checkbox. */}
            <div className="own__tablewrap" role="region" aria-label="Assignments table, scrollable" tabIndex={0} aria-busy={list.loading}>
              <table className="own__table" aria-label="Ownership assignments">
                <thead>
                  <tr>
                    {mayReaffirm ? (
                      <th scope="col" className="own__check">
                        <input
                          type="checkbox"
                          checked={allChosen}
                          disabled={eligibleOnPage.length === 0}
                          onChange={toggleAll}
                          aria-label="Select every assignment you can reaffirm on this page"
                        />
                      </th>
                    ) : null}
                    <th scope="col">Subject</th>
                    <th scope="col">Owner</th>
                    <th scope="col">How assigned</th>
                    <th scope="col">Expiry</th>
                    <th scope="col">Last reaffirmed</th>
                    {mayReaffirm ? <th scope="col">Reaffirm</th> : null}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const expiry = describeExpiry(row.expires_at);
                    const mine = reaffirmable(row);
                    return (
                      <tr key={row.id}>
                        {mayReaffirm ? (
                          <td className="own__check">
                            <input
                              type="checkbox"
                              checked={selected.has(row.id)}
                              disabled={!mine || (!selected.has(row.id) && chosen.length >= BULK_REAFFIRM_MAX_ITEMS)}
                              onChange={() => toggle(row.id)}
                              aria-label={`Select ${unwrap(row)}`}
                            />
                          </td>
                        ) : null}
                        <td>
                          <Pill tone="mute">{row.subject_type.toLowerCase()}</Pill>{" "}
                          <code className="own__code">{row.subject_id}</code>
                          {row.subject_type === "TABLE" && mayOpenCatalog ? (
                            <>
                              {" "}
                              <button
                                type="button"
                                className="own__link"
                                onClick={() => navigateTo("catalog", { asset: row.subject_id })}
                              >
                                Catalog<span className="sr-only"> entry for {row.subject_id}</span>
                              </button>
                            </>
                          ) : null}
                        </td>
                        <td>
                          {row.owner_principal} <Pill tone="mute">{row.owner_type.toLowerCase()}</Pill>
                        </td>
                        <td>
                          {row.assignment_kind.toLowerCase()}
                          {row.source_rule_id ? <span className="own__muted"> (by a rule)</span> : null}
                          <span className="own__muted"> · assigned by {row.assigned_by}</span>
                        </td>
                        <td>
                          <Pill tone={expiry.tone}>{expiry.label}</Pill>
                        </td>
                        <td>
                          {row.reaffirmed_at ? (
                            <>
                              {stamp(row.reaffirmed_at)}
                              <span className="own__muted"> by {row.reaffirmed_by ?? "an unknown principal"}</span>
                            </>
                          ) : (
                            <span className="own__muted">never</span>
                          )}
                        </td>
                        {mayReaffirm ? (
                          <td>
                            {mine ? (
                              <Button disabled={busyId !== null} onClick={() => void reaffirmOne(row)}>
                                {busyId === row.id ? "Reaffirming…" : "Reaffirm"}
                                <span className="sr-only"> {unwrap(row)}</span>
                              </Button>
                            ) : (
                              <span
                                className="own__muted"
                                title="Only the owner, a PlatformAdmin or a MetadataAdmin can reaffirm this assignment."
                              >
                                not yours
                              </span>
                            )}
                          </td>
                        ) : null}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {fetched < total ? (
              <div className="own__more">
                <Button onClick={() => void loadMore()} disabled={moreBusy}>
                  {moreBusy ? "Loading…" : `Load more (${rows.length} of ${total} shown)`}
                </Button>
                {moreError ? (
                  <p className="own__err" role="alert">
                    {moreError}
                  </p>
                ) : null}
              </div>
            ) : null}
          </>
        )}
      </section>

      {confirming ? (
        <OwnershipConfirm
          title={`Reaffirm ${chosen.length} ownership${chosen.length === 1 ? "" : "s"}?`}
          summary="You attest that each owner still owns the asset it names."
          facts={[
            <>
              Each expiry moves to the organization&rsquo;s reaffirmation period from now (180 days unless it was
              configured differently). The owners do not change.
            </>,
            <>
              Each item stands on its own: one that the server refuses is skipped and listed afterwards with its reason,
              and the others are still reaffirmed.
            </>,
            <>The change is recorded in the audit ledger, one entry per assignment.</>,
          ]}
          confirmLabel="Reaffirm"
          busy={bulk.submitting}
          error={bulk.error}
          onConfirm={() => void confirmBulk()}
          onCancel={() => {
            bulk.reset();
            setConfirming(false);
          }}
        />
      ) : null}
    </div>
  );
}

