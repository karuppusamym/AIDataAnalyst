import { useEffect, useId, useRef, useState } from "react";
import type {
  GlossaryConflictCreate,
  GlossaryConflictRead,
  GlossaryConflictResolution,
  GovernanceReviewRead,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import {
  ApiError,
  CONFLICT_DETECT_RUN_LIMIT,
  detectGlossaryConflicts,
  fetchGlossaryConflicts,
  raiseGlossaryConflict,
  submitGlossaryConflictResolution,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Button, ConfirmDialog, Dialog, Empty, ErrorState, Field, Pill } from "../components/primitives";
import { FormError, useAsyncResource, useSubmitAction } from "../components/screenState";
import { CrossLinks } from "../components/CrossLinks";
import type { CrossLink } from "../components/CrossLinks";
import {
  GLOSSARY_REVIEW_WRITE_ROLES,
  Pager,
  ReviewHandoff,
  humanize,
  listOr,
  stamp,
  statusTone,
  useGlossaryReviewAccess,
} from "./glossaryReviewShared";
import "./GlossaryReview.css";

/* ---------------------------------------------------------------------------
   Glossary review -> Conflicts (R11-AUD08).

   A glossary conflict is two positions on what something means that the
   platform refuses to settle by last write. This tab lists them, opens one to
   show BOTH positions and why they conflict, and lets a steward do the three
   things the API offers: run detection, raise one they saw for themselves, and
   propose a resolution.

   WHAT A RESOLUTION IS, because the word promises more than the API does. It is
   a PROPOSAL: the conflict moves OPEN -> REVIEW_REQUIRED and a
   `GLOSSARY_CONFLICT` governance review opens (tier T2: a person decides it,
   never an agent). Somebody other than the proposer approves or rejects it --
   maker != checker, enforced by the review decision route, not by this screen.
   Approval marks the conflict RESOLVED; rejection reopens it and clears the
   proposal. In NEITHER case is a term's definition edited
   (`stewardship_service.apply_conflict_resolution` sets a status and a
   reviewer, and module 08 section 6 says so in as many words), so every
   sentence on this tab that describes a decision says what it records and
   never what it "fixes".

   EVERY CONFLICT TYPE COMES THROUGH THIS ROUTE, including
   `METRIC_FORMULA_COLLISION`, which `semantic_api` writes into the same table
   with `term_id: null` and metric-shaped positions. There is no type filter on
   the read, so the positions are rendered from what they contain -- a name, a
   definition, then every remaining field by its own name -- rather than from
   an assumed "term" shape that a metric row would quietly break.
--------------------------------------------------------------------------- */

const PAGE_SIZE = 25;

/** The values `GlossaryConflict.status` takes: `OPEN`, then `REVIEW_REQUIRED`
 *  once a resolution is proposed, then `RESOLVED` on approval (a rejection goes
 *  back to `OPEN`). `stewardship_service.apply_conflict_resolution` /
 *  `reject_conflict_resolution`. */
const CONFLICT_STATUSES = ["OPEN", "REVIEW_REQUIRED", "RESOLVED"] as const;

const TYPE_LABELS: Record<string, string> = {
  SYNONYM_COLLISION: "Synonym collision",
  DEFINITION: "Definition",
  SOURCE_DISAGREEMENT: "Source disagreement",
  METRIC_FORMULA_COLLISION: "Metric formula collision",
};
const typeLabel = (type: string): string => TYPE_LABELS[type] ?? humanize(type);

/** The decisions `GlossaryConflictResolution.resolution` accepts, and what each
 *  RECORDS -- none of them edits a term. */
const DECISIONS: ReadonlyArray<{
  value: GlossaryConflictResolution["resolution"];
  label: string;
  records: string;
}> = [
  {
    value: "ACCEPT_POSITION_A",
    label: "Accept position A",
    records: "Position A stands as the meaning. Position B stays on the record.",
  },
  {
    value: "ACCEPT_POSITION_B",
    label: "Accept position B",
    records: "Position B stands as the meaning. Position A stays on the record.",
  },
  {
    value: "MERGE",
    label: "Merge",
    records: "Neither stands alone. You propose one merged definition for the reviewer to approve.",
  },
  {
    value: "RETAIN_BOTH",
    label: "Retain both",
    records: "Both stand: the difference is intended, and both stay on the record.",
  },
];
const decisionLabel = (value: string): string =>
  DECISIONS.find((d) => d.value === value)?.label ?? humanize(value);

/** The server's own bounds on the resolution body (`GlossaryConflictResolution`
 *  in `schemas.py`), mirrored so the form refuses what the API would refuse. */
const RATIONALE_MIN = 10;
const RATIONALE_MAX = 2000;
const DEFINITION_MAX = 10_000;

const isText = (value: unknown): value is string => typeof value === "string" && value.trim() !== "";

/** Keys that name a position, in the order they are preferred. A glossary term
 *  carries `display_name`; a metric collision carries `metric_name`. */
const NAME_KEYS = ["display_name", "metric_name", "name"] as const;

function positionName(position: Record<string, unknown>): string | null {
  const key = NAME_KEYS.find((candidate) => isText(position[candidate]));
  return key ? (position[key] as string) : null;
}

function valueText(value: unknown): string {
  if (value === null || value === undefined) return "none";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

const capitalise = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1);

/** The reason a row is a conflict, in a sentence. Built from the type and from
 *  what the positions themselves say, and it says less rather than guess: a
 *  steward-raised conflict carries free-form positions, so for those it names
 *  only who raised it. */
function conflictWhy(conflict: GlossaryConflictRead): string {
  const { position_a: a, position_b: b } = conflict;
  switch (conflict.conflict_type) {
    case "SYNONYM_COLLISION": {
      const label = [a.colliding_label, b.colliding_label].find(isText);
      return label
        ? `Two approved terms share the label “${label}” and define it differently.`
        : "Two approved terms share a label and define it differently.";
    }
    case "METRIC_FORMULA_COLLISION": {
      const kind = [a.match_kind, b.match_kind].find(isText);
      const how =
        kind === "EXACT_MATCH"
          ? " Every field of the formula is identical."
          : kind === "NORMALIZED_GRAIN_MATCH"
            ? " Every field is identical except the grain, which differs only in case or spacing."
            : "";
      return (
        "Two published metrics compute the same thing under different names: the same aggregation " +
        `over the same source table and measure.${how} Questions routed to one and to the other can disagree.`
      );
    }
    case "DEFINITION":
      return `${conflict.raised_by} recorded two definitions that disagree.`;
    case "SOURCE_DISAGREEMENT":
      return `${conflict.raised_by} recorded two sources that disagree.`;
    default:
      return `${conflict.raised_by} recorded this as a ${humanize(conflict.conflict_type)}.`;
  }
}

function conflictTitle(conflict: GlossaryConflictRead): string {
  const a = positionName(conflict.position_a);
  const b = positionName(conflict.position_b);
  if (a && b) return `${a} vs ${b}`;
  return `${typeLabel(conflict.conflict_type)} ${conflict.id.slice(0, 8)}`;
}

function PositionCard({ label, position }: { label: string; position: Record<string, unknown> }) {
  const headingId = useId();
  const nameKey = NAME_KEYS.find((candidate) => isText(position[candidate]));
  const definition = isText(position.definition) ? position.definition : null;
  const rest = Object.entries(position).filter(
    ([key]) => key !== nameKey && !(key === "definition" && definition !== null),
  );
  return (
    <div className="glrev__position" role="group" aria-labelledby={headingId}>
      <h3 className="glrev__posthead" id={headingId}>
        {label}
      </h3>
      {nameKey ? <p className="glrev__posname">{position[nameKey] as string}</p> : null}
      {definition ? <p className="glrev__posdef">{definition}</p> : null}
      {rest.length > 0 ? (
        <dl className="glrev__kv">
          {rest.map(([key, value]) => (
            <div key={key} className="glrev__kvrow">
              <dt>{capitalise(humanize(key))}</dt>
              <dd>{key.endsWith("_id") ? <code>{valueText(value)}</code> : valueText(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {nameKey === undefined && definition === null && rest.length === 0 ? (
        <p className="glrev__muted">This position is empty.</p>
      ) : null}
    </div>
  );
}

/** Where the terms in a conflict can be read in full: Business meaning, filtered
 *  to the term's name. Offered only for a position that names a term. */
function termLinks(conflict: GlossaryConflictRead): CrossLink[] {
  return [conflict.position_a, conflict.position_b]
    .filter((position) => isText(position.term_id) && isText(position.display_name))
    .map((position) => ({
      screen: "meaning",
      label: position.display_name as string,
      params: { view: "glossary", q: position.display_name as string },
      title: "Open this term in Business meaning",
    }));
}

function ConflictDetail({
  conflict,
  panelId,
  mayWrite,
  mayOpenReviewQueue,
  onResolve,
}: {
  conflict: GlossaryConflictRead;
  panelId: string;
  mayWrite: boolean;
  mayOpenReviewQueue: boolean;
  onResolve: (conflict: GlossaryConflictRead) => void;
}) {
  const links = termLinks(conflict);
  return (
    <div className="glrev__detail" id={panelId} role="region" aria-label={`Detail of ${conflictTitle(conflict)}`}>
      <p className="glrev__why">{conflictWhy(conflict)}</p>
      <div className="glrev__positions">
        <PositionCard label="Position A" position={conflict.position_a} />
        <PositionCard label="Position B" position={conflict.position_b} />
      </div>
      {links.length > 0 ? <CrossLinks label="Read the terms" links={links} /> : null}

      {conflict.proposed_resolution ? (
        <div className="glrev__proposal">
          <h3 className="glrev__posthead">Proposed resolution</h3>
          <p>
            <strong>{decisionLabel(conflict.proposed_resolution)}</strong>
          </p>
          {conflict.proposed_definition ? (
            <p className="glrev__posdef">{conflict.proposed_definition}</p>
          ) : null}
          {conflict.resolution_rationale ? (
            <p className="glrev__muted">Rationale: {conflict.resolution_rationale}</p>
          ) : null}
        </div>
      ) : null}

      {conflict.status === "REVIEW_REQUIRED" ? (
        <div className="glrev__hint">
          <p>
            Waiting for a decision in the Review queue. Whoever proposed this resolution cannot decide it: a
            different reviewer approves or rejects it, and a rejection reopens the conflict.
          </p>
          {mayOpenReviewQueue ? (
            <CrossLinks
              label="Review"
              links={[
                {
                  screen: "governance",
                  label: "Glossary conflict reviews",
                  params: { type: "GLOSSARY_CONFLICT" },
                  title: "The Review queue, filtered to glossary conflicts",
                },
              ]}
            />
          ) : null}
        </div>
      ) : null}
      {conflict.status === "RESOLVED" ? (
        <p className="glrev__hint">
          Resolved{conflict.resolved_by ? ` by ${conflict.resolved_by}` : ""} at {stamp(conflict.resolved_at)}. Both
          positions are still on the record; neither term was edited.
        </p>
      ) : null}

      {conflict.status === "OPEN" && mayWrite ? (
        <div className="glrev__actions">
          <Button variant="primary" onClick={() => onResolve(conflict)}>
            Propose a resolution
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/** What proposing a resolution says it does, before it does it, and asks for the
 *  decision and the reason the API requires. */
function ResolveConflictDialog({
  conflict,
  onClose,
  onProposed,
  onStale,
}: {
  conflict: GlossaryConflictRead;
  onClose: () => void;
  onProposed: (review: GovernanceReviewRead) => void;
  onStale: () => void;
}) {
  const [resolution, setResolution] = useState<GlossaryConflictResolution["resolution"] | null>(null);
  const [definition, setDefinition] = useState("");
  const [rationale, setRationale] = useState("");
  const submit = useSubmitAction<GovernanceReviewRead>();
  // Focus lands on the first decision, not on the dialog's close button.
  const firstDecision = useRef<HTMLInputElement>(null);
  const definitionId = useId();
  const rationaleId = useId();
  const rationaleHintId = useId();

  const merging = resolution === "MERGE";
  const definitionOk = !merging || definition.trim().length > 0;
  const rationaleOk = rationale.trim().length >= RATIONALE_MIN;
  const ready = resolution !== null && definitionOk && rationaleOk;

  const confirm = async () => {
    if (!ready || resolution === null) return;
    const body: GlossaryConflictResolution = {
      resolution,
      // A definition belongs to a merge; sending one with another decision would
      // record text the steward chose not to propose.
      ...(merging ? { resolved_definition: definition.trim() } : {}),
      rationale: rationale.trim(),
    };
    const review = await submit.run(async () => {
      try {
        return await submitGlossaryConflictResolution(conflict.id, body);
      } catch (failure) {
        // 409 "only open conflicts can be resolved": the row on screen described a
        // state that no longer holds, so the list is re-read behind the dialog.
        if (failure instanceof ApiError && failure.status === 409) onStale();
        throw failure;
      }
    });
    if (review !== null) onProposed(review);
  };

  return (
    <Dialog
      title="Propose a resolution"
      description={
        `${conflictTitle(conflict)}. This does not settle anything by itself: it moves the conflict to ` +
        "review and opens a governance review that a different reviewer approves or rejects. Approval marks " +
        "the conflict resolved; rejection reopens it. Neither edits either term's definition, and both " +
        "positions stay on the record."
      }
      onClose={onClose}
      initialFocusRef={firstDecision}
      dismissOnBackdrop={false}
      className="dlg--wide"
      footer={
        <>
          <Button onClick={onClose} disabled={submit.submitting}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!ready || submit.submitting} onClick={() => void confirm()}>
            {submit.submitting ? "Working…" : "Propose resolution"}
          </Button>
        </>
      }
    >
      <fieldset className="glrev__decisions">
        <legend>Decision</legend>
        {DECISIONS.map((decision, index) => (
          <label key={decision.value} className="glrev__decision">
            <input
              ref={index === 0 ? firstDecision : undefined}
              type="radio"
              name="glossary-conflict-decision"
              value={decision.value}
              checked={resolution === decision.value}
              onChange={() => setResolution(decision.value)}
            />
            <span>
              <strong>{decision.label}</strong>
              <span className="glrev__decisionhint">{decision.records}</span>
            </span>
          </label>
        ))}
      </fieldset>

      {merging ? (
        <div className="field">
          <label className="field__label" htmlFor={definitionId}>
            Merged definition
          </label>
          <textarea
            id={definitionId}
            className="dlg__reason"
            rows={4}
            maxLength={DEFINITION_MAX}
            value={definition}
            onChange={(event) => setDefinition(event.target.value)}
          />
          <span className="dlg__hint">
            A merge is decided on the definition it proposes, so it is required here. It is recorded with the
            proposal; it does not replace either term&rsquo;s definition.
          </span>
        </div>
      ) : null}

      <div className="field">
        <label className="field__label" htmlFor={rationaleId}>
          Rationale
        </label>
        <textarea
          id={rationaleId}
          className="dlg__reason"
          rows={4}
          maxLength={RATIONALE_MAX}
          value={rationale}
          aria-describedby={rationaleHintId}
          onChange={(event) => setRationale(event.target.value)}
        />
        <span className="dlg__hint" id={rationaleHintId}>
          At least {RATIONALE_MIN} characters. It is recorded with the proposal and shown to the reviewer.
        </span>
      </div>

      {submit.error ? <FormError detail={submit.error} /> : null}
    </Dialog>
  );
}

const RAISE_TYPES: ReadonlyArray<{ value: GlossaryConflictCreate["conflict_type"]; label: string }> = [
  { value: "DEFINITION", label: "Definition" },
  { value: "SYNONYM_COLLISION", label: "Synonym collision" },
  { value: "SOURCE_DISAGREEMENT", label: "Source disagreement" },
];

interface DraftPosition {
  name: string;
  definition: string;
  source: string;
}
const EMPTY_POSITION: DraftPosition = { name: "", definition: "", source: "" };

/** A draft position as the wire object: only what the steward wrote. The API
 *  takes any object here, so nothing is added and an empty field is absent. */
function positionBody(draft: DraftPosition): Record<string, unknown> {
  return {
    ...(draft.name.trim() ? { display_name: draft.name.trim() } : {}),
    ...(draft.definition.trim() ? { definition: draft.definition.trim() } : {}),
    ...(draft.source.trim() ? { source: draft.source.trim() } : {}),
  };
}

function PositionFields({
  legend,
  value,
  onChange,
}: {
  legend: string;
  value: DraftPosition;
  onChange: (next: DraftPosition) => void;
}) {
  return (
    <fieldset className="glrev__draft">
      <legend>{legend}</legend>
      <Field label={`${legend} name`}>
        <input value={value.name} maxLength={200} onChange={(e) => onChange({ ...value, name: e.target.value })} />
      </Field>
      <Field label={`${legend} definition`}>
        <textarea
          className="dlg__reason"
          rows={3}
          value={value.definition}
          onChange={(e) => onChange({ ...value, definition: e.target.value })}
        />
      </Field>
      <Field label={`${legend} source (optional)`}>
        <input value={value.source} maxLength={200} onChange={(e) => onChange({ ...value, source: e.target.value })} />
      </Field>
    </fieldset>
  );
}

function RaiseConflictDialog({
  organizationId,
  onClose,
  onRaised,
}: {
  organizationId: string;
  onClose: () => void;
  onRaised: (conflict: GlossaryConflictRead) => void;
}) {
  const [type, setType] = useState<GlossaryConflictCreate["conflict_type"]>("DEFINITION");
  const [a, setA] = useState<DraftPosition>(EMPTY_POSITION);
  const [b, setB] = useState<DraftPosition>(EMPTY_POSITION);
  const [owner, setOwner] = useState("");
  const submit = useSubmitAction<GlossaryConflictRead>();

  // A position with no name and no definition says nothing to disagree with.
  const filled = (draft: DraftPosition) => draft.name.trim() !== "" || draft.definition.trim() !== "";
  const ready = filled(a) && filled(b);

  const confirm = async () => {
    if (!ready) return;
    const body: GlossaryConflictCreate = {
      conflict_type: type,
      position_a: positionBody(a),
      position_b: positionBody(b),
      ...(owner.trim() ? { assigned_owner: owner.trim() } : {}),
    };
    const conflict = await submit.run(() => raiseGlossaryConflict(organizationId, body));
    if (conflict !== null) onRaised(conflict);
  };

  return (
    <Dialog
      title="Raise a conflict"
      description={
        "Records two positions you have seen disagree. It is created OPEN, assigned to the owner you name, and " +
        "changes no term. Resolving it is a separate step, and a different reviewer decides that."
      }
      onClose={onClose}
      dismissOnBackdrop={false}
      className="dlg--wide"
      footer={
        <>
          <Button onClick={onClose} disabled={submit.submitting}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!ready || submit.submitting} onClick={() => void confirm()}>
            {submit.submitting ? "Working…" : "Raise conflict"}
          </Button>
        </>
      }
    >
      <Field label="Conflict type">
        <select value={type} onChange={(e) => setType(e.target.value as GlossaryConflictCreate["conflict_type"])}>
          {RAISE_TYPES.map((entry) => (
            <option key={entry.value} value={entry.value}>
              {entry.label}
            </option>
          ))}
        </select>
      </Field>
      <PositionFields legend="Position A" value={a} onChange={setA} />
      <PositionFields legend="Position B" value={b} onChange={setB} />
      <Field label="Assigned owner (optional)">
        <input
          value={owner}
          maxLength={255}
          placeholder="risk-data-stewards@tenant.example"
          onChange={(e) => setOwner(e.target.value)}
        />
      </Field>
      {submit.error ? <FormError detail={submit.error} /> : null}
    </Dialog>
  );
}

interface Notice {
  text: string;
  /** Present when the notice is a hand-off to the Review queue: the review that was opened. */
  reviewId?: string;
}

export function GlossaryConflicts() {
  const organizationId = useOrgId();
  const access = useGlossaryReviewAccess();
  const [params, setParams] = useUrlState();
  const statusParam = params.get("status") ?? "";
  // A `?status=` this tab does not know is read as "every status": a filter the
  // select cannot show would leave a list narrowed by something invisible.
  const status = (CONFLICT_STATUSES as readonly string[]).includes(statusParam) ? statusParam : "";
  const [offset, setOffset] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [detecting, setDetecting] = useState(false);
  const [raising, setRaising] = useState(false);
  const [resolving, setResolving] = useState<GlossaryConflictRead | null>(null);
  const detect = useSubmitAction<PageOf<GlossaryConflictRead>>();
  const panelPrefix = useId();

  const list = useAsyncResource<PageOf<GlossaryConflictRead>>(
    (signal) =>
      fetchGlossaryConflicts(organizationId, { status: status || null, limit: PAGE_SIZE, offset }, signal),
    [organizationId, status, offset],
    { enabled: access.read === "ask" },
  );
  const reload = list.reload;

  // A page past the end -- the last row of page 2 was just proposed away from an
  // OPEN filter -- is not "no conflicts": step back to the last page that exists.
  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  useEffect(() => {
    if (list.data && list.data.items.length === 0 && offset > 0 && list.data.total > 0) {
      setOffset(Math.max(0, (Math.ceil(list.data.total / PAGE_SIZE) - 1) * PAGE_SIZE));
    }
  }, [list.data, offset]);

  const setStatus = (next: string) => {
    setOffset(0);
    setOpenId(null);
    setParams({ status: next || null });
  };

  const openDetect = () => {
    detect.reset();
    setNotice(null);
    setDetecting(true);
  };
  const runDetect = async () => {
    const result = await detect.run(() => detectGlossaryConflicts(organizationId));
    if (result === null) return; // the refusal stays in the dialog, in the server's words
    setDetecting(false);
    const raised = result.items.length;
    setNotice({
      text:
        raised === 0
          ? "Detection found nothing new: every colliding pair is already open or in review, or no two approved terms share a label with different definitions."
          : `Detection raised ${raised} new conflict${raised === 1 ? "" : "s"}.` +
            (raised >= CONFLICT_DETECT_RUN_LIMIT
              ? ` It stopped at its limit of ${CONFLICT_DETECT_RUN_LIMIT} per run: run detection again for more.`
              : ""),
    });
    reload();
  };

  const closeResolve = () => setResolving(null);

  return (
    <div className="glrev__view">
      <div className="glrev__head">
        <div>
          <h2 className="glrev__h2">Glossary conflicts</h2>
          <p className="glrev__lede">
            Two positions on what something means, kept side by side until a reviewer settles them. Nothing here
            rewrites a term: a resolution is a decision on the record.
          </p>
        </div>
        {access.mayWrite ? (
          <div className="glrev__headactions">
            <Button variant="primary" onClick={openDetect}>
              Detect conflicts
            </Button>
            <Button onClick={() => setRaising(true)}>Raise a conflict</Button>
          </div>
        ) : null}
      </div>

      {access.identityKnown && !access.mayWrite ? (
        <p className="glrev__hint">
          You can read conflicts. Detecting, raising and resolving them is for sessions holding{" "}
          {listOr(GLOSSARY_REVIEW_WRITE_ROLES)}.
        </p>
      ) : null}

      {notice ? (
        notice.reviewId ? (
          <ReviewHandoff reviewId={notice.reviewId} mayOpenReviewQueue={access.mayOpenReviewQueue}>
            {notice.text}
          </ReviewHandoff>
        ) : (
          <p className="glrev__notice" role="status">
            {notice.text}
          </p>
        )
      ) : null}

      {access.read === "skip" ? (
        <p className="glrev__hint">
          Not applicable to your roles: only sessions holding one of the roles that read glossary review can see
          conflicts.
        </p>
      ) : (
        <>
          <div className="glrev__filters">
            <Field label="Status">
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="">All statuses</option>
                {CONFLICT_STATUSES.map((value) => (
                  <option key={value} value={value}>
                    {humanize(value)}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          {list.error ? (
            <ErrorState title="Glossary conflicts could not be loaded" detail={list.error} onRetry={reload} />
          ) : access.read === "wait" || !list.data ? (
            <div className="glrev__loading" role="status">
              Loading glossary conflicts…
            </div>
          ) : items.length === 0 && total === 0 ? (
            <Empty
              title={status ? `No conflicts with status ${humanize(status)}` : "No glossary conflicts"}
              hint={
                status
                  ? "Choose another status, or all statuses, to see the rest."
                  : access.mayWrite
                    ? "Detect conflicts to look for approved terms that share a label but define it differently, or raise one you have seen."
                    : "No conflict has been detected or raised in this organization."
              }
            />
          ) : (
            <>
              <ul className="glrev__list" aria-label="Glossary conflicts">
                {items.map((conflict) => {
                  const open = openId === conflict.id;
                  const panelId = `${panelPrefix}-${conflict.id}`;
                  return (
                    <li key={conflict.id} className="glrev__item">
                      <div className="glrev__itemhead">
                        <button
                          type="button"
                          className="glrev__toggle"
                          aria-expanded={open}
                          aria-controls={open ? panelId : undefined}
                          onClick={() => setOpenId(open ? null : conflict.id)}
                        >
                          {conflictTitle(conflict)}
                        </button>
                        <Pill tone="mute">{typeLabel(conflict.conflict_type)}</Pill>
                        <Pill tone={statusTone(conflict.status)}>{humanize(conflict.status)}</Pill>
                      </div>
                      <p className="glrev__meta">
                        Raised by {conflict.raised_by} at {stamp(conflict.created_at)}
                        {conflict.assigned_owner ? ` · owner ${conflict.assigned_owner}` : ""}
                      </p>
                      {open ? (
                        <ConflictDetail
                          conflict={conflict}
                          panelId={panelId}
                          mayWrite={access.mayWrite}
                          mayOpenReviewQueue={access.mayOpenReviewQueue}
                          onResolve={setResolving}
                        />
                      ) : null}
                    </li>
                  );
                })}
              </ul>
              <Pager
                offset={offset}
                limit={PAGE_SIZE}
                total={total}
                shown={items.length}
                noun="conflicts"
                onPage={(next) => {
                  setOpenId(null);
                  setOffset(next);
                }}
              />
            </>
          )}
        </>
      )}

      {detecting ? (
        <ConfirmDialog
          title="Detect glossary conflicts?"
          description={
            "Scans up to 5,000 approved, active glossary terms for two that share a label (a display name or a " +
            "synonym, ignoring case) but define it differently, and raises an OPEN conflict for each new pair, at " +
            `most ${CONFLICT_DETECT_RUN_LIMIT} per run. Pairs already open or in review are skipped; a pair ` +
            "resolved earlier is raised again if its terms still collide, because a resolution edits neither " +
            "term. Detection changes no term. It records the conflicts, an event and an audit entry."
          }
          confirmLabel="Detect conflicts"
          busy={detect.submitting}
          error={detect.error}
          onConfirm={() => void runDetect()}
          onCancel={() => setDetecting(false)}
        />
      ) : null}

      {raising ? (
        <RaiseConflictDialog
          organizationId={organizationId}
          onClose={() => setRaising(false)}
          onRaised={(conflict) => {
            setRaising(false);
            setNotice({ text: `Raised “${conflictTitle(conflict)}” as an open conflict.` });
            reload();
          }}
        />
      ) : null}

      {resolving ? (
        <ResolveConflictDialog
          conflict={resolving}
          onClose={closeResolve}
          onStale={reload}
          onProposed={(review) => {
            setResolving(null);
            setNotice({
              text:
                "Resolution proposed. A governance review is waiting in the Review queue; someone other than " +
                "you approves or rejects it. Approval marks the conflict resolved, rejection reopens it, and " +
                "neither edits a term.",
              reviewId: review.id,
            });
            reload();
          }}
        />
      ) : null}
    </div>
  );
}
