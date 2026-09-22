import { useCallback, useEffect, useMemo, useRef, useState } from "react";
/* Filters and selection live in the URL so a filtered view is shareable and
   survives Back/Forward. This screen carried a verbatim copy of the old hook
   -- a `useState` seeded once from `location.search`, subscribed to nothing --
   so its idea of the selection and the address bar drifted apart the first
   time either the Back button or a same-screen link was used (review
   2026-09-05, F09 - R07). The shared hook reads one location store. */
import { useUrlState } from "../lib/useUrlState";
import type {
  StudioChangeItemRead,
  StudioChangeSetRead,
  StudioConflict,
  StudioDiffRead,
  StudioImpactPreview,
  StudioTestResultRead,
} from "../lib/types";
import {
  ApiError,
  fetchStudioChangeSetItems,
  fetchStudioChangeSets,
  fetchStudioDiff,
  fetchStudioImpact,
  submitStudioChangeSet,
} from "../lib/api";
import { readDecision, roleHolds } from "../lib/roles";
import { useSession } from "../lib/session";
import { VirtualList } from "../components/VirtualList";
import { Button, CopyLinkButton, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import { StatusStrip, useStatusChannel } from "../components/screenState";
import { AddItemDialog, NewChangeSetDialog, RemoveItemDialog } from "./StudioAuthoring";
import { ItemCheck, hasCheck } from "./StudioChecks";
import { EvalQuestionsDialog, EvalRunSection } from "./StudioEval";
import { ConflictsView, DetectConflictsDialog, RunTestsDialog, TestResultView } from "./StudioVerify";
import { ITEMS_EDITABLE_STATUS, STUDIO_READ_ROLES, STUDIO_WRITE_ROLES, TESTABLE_STATUSES, listOr } from "./studioRoles";
import "../components/EvidencePane.css";
import "./StudioChangeSetsScreen.css";

/* ---------------------------------------------------------------------------
   Studio change sets — UX-15, the Catalog pattern applied to the real
   authoring-environment API (`studio_api.py`, module 19 / ST-A7).

     1. URL state       status filter, `cs` (the focused change set, permalinkable)
     2. abortable fetch  one in-flight list request per view
     3. virtualization   `VirtualList`
     4. evidence pane    a change set's own items/diff/impact, fetched
                         real-time from `.../items`, `.../diff`, `.../impact`

   Submission calls the real `POST .../submit` -- the test-gated (ST-A7) and
   eval-regression-gated (ST-A8) path that materializes any CONTEXT_PRODUCT
   item into the same `GovernanceReview` queue `ReviewQueueScreen` reads, not
   a client-side status flip. A 409 from that gate (untested items, a
   regressed eval question) renders as the endpoint's own detail string,
   exactly like every other governed write this shell makes.

   AUTHORING (R11-AUD08). This screen used to read change sets and submit them;
   nothing in the UI could create one, so the "Create one from the Studio
   authoring surface" the empty state pointed at did not exist. It now does the
   rest of the lifecycle the API has: New change set, Add item and Remove item
   (DRAFT only), Run tests, Detect conflicts, the eval run, the mined eval
   questions and the two definition checks. Where each control appears, and to
   whom:

     - THE WRITE ROLES (DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin)
       get the controls, and only when the session is KNOWN to hold one
       (`roleHolds`, fail-closed): nothing that writes is offered on a guess while
       `/v1/me` is in flight. That includes Submit, which was offered to everyone
       and answered a Viewer's click with a 403.
     - THE STATUS decides what a write role is offered. Items can be added or
       removed only while DRAFT (`add_item`, `remove_item` answer 409 otherwise);
       tests and submission are accepted in DRAFT or TESTING; a SUBMITTED, MERGED or
       REJECTED change set is read-only. Running the tests moves DRAFT to TESTING
       and there is no way back, so it is also what locks the items.
     - EVERY READ THE API ADMITS (adding the Analyst, Auditor, Reviewer and Viewer
       roles) is held while identity is in flight and never sent to a session known
       to be outside the list (`readDecision`).

   After every write the screen RE-READS what the write changed instead of
   editing local state: what it shows is what the API holds. That also makes a
   write the API acknowledged but did not keep visible -- see `expectAfterLoad`.
--------------------------------------------------------------------------- */

const statusTone = (s: string): Tone =>
  s === "MERGED" ? "ok" : s === "SUBMITTED" ? "info" : s === "REJECTED" ? "bad" : s === "TESTING" ? "warn" : "mute";

const itemTone = (testStatus: string): "ok" | "bad" | "info" =>
  testStatus === "PASSED" ? "ok" : testStatus === "FAILED" ? "bad" : "info";

function ChangeSetRow({
  cs,
  selected,
  onSelect,
}: {
  cs: StudioChangeSetRead;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <article className={`csrow${selected ? " csrow--sel" : ""}`} aria-label={cs.name}>
      <button className="csrow__click" onClick={onSelect}>
        <div className="csrow__badges">
          <Pill tone={statusTone(cs.status)}>{cs.status.toLowerCase()}</Pill>
          {cs.conflict_status !== "CLEAN" ? <Pill tone="bad">{cs.conflict_status.toLowerCase()}</Pill> : null}
        </div>
        <h3 className="csrow__title">{cs.name}</h3>
        <div className="csrow__meta">
          <span>{cs.author}</span>
          <span>·</span>
          <time dateTime={cs.updated_at}>{cs.updated_at.slice(0, 10)}</time>
        </div>
      </button>
    </article>
  );
}

type DetailDialog = "add" | "test" | "conflicts" | null;

function ChangeSetDetail({ cs, onChanged }: { cs: StudioChangeSetRead; onChanged: () => void }) {
  const session = useSession();
  // Every control that writes is offered only to a session KNOWN to hold a write role
  // (matrix rows for `add_item`, `remove_item`, `run_tests`, `detect_conflicts_endpoint`,
  // `submit_change_set`: `STUDIO_WRITE_ROLES`). Fail-closed: `roleHolds`, not `roleAllows`.
  const mayWrite = roleHolds(session.me?.roles, STUDIO_WRITE_ROLES);
  // "Needs DataSteward..." is a statement about THIS session, so it waits for identity:
  // said while `/v1/me` is in flight it would tell a steward-to-be they cannot.
  const identityKnown = session.me?.roles !== undefined;

  const [items, setItems] = useState<StudioChangeItemRead[] | null>(null);
  const [diff, setDiff] = useState<StudioDiffRead | null>(null);
  const [impact, setImpact] = useState<StudioImpactPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const [dialog, setDialog] = useState<DetailDialog>(null);
  const [removing, setRemoving] = useState<StudioChangeItemRead | null>(null);
  const [testResult, setTestResult] = useState<StudioTestResultRead | null>(null);
  const [conflicts, setConflicts] = useState<{ list: StudioConflict[]; suppliedKeys: number } | null>(null);
  const [evalTick, setEvalTick] = useState(0);
  const channel = useStatusChannel();
  const { success, failure } = channel;

  const loadAll = useCallback(
    async (signal: AbortSignal): Promise<StudioChangeItemRead[]> => {
      const [i, d, imp] = await Promise.all([
        fetchStudioChangeSetItems(cs.id, signal),
        fetchStudioDiff(cs.id, signal),
        fetchStudioImpact(cs.id, signal),
      ]);
      setItems(i);
      setDiff(d);
      setImpact(imp);
      return i;
    },
    [cs.id],
  );

  // The first read. The screen keys this component by change-set id, so a different
  // selection is a fresh mount and starts from `null` -- there is no stale pane to reset.
  useEffect(() => {
    const ac = new AbortController();
    setError(null);
    loadAll(ac.signal).catch((e: unknown) => {
      if ((e as Error)?.name === "AbortError") return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    });
    return () => ac.abort();
  }, [loadAll]);

  // A re-read after a write. Kept apart from the first read so a failed refresh says so on the
  // status strip and leaves the pane as it was, instead of replacing everything with an error.
  const refreshing = useRef<AbortController | null>(null);
  useEffect(() => () => refreshing.current?.abort(), []);
  const refresh = useCallback(async (): Promise<StudioChangeItemRead[] | null> => {
    refreshing.current?.abort();
    const ac = new AbortController();
    refreshing.current = ac;
    try {
      return await loadAll(ac.signal);
    } catch (e) {
      if ((e as Error)?.name !== "AbortError") failure(e);
      return null;
    }
  }, [loadAll, failure]);

  const submit = useCallback(async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await submitStudioChangeSet(cs.id);
      onChanged();
    } catch (e) {
      setSubmitError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [cs.id, onChanged]);

  const afterAdd = async (added: StudioChangeItemRead) => {
    setDialog(null);
    const fresh = await refresh();
    if (fresh === null) return;
    // The API answered 201 with the item. If the list it then returns does not hold it, the
    // write was acknowledged and not kept, and saying "Added" would be the screen's invention.
    if (!fresh.some((item) => item.id === added.id)) {
      failure(
        `The API accepted ${added.object_type} ${added.object_id} but it is not in this change set's item list. It may not have been saved; reload to check.`,
      );
    } else {
      success(`Added ${added.object_type} ${added.object_id} (${added.operation.toLowerCase()}).`);
    }
  };

  const afterRemove = async (removed: StudioChangeItemRead) => {
    setRemoving(null);
    const fresh = await refresh();
    if (fresh === null) return;
    if (fresh.some((item) => item.id === removed.id)) {
      failure(
        `The API accepted the removal of ${removed.object_type} ${removed.object_id} but the item is still listed. It may not have been saved; reload to check.`,
      );
    } else {
      success(`Removed ${removed.object_type} ${removed.object_id}.`);
    }
  };

  const afterTests = (result: StudioTestResultRead) => {
    setDialog(null);
    channel.clear(); // an earlier "Added ..." is not what the pane is about to show
    setTestResult(result);
    setEvalTick((tick) => tick + 1);
    void refresh(); // each item's PASSED/FAILED is read back, not inferred from the totals
    onChanged(); // the change set's own status moved (DRAFT -> TESTING): re-read the list
  };

  const afterConflicts = (list: StudioConflict[], suppliedKeys: number) => {
    setDialog(null);
    channel.clear();
    setConflicts({ list, suppliedKeys });
    onChanged(); // the change set's conflict status was recorded: re-read the list
  };

  const canSubmit = TESTABLE_STATUSES.includes(cs.status);
  // Items: DRAFT only, and only for a write role. Tests and conflicts: while it can still be submitted.
  const canEditItems = mayWrite && cs.status === ITEMS_EDITABLE_STATUS;
  const canVerify = mayWrite && canSubmit;

  return (
    <aside className="evp" aria-label={`Detail for ${cs.name}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={cs.name}>{cs.name}</div>
          <div className="evp__path">{cs.author} · {cs.status.toLowerCase()}</div>
        </div>
      </header>
      <div className="evp__body">
        <StatusStrip status={channel.status} />
        {error ? (
          <div className="evp__error" role="alert">{error}</div>
        ) : items === null ? (
          <div className="evp__load" role="status">Loading change set…</div>
        ) : (
          <>
            {canEditItems || canVerify ? (
              <div className="cs__actions" role="group" aria-label="Authoring actions">
                {canEditItems ? <Button onClick={() => setDialog("add")}>Add item</Button> : null}
                {canVerify ? <Button onClick={() => setDialog("test")}>Run tests</Button> : null}
                {canVerify ? <Button onClick={() => setDialog("conflicts")}>Detect conflicts</Button> : null}
              </div>
            ) : null}
            {mayWrite && !canEditItems ? (
              <p className="cs__none cs__locked">
                Items are locked: they can only be added or removed while a change set is {ITEMS_EDITABLE_STATUS}. This one is{" "}
                {cs.status.toLowerCase()}.
              </p>
            ) : null}

            <div className="evp__sub">Items ({items.length})</div>
            {items.length === 0 ? <p className="cs__none">This change set has no items yet.</p> : null}
            <ol className="evl">
              {items.map((it) => (
                <li key={it.id} className={`evi evi--${itemTone(it.test_status)}`}>
                  <div className="evi__label">{it.object_type} · {it.operation}</div>
                  <div className="evi__value">{it.object_id}</div>
                  <div className="evi__source">test status: {it.test_status.toLowerCase()}</div>
                  {canEditItems || hasCheck(it) ? (
                    <div className="cs__itemactions">
                      {canEditItems ? (
                        <Button onClick={() => setRemoving(it)}>
                          Remove<span className="sr-only"> {it.object_type} {it.object_id}</span>
                        </Button>
                      ) : null}
                      <ItemCheck item={it} />
                    </div>
                  ) : null}
                </li>
              ))}
            </ol>

            {testResult ? <TestResultView result={testResult} /> : null}
            {conflicts ? <ConflictsView conflicts={conflicts.list} suppliedKeys={conflicts.suppliedKeys} /> : null}
            <EvalRunSection changeSet={cs} refreshKey={evalTick} />

            <div className="evp__sub" style={{ marginTop: 14 }}>
              Impact ({impact?.affected_object_count ?? 0} affected)
            </div>
            {impact && impact.affected_objects.length > 0 ? (
              <pre className="cs__pre">{JSON.stringify(impact.affected_objects, null, 2)}</pre>
            ) : (
              <p className="cs__none">No downstream impact detected.</p>
            )}

            <div className="evp__sub" style={{ marginTop: 14 }}>Diff</div>
            {diff && diff.items.length > 0 ? (
              <pre className="cs__pre">{JSON.stringify(diff.items, null, 2)}</pre>
            ) : (
              <p className="cs__none">No diff recorded.</p>
            )}
          </>
        )}
      </div>
      <footer className="evp__foot" style={{ flexWrap: "wrap", gap: 8 }}>
{/* The copied link names the screen that resolves this selection.
            Built as `origin + pathname + '?' + id` it carried no `#/studio`,
            so a fresh tab landed on the persona default and the id was read by
            nobody (review 2026-09-05, F08). */}
        <CopyLinkButton target={{ screen: "studio", params: { cs: cs.id } }} />
        {canSubmit ? (
          mayWrite ? (
            <Button variant="primary" disabled={submitting} onClick={() => void submit()}>
              {submitting ? "Submitting…" : "Submit for review"}
            </Button>
          ) : identityKnown ? (
            <span className="evp__hint">read-only for your roles — editing and submitting need {listOr(STUDIO_WRITE_ROLES)}</span>
          ) : null
        ) : (
          <span className="evp__hint">{cs.status.toLowerCase()} — nothing left to submit</span>
        )}
        {submitError ? <div className="cs__submiterr" role="alert">{submitError}</div> : null}
      </footer>

      {dialog === "add" ? (
        <AddItemDialog changeSet={cs} onClose={() => setDialog(null)} onAdded={(added) => void afterAdd(added)} />
      ) : null}
      {dialog === "test" ? (
        <RunTestsDialog changeSet={cs} onClose={() => setDialog(null)} onDone={afterTests} />
      ) : null}
      {dialog === "conflicts" && items ? (
        <DetectConflictsDialog changeSet={cs} items={items} onClose={() => setDialog(null)} onDone={afterConflicts} />
      ) : null}
      {removing ? (
        <RemoveItemDialog
          changeSet={cs}
          item={removing}
          onClose={() => setRemoving(null)}
          onRemoved={(removed) => void afterRemove(removed)}
        />
      ) : null}
    </aside>
  );
}

export function StudioChangeSetsScreen() {
  const [params, setParams] = useUrlState();
  const statusFilter = params.get("status") ?? "ALL";
  const selectedId = params.get("cs");

  const session = useSession();
  // The list and every read behind the detail pane share one role list (matrix rows for
  // `list_change_sets`, `list_items`, `view_diff`, `impact_preview`, `get_latest_eval_run`).
  // `readDecision` holds the request while `/v1/me` is in flight and never sends it for a
  // session known to be outside the list: it used to be sent unconditionally, and a role with
  // no Studio read took a 403 on every load.
  const read = readDecision(session, STUDIO_READ_ROLES);
  const mayWrite = roleHolds(session.me?.roles, STUDIO_WRITE_ROLES);

  const [items, setItems] = useState<StudioChangeSetRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"new" | "eval" | null>(null);
  const channel = useStatusChannel();
  const { failure } = channel;

  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);
  // A change set the API has just acknowledged creating. The NEXT list read that completes
  // must contain it; if it does not, the create was answered 201 and not kept, and the screen
  // says so instead of selecting a row that is not there. Set before the read it depends on is
  // started, so no earlier read can complete in between (starting one supersedes it).
  const expectAfterLoad = useRef<{ id: string; name: string } | null>(null);

  /** `quiet` re-reads without swapping the list for a skeleton: the rows stay while they refresh. */
  const load = useCallback(async (quiet = false) => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    if (!quiet) setLoading(true);
    setError(null);
    try {
      const rows = await fetchStudioChangeSets(
        { status: statusFilter !== "ALL" ? statusFilter : null, limit: 200 },
        ac.signal,
      );
      if (seq !== reqSeq.current) return;
      setItems(rows);
      const expected = expectAfterLoad.current;
      expectAfterLoad.current = null;
      if (expected && !rows.some((row) => row.id === expected.id)) {
        failure(
          `The API accepted “${expected.name}” (${expected.id}) but the change set list does not include it. It may not have been saved; reload to check.`,
        );
      }
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [statusFilter, failure]);

  useEffect(() => {
    if (read !== "ask") return;
    void load();
    return () => inflight.current?.abort();
  }, [load, read]);

  const selected = useMemo(() => items.find((cs) => cs.id === selectedId) ?? null, [items, selectedId]);

  const onCreated = (created: StudioChangeSetRead) => {
    setDialog(null);
    channel.success(`Created “${created.name}” as a draft.`);
    expectAfterLoad.current = { id: created.id, name: created.name };
    if (statusFilter !== "ALL") {
      // A DRAFT would not show under the filter the author was looking at: clear it, and the
      // filter change re-reads the list.
      setParams({ status: null, cs: created.id });
    } else {
      setParams({ cs: created.id });
      void load(true);
    }
  };

  return (
    <div className="csscreen">
      <header className="csscreen__head csscreen__head--row">
        <div>
          <h1 className="csscreen__h1">Studio change sets</h1>
          <p className="csscreen__lede">
            Draft, test and submit governed changes to metrics, tools, terms and context
            products — DRAFT → TESTING → SUBMITTED → MERGED/REJECTED.
          </p>
        </div>
        {read === "ask" ? (
          <div className="csscreen__actions">
            <Button onClick={() => setDialog("eval")}>Eval questions</Button>
            {mayWrite ? (
              <Button variant="primary" onClick={() => setDialog("new")}>New change set</Button>
            ) : null}
          </div>
        ) : null}
      </header>

      {read === "skip" ? (
        <Empty
          title="Studio is not available to your roles"
          hint={`Only sessions holding ${listOr(STUDIO_READ_ROLES)} can read change sets.`}
        />
      ) : (
        <>
          <div className="csscreen__filters">
            <Field label="Status">
              <select value={statusFilter} onChange={(e) => setParams({ status: e.target.value === "ALL" ? null : e.target.value, cs: null })}>
                <option value="ALL">All</option>
                <option value="DRAFT">Draft</option>
                <option value="TESTING">Testing</option>
                <option value="SUBMITTED">Submitted</option>
                <option value="MERGED">Merged</option>
                <option value="REJECTED">Rejected</option>
              </select>
            </Field>
          </div>

          <StatusStrip status={channel.status} />

          <div className="csscreen__main">
            {error ? (
              <ErrorState title="Studio change sets could not be loaded" detail={error} onRetry={() => void load()} />
            ) : loading ? (
              <div className="csscreen__skeleton" role="status" aria-live="polite">
                Loading change sets…
              </div>
            ) : (
              <VirtualList
                items={items}
                getKey={(cs) => cs.id}
                ariaLabel="Studio change sets"
                estimateSize={98}
                emptyState={
                  <Empty
                    title="No change sets"
                    hint={mayWrite ? "Use New change set to start one." : "Nothing has been created yet."}
                  />
                }
                renderItem={(cs) => (
                  <ChangeSetRow cs={cs} selected={cs.id === selectedId} onSelect={() => setParams({ cs: cs.id })} />
                )}
              />
            )}
            {selected ? <ChangeSetDetail key={selected.id} cs={selected} onChanged={() => void load(true)} /> : null}
          </div>
        </>
      )}

      {dialog === "new" ? <NewChangeSetDialog onClose={() => setDialog(null)} onCreated={onCreated} /> : null}
      {dialog === "eval" ? <EvalQuestionsDialog onClose={() => setDialog(null)} /> : null}
    </div>
  );
}
