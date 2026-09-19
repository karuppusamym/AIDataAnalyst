import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  decideReviewBatch,
  fetchChangeQueue,
  fetchChangeQueueDetails,
  fetchReviewBatch,
  fetchReviewBatchMembers,
  freezeReviewBatch,
} from "../lib/api";
import type {
  ChangeQueueDetail,
  ChangeQueueItem,
  ReviewBatch,
  ReviewBatchCorrection,
  ReviewBatchDecision,
  ReviewBatchMember,
  ReviewBatchMemberOutcome,
} from "../lib/api";
import { VirtualList } from "../components/VirtualList";
import { Button, ConfirmDialog, Empty, ErrorState, Field, Pill } from "../components/primitives";
import "./ReviewQueueScreen.css";
import "./ReviewBatchQueue.css";

/* ---------------------------------------------------------------------------
   R11-REV01 — batch review over the change-focused queue.

   A third tab of the review surface, not a new destination: R11-S13 is
   consolidating Stewardship and keeps the Review queue independent, so this
   adds no sidebar entry and no route -- it is `?queue=batch` of the screen the
   reviewer already uses, beside the governance and parsed-lineage queues.

   What it does, in the order a reviewer does it:

     1. Pages the queue from the server (keyset cursor, 50 rows a page) into a
        windowed list, so a 2,000-row queue never mounts 2,000 rows and never
        asks for them at once. Evidence beyond each row's preview is fetched
        only when the row is opened.
     2. Keeps the selection ACROSS pages, keyed by review id and carrying the
        evidence fingerprint shown on the page. Rows this reviewer may not
        decide (their own proposals, maker-checker) cannot be selected.
     3. Freezes the selection: the server binds the ids and the versions seen,
        and answers with how many members are eligible and why the rest are
        not. Nothing is decided yet.
     4. Decides the frozen batch once. Every member is re-checked by the server
        and decided through the same path a single decision takes; the answer
        is per member -- applied, or refused with a reason -- and each applied
        member names its correction, or says plainly that none exists.

   Batch approval is gated per object type: each type needs the evidence its
   review rests on (a draft's text *and* its source signals, a version's diff,
   ...). A row says which facts it lacks; a type with no evidence contract can
   only be rejected in a batch. The frozen members are listed, paged from the
   server, before anything is decided. A decision is committed per chunk of
   members, so one that fails part-way is resumable: the screen re-reads the
   batch and offers to resume the same decision, which decides only what is
   left.
--------------------------------------------------------------------------- */

const PAGE_SIZE = 50;

const FAMILIES = [
  { value: "", label: "All families" },
  { value: "DESCRIPTION", label: "Descriptions" },
  { value: "SEMANTIC", label: "Semantic meaning" },
  { value: "STEWARDSHIP", label: "Stewardship" },
  { value: "PUBLICATION", label: "Publication" },
  { value: "ACCESS", label: "Access and trust" },
  { value: "OTHER", label: "Other" },
] as const;

/** Reason codes are the server's contract; these are how a reviewer reads them. */
const REASONS: Record<string, string> = {
  STALE_EVIDENCE: "Changed since you reviewed it",
  ALREADY_DECIDED: "Decided by someone else first",
  CONCURRENT_DECISION: "Another decision won the race",
  NOT_FOUND: "Not found in this organization",
  NOT_PENDING: "No longer pending",
  MAKER_CHECKER: "You proposed this (maker-checker)",
  NOT_AUTHORIZED: "Not authorized",
  UNSUPPORTED_TYPE: "No decision path for this type",
  TARGET_UNAVAILABLE: "Its target no longer exists",
  EVIDENCE_NOT_SHOWN: "No evidence shown: reject in a batch or decide it individually",
  NO_EVIDENCE_CONTRACT: "No batch evidence contract for this type: reject in a batch or decide it individually",
  REQUIRED_EVIDENCE_MISSING: "Required evidence missing: reject in a batch or decide it individually",
  INDIVIDUAL_DECISION_REQUIRED: "Trust-boundary change: decide it individually",
  RATIONALE_REQUIRED: "A rejection needs a rationale",
  TARGET_REFUSED: "The target refused the change",
  NO_HUMAN_REVERSAL_ROUTE: "A reversal exists but has no reviewer route yet",
  NO_REOPEN_PATH: "Rejected: propose it again to change it",
  CORRECT_THROUGH_OBJECT_PATH: "Correct it through the object's own screen",
  NOT_APPLIED: "Nothing was applied",
  REVIEW_BATCH_ALREADY_DECIDED: "This batch has already been decided",
  REVIEW_BATCH_DECISION_MISMATCH: "This batch's decision was started the other way: resume it with that decision",
  REVIEW_BATCH_DECISION_CONFLICT: "A concurrent change interrupted the decision",
};

function reason(code: string | null | undefined): string {
  if (!code) return "";
  return REASONS[code] ?? code;
}

function describeFailure(error: unknown): string {
  return error instanceof ApiError ? reason(error.detail) : (error as Error).message;
}

/** `PROPOSED_TEXT` -> "proposed text": the server's fact names, read aloud. */
function factLabel(name: string): string {
  return name.toLowerCase().replace(/_/g, " ");
}

function GatePill({ item }: { item: ChangeQueueItem }) {
  if (!item.approve_gate) return null;
  if (item.approve_gate === "REQUIRED_EVIDENCE_MISSING" && item.approve_evidence_missing.length > 0) {
    return (
      <Pill tone="warn">
        Missing for batch approval: {item.approve_evidence_missing.map(factLabel).join(", ")}
      </Pill>
    );
  }
  return <Pill tone="warn">{reason(item.approve_gate)}</Pill>;
}

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…` : value;
}

function CorrectionLine({ correction }: { correction: ReviewBatchCorrection }) {
  if (correction.available) {
    return (
      <span className="rbq__corr">
        Correct with {correction.kind === "WITHDRAW_DESCRIPTION" ? "a withdrawal request" : correction.kind}
        : <code>{correction.method} {correction.path}</code>
        {correction.subject_type ? (
          <>
            {" "}for {correction.subject_type.toLowerCase()} <code>{shortId(correction.subject_id ?? "")}</code>
          </>
        ) : null}
      </span>
    );
  }
  if (correction.reason_code === "NOT_APPLIED") return null;
  return <span className="rbq__corr rbq__corr--none">{reason(correction.reason_code)}</span>;
}

function QueueRow({
  item,
  selected,
  locked,
  detail,
  onToggle,
  onOpen,
}: {
  item: ChangeQueueItem;
  selected: boolean;
  locked: boolean;
  detail: ChangeQueueDetail | "loading" | { error: string } | undefined;
  onToggle: () => void;
  onOpen: () => void;
}) {
  const blocked = item.decide_blocker !== null;
  const label = `${item.object_type} ${shortId(item.object_id)}`;
  return (
    <article className={`rbq__row${selected ? " rbq__row--selected" : ""}`}>
      <input
        type="checkbox"
        className="rbq__check"
        aria-label={`Select ${label}`}
        checked={selected}
        disabled={blocked || locked}
        onChange={onToggle}
      />
      <div className="rbq__body">
        <div className="rbq__title">
          <b>{item.object_type}</b> <span className="rbq__kind">{item.change_kind}</span>
        </div>
        <div className="rbq__pills">
          <Pill tone="info">{item.review_family}</Pill>
          <Pill tone={item.risk_tier === "T3" ? "bad" : item.risk_tier === "T2" ? "warn" : "mute"}>
            {item.risk_tier}
          </Pill>
          {blocked ? <Pill tone="bad">{reason(item.decide_blocker)}</Pill> : null}
          {!blocked ? <GatePill item={item} /> : null}
        </div>
        {item.evidence_preview[0] ? (
          <p className="rbq__claim">{item.evidence_preview[0].claim}</p>
        ) : (
          <p className="rbq__claim rbq__claim--none">No composed evidence for this type.</p>
        )}
        {item.evidence_count > 0 ? (
          <button type="button" className="rbq__open" onClick={onOpen} aria-expanded={detail !== undefined}>
            {detail === undefined ? `Show all ${item.evidence_count} evidence items` : "Hide evidence"}
          </button>
        ) : null}
        {detail === "loading" ? <p className="rbq__claim">Loading evidence…</p> : null}
        {detail && detail !== "loading" && "error" in detail ? (
          <p className="rbq__err" role="alert">{detail.error}</p>
        ) : null}
        {detail && detail !== "loading" && !("error" in detail) ? (
          <ul className="rbq__evidence">
            {detail.evidence.map((evidence, index) => (
              <li key={index}>
                {evidence.claim} <span className="rbq__src">({evidence.source})</span>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </article>
  );
}

function MemberRow({ member }: { member: ReviewBatchMemberOutcome }) {
  const applied = member.outcome === "APPLIED";
  return (
    <article className={`rbq__member rbq__member--${member.outcome.toLowerCase()}`}>
      <div className="rbq__title">
        <Pill tone={applied ? "ok" : member.outcome === "SKIPPED" ? "mute" : "bad"}>{member.outcome}</Pill>{" "}
        <b>{member.object_type ?? "Unknown review"}</b> <code>{shortId(member.review_id)}</code>
      </div>
      {!applied ? <p className="rbq__claim">{reason(member.reason_code)}</p> : null}
      {member.detail ? <p className="rbq__claim rbq__src">{member.detail}</p> : null}
      <CorrectionLine correction={member.correction} />
    </article>
  );
}

const MEMBER_PAGE = 100;

function FrozenMemberRow({ member }: { member: ReviewBatchMember }) {
  const excluded = member.eligibility === "EXCLUDED";
  const gate = member.approve_gate_code;
  return (
    <article className="rbq__member">
      <div className="rbq__title">
        <span className="tnum">#{member.position + 1}</span> <b>{member.object_type ?? "Unknown review"}</b>{" "}
        <code>{shortId(member.review_id)}</code>{" "}
        {excluded ? (
          <Pill tone="mute">Excluded</Pill>
        ) : gate ? (
          <Pill tone="warn">Reject only</Pill>
        ) : (
          <Pill tone="ok">Approvable</Pill>
        )}
      </div>
      {excluded || gate ? (
        <p className="rbq__claim">{reason(excluded ? member.exclusion_code : gate)}</p>
      ) : null}
    </article>
  );
}

/** Every frozen member, paged from the server in selection order, so the
 *  reviewer can inspect what they are about to decide -- not just its counts. */
function FrozenMembers({ batch }: { batch: ReviewBatch }) {
  const [members, setMembers] = useState<ReviewBatchMember[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  /* Same guard as the queue's: the windowed list may ask for the next page
     again before `loadingMore` has flipped. */
  const requested = useRef<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    requested.current = null;
    setLoading(true);
    setError(null);
    fetchReviewBatchMembers(batch.id, { limit: MEMBER_PAGE }, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return;
        setMembers(page.items);
        setCursor(page.next_cursor);
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) setError(describeFailure(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [batch.id]);

  const loadMore = useCallback(async () => {
    if (!cursor || requested.current === cursor) return;
    requested.current = cursor;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const page = await fetchReviewBatchMembers(batch.id, { cursor, limit: MEMBER_PAGE });
      setMembers((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
    } catch (e) {
      setLoadMoreError(describeFailure(e));
    } finally {
      setLoadingMore(false);
    }
  }, [batch.id, cursor]);

  return (
    <div className="rbq__frozen">
      <h3 className="rbq__countsh">Members, in the order you selected them</h3>
      {loading ? (
        <p className="rbq__claim" role="status">Loading members…</p>
      ) : error ? (
        <p className="rbq__err" role="alert">{error}</p>
      ) : (
        <div className="rbq__members">
          <VirtualList
            items={members}
            getKey={(member) => member.review_id}
            renderItem={(member) => <FrozenMemberRow member={member} />}
            estimateSize={56}
            totalCount={batch.item_count}
            onReachEnd={cursor ? () => void loadMore() : undefined}
            loadingMore={loadingMore}
            loadMoreError={loadMoreError}
            ariaLabel="Frozen batch members"
          />
        </div>
      )}
    </div>
  );
}

function Counts({ title, counts }: { title: string; counts: Record<string, number> }) {
  const entries = Object.entries(counts);
  if (entries.length === 0) return null;
  return (
    <div className="rbq__counts">
      <span className="rbq__countsh">{title}</span>
      <ul>
        {entries.map(([code, count]) => (
          <li key={code}>
            <span className="tnum">{count}</span> {reason(code.split(":").pop())}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ReviewBatchQueue() {
  const [family, setFamily] = useState("");
  const [decidableOnly, setDecidableOnly] = useState(true);
  const [items, setItems] = useState<ChangeQueueItem[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Map<string, string>>(() => new Map());
  const [details, setDetails] = useState<
    Record<string, ChangeQueueDetail | "loading" | { error: string }>
  >({});
  const [batch, setBatch] = useState<ReviewBatch | null>(null);
  const [decision, setDecision] = useState<ReviewBatchDecision | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState(false);
  /* The rationale of the last rejection sent, kept so a resumed rejection
     records the same one on the members still to be decided. */
  const [lastRationale, setLastRationale] = useState<string | null>(null);
  const inflight = useRef<AbortController | null>(null);
  /* The cursor whose page has been asked for. The windowed list calls its
     reach-end callback from render, so it can fire again before the
     `loadingMore` state it checks has flipped; without this the same page is
     fetched twice and its rows appear twice. Kept (not cleared) after a failed
     page, so a refusal is shown once instead of retried on every render. */
  const requestedCursor = useRef<string | null>(null);

  const load = useCallback(async () => {
    inflight.current?.abort();
    const controller = new AbortController();
    inflight.current = controller;
    requestedCursor.current = null;
    setLoading(true);
    setError(null);
    setLoadMoreError(null);
    try {
      const page = await fetchChangeQueue(
        { family: family || null, decidableOnly, limit: PAGE_SIZE },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setItems(page.items);
      setTotal(page.total);
      setCursor(page.next_cursor);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      setError(describeFailure(e));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [family, decidableOnly]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const loadMore = useCallback(async () => {
    if (!cursor || requestedCursor.current === cursor) return;
    requestedCursor.current = cursor;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const page = await fetchChangeQueue({
        family: family || null,
        decidableOnly,
        cursor,
        limit: PAGE_SIZE,
      });
      setItems((current) => [...current, ...page.items]);
      setCursor(page.next_cursor);
      setTotal(page.total);
    } catch (e) {
      setLoadMoreError(describeFailure(e));
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, family, decidableOnly]);

  const locked = batch !== null;

  const toggle = useCallback(
    (item: ChangeQueueItem) => {
      if (locked || item.decide_blocker !== null) return;
      setSelected((current) => {
        const next = new Map(current);
        if (next.has(item.review_id)) next.delete(item.review_id);
        else next.set(item.review_id, item.evidence_fingerprint);
        return next;
      });
    },
    [locked],
  );

  const selectLoaded = useCallback(() => {
    if (locked) return;
    setSelected((current) => {
      const next = new Map(current);
      for (const item of items) {
        if (item.decide_blocker === null) next.set(item.review_id, item.evidence_fingerprint);
      }
      return next;
    });
  }, [items, locked]);

  const open = useCallback(async (item: ChangeQueueItem) => {
    if (details[item.review_id] !== undefined) {
      setDetails((current) => {
        const next = { ...current };
        delete next[item.review_id];
        return next;
      });
      return;
    }
    setDetails((current) => ({ ...current, [item.review_id]: "loading" }));
    try {
      const response = await fetchChangeQueueDetails([item.review_id]);
      const found = response.items[0];
      setDetails((current) => ({
        ...current,
        [item.review_id]: found ?? { error: "This review is no longer in the queue." },
      }));
    } catch (e) {
      setDetails((current) => ({ ...current, [item.review_id]: { error: describeFailure(e) } }));
    }
  }, [details]);

  const freeze = useCallback(async () => {
    if (selected.size === 0) return;
    setBusy(true);
    setActionError(null);
    try {
      const frozen = await freezeReviewBatch(
        [...selected.entries()].map(([review_id, evidence_fingerprint]) => ({
          review_id,
          evidence_fingerprint,
        })),
      );
      setBatch(frozen);
    } catch (e) {
      setActionError(describeFailure(e));
    } finally {
      setBusy(false);
    }
  }, [selected]);

  const decide = useCallback(
    async (verdict: "APPROVE" | "REJECT", rationale: string | null) => {
      if (!batch) return;
      setBusy(true);
      setActionError(null);
      if (verdict === "REJECT") setLastRationale(rationale);
      try {
        const result = await decideReviewBatch(batch.id, { decision: verdict, reason: rationale });
        setDecision(result);
        setBatch(result.batch);
        setRejecting(false);
      } catch (e) {
        setActionError(describeFailure(e));
        /* A decision commits per chunk of members, so a failure may have
           stopped part-way. Re-read the batch: if it is resumable, the panel
           offers to resume -- with the decision the batch recorded, which may
           be the other one if that is what this call was refused for. */
        try {
          setBatch(await fetchReviewBatch(batch.id));
        } catch {
          /* The original failure is what the reviewer needs to see. */
        }
        setRejecting(false);
      } finally {
        setBusy(false);
      }
    },
    [batch],
  );

  const resume = useCallback(() => {
    if (!batch?.decision) return;
    if (batch.decision === "REJECT") {
      if (lastRationale) void decide("REJECT", lastRationale);
      else setRejecting(true);
      return;
    }
    void decide("APPROVE", null);
  }, [batch, decide, lastRationale]);

  const startOver = useCallback(() => {
    setBatch(null);
    setDecision(null);
    setSelected(new Map());
    setActionError(null);
    setLastRationale(null);
    void load();
  }, [load]);

  const approvable = useMemo(() => {
    if (!batch) return 0;
    const gated = Object.values(batch.approve_gate_counts).reduce((sum, value) => sum + value, 0);
    return batch.eligible_count - gated;
  }, [batch]);

  return (
    <section className="rq rbq" aria-labelledby="rbq-h1">
      <header className="rq__head">
        <h1 id="rbq-h1" className="rq__h1">Batch review</h1>
        <p className="rq__lede">
          Select changes across pages, <b>freeze</b> them at the versions you read, then decide
          them once. Each member is re-checked before it is applied; anything that changed or was
          decided by someone else is refused and reported, not applied.
        </p>
      </header>

      <div className="rq__filters">
        <Field label="Review family">
          <select
            value={family}
            disabled={locked}
            onChange={(event) => setFamily(event.target.value)}
          >
            {FAMILIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>
        <label className="rbq__toggle">
          <input
            type="checkbox"
            checked={decidableOnly}
            disabled={locked}
            onChange={(event) => setDecidableOnly(event.target.checked)}
          />{" "}
          Only changes I can decide
        </label>
      </div>

      <div className="rbq__bar" role="status">
        <span className="rbq__summary">
          <span className="tnum">{total ?? "…"}</span> matching ·{" "}
          <span className="tnum">{items.length}</span> loaded ·{" "}
          <b className="tnum">{selected.size}</b> selected across pages
        </span>
        <Button disabled={locked || items.length === 0} onClick={selectLoaded}>
          Select loaded rows
        </Button>
        <Button disabled={locked || selected.size === 0} onClick={() => setSelected(new Map())}>
          Clear selection
        </Button>
        <Button variant="primary" disabled={locked || busy || selected.size === 0} onClick={() => void freeze()}>
          Freeze batch of {selected.size}
        </Button>
      </div>

      {actionError ? (
        <p className="rbq__err" role="alert">
          {actionError}
        </p>
      ) : null}

      {batch && !decision ? (
        <section className="rbq__panel" aria-label="Frozen batch">
          <h2 className="rq__h2">
            Frozen batch: <span className="tnum">{batch.item_count}</span> members,{" "}
            <span className="tnum">{batch.eligible_count}</span> eligible
          </h2>
          <Counts title="Excluded when frozen" counts={batch.exclusion_counts} />
          <Counts title="Eligible, but rejection only" counts={batch.approve_gate_counts} />
          {batch.resumable ? (
            <div className="rbq__resume" role="status">
              <p>
                The {batch.decision === "REJECT" ? "rejection" : "approval"} stopped part-way:{" "}
                <span className="tnum">{batch.outcome_counts.PENDING ?? 0}</span> of{" "}
                <span className="tnum">{batch.item_count}</span> members are not recorded yet. Members
                already decided stay decided; resuming decides only the rest.
              </p>
              <Button variant="primary" disabled={busy} onClick={resume}>
                Resume the {batch.decision === "REJECT" ? "rejection" : "approval"}
              </Button>
            </div>
          ) : (
            <div className="rbq__actions">
              <Button variant="primary" disabled={busy || approvable <= 0} onClick={() => void decide("APPROVE", null)}>
                Approve {approvable} eligible
              </Button>
              <Button disabled={busy || batch.eligible_count === 0} onClick={() => setRejecting(true)}>
                Reject batch…
              </Button>
              <Button disabled={busy} onClick={startOver}>
                Discard and start over
              </Button>
            </div>
          )}
          <FrozenMembers batch={batch} />
        </section>
      ) : null}

      {decision ? (
        <section className="rbq__panel" aria-label="Batch outcome">
          <h2 className="rq__h2">
            {decision.overall === "SUCCESS"
              ? "Every member was applied"
              : decision.overall === "PARTIAL_SUCCESS"
                ? "Partly applied"
                : "Nothing was applied"}
            : <span className="tnum">{decision.applied_count}</span> applied,{" "}
            <span className="tnum">{decision.refused_count}</span> refused,{" "}
            <span className="tnum">{decision.skipped_count}</span> skipped
          </h2>
          {decision.resumed ? (
            <p className="rbq__claim">
              Resumed an interrupted decision: <span className="tnum">{decision.decided_in_this_call_count}</span>{" "}
              members decided now; the rest were recorded by the earlier attempt and were not decided again.
            </p>
          ) : null}
          <div className="rbq__members">
            <VirtualList
              items={decision.members}
              getKey={(member) => member.review_id}
              renderItem={(member) => <MemberRow member={member} />}
              estimateSize={72}
              ariaLabel="Batch members and their outcomes"
            />
          </div>
          <div className="rbq__actions">
            <Button onClick={startOver}>Start a new batch</Button>
          </div>
        </section>
      ) : null}

      <div className="rq__main">
        {loading ? (
          <div className="rq__load" role="status">Loading the queue…</div>
        ) : error ? (
          <ErrorState title="The change queue could not be loaded" detail={error} onRetry={() => void load()} />
        ) : (
          <VirtualList
            items={items}
            getKey={(item) => item.review_id}
            renderItem={(item) => (
              <QueueRow
                item={item}
                selected={selected.has(item.review_id)}
                locked={locked}
                detail={details[item.review_id]}
                onToggle={() => toggle(item)}
                onOpen={() => void open(item)}
              />
            )}
            estimateSize={110}
            totalCount={total}
            onReachEnd={cursor ? () => void loadMore() : undefined}
            loadingMore={loadingMore}
            loadMoreError={loadMoreError}
            ariaLabel="Change queue"
            emptyState={<Empty title="Nothing to review" hint="No pending change matches these filters." />}
          />
        )}
      </div>

      {rejecting ? (
        <ConfirmDialog
          title="Reject the frozen batch"
          description="The rationale is recorded on every member this rejection applies to."
          confirmLabel="Reject batch"
          requireReason
          reasonLabel="Rationale"
          busy={busy}
          error={actionError}
          onConfirm={(rationale) => void decide("REJECT", rationale)}
          onCancel={() => setRejecting(false)}
        />
      ) : null}
    </section>
  );
}
