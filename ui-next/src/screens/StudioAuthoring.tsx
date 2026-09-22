import { useRef, useState } from "react";
import type { StudioChangeItemCreate, StudioChangeItemRead, StudioChangeSetRead } from "../lib/types";
import { addStudioChangeItem, createStudioChangeSet, removeStudioChangeItem } from "../lib/api";
import { Button, ConfirmDialog, Dialog, Field } from "../components/primitives";
import { FormError, useSubmitAction } from "../components/screenState";
import { CheckOutcome, hasCheck, startCheck } from "./StudioChecks";
import type { CheckAnswer } from "./StudioChecks";
import { OBJECT_TYPES, OPERATIONS, SNAPSHOT_HINTS, parseJsonObject } from "./studioForm";
import type { StudioObjectType, StudioOperation } from "./studioForm";
import { ITEMS_EDITABLE_STATUS } from "./studioRoles";

/* ---------------------------------------------------------------------------
   Studio authoring dialogs (R11-AUD08): a new change set, an item added to a
   DRAFT one, an item removed.

   Each is a modal that OWNS its request (`useSubmitAction`: one write admitted at
   a time, the server's refusal kept in the dialog in the server's own words) and
   reports success to the screen through a callback. The screen -- not the dialog
   -- re-reads whatever the write changed, because what a dialog knows is what the
   API said in reply, and what the screen must show is what the API holds now.

   NOTHING IS OFFERED ON A GUESS. Whether a control appears is decided by the
   screen (`roleHolds`, fail-closed, and the change set's status); these
   components assume they were opened legitimately and treat a refusal as an
   answer, not as a bug.
--------------------------------------------------------------------------- */

/** `POST /v1/studio/change-sets`: the one field the API takes is `name` (2-200 characters). */
export function NewChangeSetDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (created: StudioChangeSetRead) => void;
}) {
  const [name, setName] = useState("");
  const create = useSubmitAction<StudioChangeSetRead>();
  const blank = name.trim() === "";
  // The dialog's default is the first focusable element, which is its close button: a form
  // opens on its first field, so typing starts at once.
  const nameRef = useRef<HTMLInputElement>(null);

  // Length is the server's to judge: a one-character name is sent, and its 422 ("at least 2
  // characters") is what the author reads. Only a field with nothing in it is held back.
  const submit = async () => {
    if (blank) return;
    const created = await create.run(() => createStudioChangeSet({ name: name.trim() }));
    if (created) onCreated(created);
  };

  return (
    <Dialog
      title="New change set"
      description="A change set starts as a DRAFT: add items, run the tests, then submit it for review."
      onClose={onClose}
      dismissOnBackdrop={false}
      initialFocusRef={nameRef}
      footer={
        <>
          <Button onClick={onClose} disabled={create.submitting}>
            Cancel
          </Button>
          <Button variant="primary" disabled={create.submitting || blank} onClick={() => void submit()}>
            {create.submitting ? "Creating…" : "Create change set"}
          </Button>
        </>
      }
    >
      <Field label="Name">
        <input
          ref={nameRef}
          value={name}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void submit();
            }
          }}
          autoComplete="off"
        />
      </Field>
      {create.error ? <FormError detail={create.error} /> : null}
    </Dialog>
  );
}

/**
 * `POST /v1/studio/change-sets/{id}/items`: one proposed change to a governed object.
 *
 * The API accepts any JSON object for either snapshot and computes the item's diff only
 * when BOTH are given, so the form says so rather than leaving an author to wonder why a
 * changed metric shows no diff. The snapshots are typed as JSON because that is what they
 * are; a syntax error is reported here (the API never sees the text) and everything else
 * is the server's to refuse.
 */
export function AddItemDialog({
  changeSet,
  onClose,
  onAdded,
}: {
  changeSet: StudioChangeSetRead;
  onClose: () => void;
  onAdded: (added: StudioChangeItemRead) => void;
}) {
  const [objectType, setObjectType] = useState<StudioObjectType>("METRIC");
  const [operation, setOperation] = useState<StudioOperation>("CREATE");
  const [objectId, setObjectId] = useState("");
  const [beforeText, setBeforeText] = useState("");
  const [afterText, setAfterText] = useState("");
  const add = useSubmitAction<StudioChangeItemRead>();
  const check = useSubmitAction<CheckAnswer>();
  const typeRef = useRef<HTMLSelectElement>(null);

  /** Both snapshots parsed, or the first field that is not JSON. */
  const parse = () => {
    const before = parseJsonObject(beforeText, "Before snapshot");
    const after = parseJsonObject(afterText, "After snapshot");
    return { before, after };
  };

  const submit = async () => {
    const { before, after } = parse();
    if (!before.ok) return add.fail(before.error);
    if (!after.ok) return add.fail(after.error);
    const body: StudioChangeItemCreate = {
      object_type: objectType,
      object_id: objectId.trim(),
      operation,
      ...(before.value ? { before_snapshot: before.value } : {}),
      ...(after.value ? { after_snapshot: after.value } : {}),
    };
    const added = await add.run(() => addStudioChangeItem(changeSet.id, body));
    if (added) onAdded(added);
  };

  const runCheck = () => {
    const { after } = parse();
    check.reset();
    if (!after.ok) return check.fail(after.error);
    startCheck(check, { object_type: objectType, object_id: objectId, operation, after_snapshot: after.value });
  };

  return (
    <Dialog
      title="Add item"
      description={`Proposes a change to a governed object in “${changeSet.name}”. Items can only be added or removed while the change set is ${ITEMS_EDITABLE_STATUS}.`}
      onClose={onClose}
      dismissOnBackdrop={false}
      initialFocusRef={typeRef}
      className="dlg--wide"
      footer={
        <>
          <Button onClick={onClose} disabled={add.submitting}>
            Cancel
          </Button>
          {hasCheck({ object_type: objectType, operation }) ? (
            <Button onClick={runCheck} disabled={add.submitting || check.submitting}>
              {check.submitting ? "Checking…" : objectType === "TOOL" ? "Check contract" : "Check definition"}
            </Button>
          ) : null}
          <Button
            variant="primary"
            disabled={add.submitting || objectId.trim() === ""}
            onClick={() => void submit()}
          >
            {add.submitting ? "Adding…" : "Add item"}
          </Button>
        </>
      }
    >
      <div className="csform__row">
        <Field label="Object type">
          <select
            ref={typeRef}
            value={objectType}
            onChange={(event) => {
              setObjectType(event.target.value as StudioObjectType);
              check.reset();
            }}
          >
            {OBJECT_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Operation">
          <select
            value={operation}
            onChange={(event) => {
              setOperation(event.target.value as StudioOperation);
              check.reset();
            }}
          >
            {OPERATIONS.map((op) => (
              <option key={op} value={op}>
                {op}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Object id">
          <input value={objectId} onChange={(event) => setObjectId(event.target.value)} autoComplete="off" />
        </Field>
      </div>
      <p className="dlg__hint">{SNAPSHOT_HINTS[objectType]} A DELETE needs no snapshot.</p>
      <Field label="Before snapshot (JSON, optional)">
        <textarea
          className="csform__json"
          rows={5}
          spellCheck={false}
          value={beforeText}
          onChange={(event) => setBeforeText(event.target.value)}
        />
      </Field>
      <Field label="After snapshot (JSON, optional)">
        <textarea
          className="csform__json"
          rows={7}
          spellCheck={false}
          value={afterText}
          onChange={(event) => setAfterText(event.target.value)}
        />
      </Field>
      <p className="dlg__hint">
        The change set&rsquo;s diff for this item is computed only when both snapshots are given. An UPDATE&rsquo;s before
        snapshot is what Detect conflicts compares with the published state.
      </p>
      {check.error ? <FormError detail={check.error} /> : null}
      {check.result ? <CheckOutcome answer={check.result} /> : null}
      {add.error ? <FormError detail={add.error} /> : null}
    </Dialog>
  );
}

/**
 * `DELETE /v1/studio/change-sets/{id}/items/{item_id}`, behind a confirmation.
 *
 * What it removes is a PROPOSAL: the item row. The governed object it named is not touched,
 * and nothing is undone -- but the item and its test status are gone, and (the API deletes,
 * it does not archive) adding it again starts it UNTESTED. The removal is recorded in the
 * audit ledger (`studio.change_item.remove`).
 */
export function RemoveItemDialog({
  changeSet,
  item,
  onClose,
  onRemoved,
}: {
  changeSet: StudioChangeSetRead;
  item: StudioChangeItemRead;
  onClose: () => void;
  onRemoved: (removed: StudioChangeItemRead) => void;
}) {
  const remove = useSubmitAction<true>();
  const confirm = async () => {
    const done = await remove.run(async () => {
      await removeStudioChangeItem(changeSet.id, item.id);
      return true as const;
    });
    if (done) onRemoved(item);
  };
  return (
    <ConfirmDialog
      title={`Remove ${item.object_type} ${item.object_id}?`}
      description="This deletes the item from the change set and records the removal in the audit ledger. The governed object it named is not touched. Adding it again starts it untested."
      confirmLabel="Remove item"
      destructive
      busy={remove.submitting}
      error={remove.error}
      onConfirm={() => void confirm()}
      onCancel={onClose}
    />
  );
}
