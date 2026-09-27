import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import {
  ApiError,
  OWNERSHIP_MATCH_FIELDS,
  OWNERSHIP_OWNER_TYPES,
  OWNERSHIP_RULE_APPLY_MAX_TABLES,
  OWNERSHIP_RULE_KEY_PATTERN,
  applyOwnershipRule,
  createOwnershipRule,
  fetchOwnershipRules,
} from "../lib/api";
import type { OwnershipMatchField, OwnershipOwnerType } from "../lib/api";
import type { BulkStewardshipOperationRead, OwnershipRuleCreate, OwnershipRuleRead } from "../lib/types";
import { useOrgId } from "../lib/org";
import { readDecision, roleHolds } from "../lib/roles";
import { useSession } from "../lib/session";
import { Button, Empty, ErrorState, Field, Pill, useUnsavedChanges } from "../components/primitives";
import { FormError, FormSuccess, useAsyncResource, useSubmitAction } from "../components/screenState";
import { HintedField, NotApplicable, OwnershipConfirm, RequestedReview, listOr, stamp } from "./OwnershipParts";
import { OwnershipRequests } from "./OwnershipRequests";

/* ---------------------------------------------------------------------------
   Ownership -> Rules (R11-AUD08, part 2).

   A rule is a standing instruction -- "every table whose schema is `retail` is
   owned by the retail stewards" -- that a steward writes once and APPLIES when
   they want it acted on. The API has three routes for it and this panel is all
   three:

     GET  /v1/organizations/{id}/ownership-rules   the ACTIVE rules
     POST /v1/organizations/{id}/ownership-rules   create one
     POST /v1/ownership-rules/{id}/apply           act on it

   WHAT CREATING A RULE DOES: records it, audited (`ownership.rule.create`). No
   table is matched and no owner assigned. The form says so, because the two
   verbs sit in one panel and "Create rule" reads like it might do the job.

   WHAT APPLYING DOES, which is not what the word says. `apply_ownership_rule`
   takes no body and has no preview mode. When called it walks the organization's
   ACTIVE tables (the first 10,000 by id), keeps those the rule matches -- at most
   500 -- and opens ONE review asking to make the rule's owner an owner of each.
   Nothing is assigned until a DIFFERENT principal approves that review. So the
   confirmation says, in this order: what will be searched for, that there is no
   way to see the matches first, that nothing changes until a reviewer approves,
   and that an owner is ADDED (a table's other owners are not removed; tables this
   owner already holds are skipped). It also says a second apply opens a second
   review, because the server does not notice a duplicate -- a steward who clicks
   twice, or a colleague who does, doubles the reviewer's work.

   A rule cannot be edited or retired: the API has no route for either. The panel
   says so where a steward would look for the missing button.

   The confirmation is the only place these numbers are stated as facts, and each
   is the handler's own (`OWNERSHIP_RULE_APPLY_MAX_TABLES`, `fnmatchcase` over
   case-folded values, TAG matching ANY approved tag, a table with no domain never
   matching DOMAIN_KEY). A refusal is shown as the server wrote it.
--------------------------------------------------------------------------- */

/**
 * The roles `GET /v1/organizations/{organization_id}/ownership-rules` admits.
 *
 * Copied from the surface-control matrix row for
 * `aida.stewardship_api.list_ownership_rules`
 * (`Docs/50-security/surface-control-matrix.md`): Analyst, Auditor, DataAdmin,
 * DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer.
 */
const RULES_READ_ROLES = [
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
 * The roles `POST /v1/organizations/{organization_id}/ownership-rules` and
 * `POST /v1/ownership-rules/{rule_id}/apply` admit.
 *
 * Copied from the matrix rows for `aida.stewardship_api.create_ownership_rule`
 * and `apply_ownership_rule`: DataSteward, MetadataAdmin, PlatformAdmin,
 * SemanticAdmin -- the same four for both.
 */
const RULES_WRITE_ROLES = ["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"];

const MATCH_FIELDS: Record<OwnershipMatchField, { label: string; help: string }> = {
  TABLE_NAME: { label: "Table name", help: "The table's own name, without its schema." },
  SCHEMA_NAME: { label: "Schema name", help: "The name of the schema the table is in." },
  QUALIFIED_NAME: { label: "Qualified name", help: "schema.table, matched as one string." },
  DOMAIN_KEY: {
    label: "Domain key",
    help: "The key of the business domain the table is annotated with. A table with no domain never matches.",
  },
  TAG: { label: "Business tag", help: "Matches when ANY of the table's approved business tags matches." },
};

const OWNER_TYPE_LABEL: Record<OwnershipOwnerType, string> = { INDIVIDUAL: "Individual", GROUP: "Group" };

interface Draft {
  ruleKey: string;
  displayName: string;
  matchField: OwnershipMatchField;
  matchPattern: string;
  ownerType: OwnershipOwnerType;
  ownerPrincipal: string;
}

const EMPTY_DRAFT: Draft = {
  ruleKey: "",
  displayName: "",
  matchField: "SCHEMA_NAME",
  matchPattern: "",
  ownerType: "GROUP",
  ownerPrincipal: "",
};

export const RULE_UNSAVED_MESSAGE = "Discard the ownership rule you have not created?";

/** What is wrong with the draft, in the words shown under the field -- the server's limits, mirrored. */
function draftProblems(draft: Draft): Partial<Record<keyof Draft, string>> {
  const problems: Partial<Record<keyof Draft, string>> = {};
  if (!OWNERSHIP_RULE_KEY_PATTERN.test(draft.ruleKey)) {
    problems.ruleKey = "Use lowercase letters, digits, - and _, starting with a letter: 2 to 100 characters.";
  }
  const name = draft.displayName.trim().length;
  if (name < 2 || name > 200) problems.displayName = "2 to 200 characters.";
  const pattern = draft.matchPattern.trim().length;
  if (pattern < 1 || pattern > 255) problems.matchPattern = "1 to 255 characters.";
  const principal = draft.ownerPrincipal.trim().length;
  if (principal < 2 || principal > 255) problems.ownerPrincipal = "2 to 255 characters.";
  return problems;
}

function CreateRuleForm({ organizationId, onCreated }: { organizationId: string; onCreated: () => void }) {
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [edited, setEdited] = useState(false);
  const create = useSubmitAction<OwnershipRuleRead>();
  useUnsavedChanges(edited, RULE_UNSAVED_MESSAGE);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((previous) => ({ ...previous, [key]: value }));
    setEdited(true);
  };
  const problems = draftProblems(draft);
  const valid = Object.keys(problems).length === 0;
  // A hint appears once a field has been typed in, not on an empty form: an
  // untouched form that opens covered in complaints reads as already broken.
  const hint = (key: keyof Draft) => (draft[key] !== "" && problems[key] ? problems[key] : null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!valid) return;
    const body: OwnershipRuleCreate = {
      rule_key: draft.ruleKey,
      display_name: draft.displayName.trim(),
      match_field: draft.matchField,
      match_pattern: draft.matchPattern.trim(),
      owner_type: draft.ownerType,
      owner_principal: draft.ownerPrincipal.trim(),
    };
    const created = await create.run(() => createOwnershipRule(organizationId, body));
    if (created === null) return; // the refusal is in `create.error`, shown under the form
    setDraft(EMPTY_DRAFT);
    setEdited(false);
    onCreated();
  };

  const field = MATCH_FIELDS[draft.matchField];
  return (
    <section className="own__panel" aria-label="Create an ownership rule">
      <div className="own__panelhead">
        <div>
          <p className="own__eyebrow">NEW RULE</p>
          <h3 className="own__h2">Create an ownership rule</h3>
        </div>
      </div>
      <p className="own__note">
        A rule is a standing instruction. Creating one records it and assigns nothing: no table is matched and no owner
        is named until you apply it, and applying it asks a reviewer first.
      </p>
      <form className="own__form" onSubmit={(event) => void submit(event)} noValidate>
        <div className="own__grid">
          <HintedField label="Rule key" hint={hint("ruleKey") ?? "Unique in this organization. It cannot be changed later."}>
            {(describedBy) => (
              <input
                value={draft.ruleKey}
                onChange={(event) => set("ruleKey", event.target.value)}
                placeholder="retail-tables"
                maxLength={100}
                aria-invalid={hint("ruleKey") !== null}
                aria-describedby={describedBy}
                autoComplete="off"
              />
            )}
          </HintedField>
          <HintedField label="Display name" hint={hint("displayName") ?? "What the rule is called in lists."}>
            {(describedBy) => (
              <input
                value={draft.displayName}
                onChange={(event) => set("displayName", event.target.value)}
                placeholder="Retail tables"
                maxLength={200}
                aria-invalid={hint("displayName") !== null}
                aria-describedby={describedBy}
              />
            )}
          </HintedField>
          <HintedField label="Match field" hint={field.help}>
            {(describedBy) => (
              <select
                value={draft.matchField}
                onChange={(event) => set("matchField", event.target.value as OwnershipMatchField)}
                aria-describedby={describedBy}
              >
                {OWNERSHIP_MATCH_FIELDS.map((value) => (
                  <option key={value} value={value}>
                    {MATCH_FIELDS[value].label}
                  </option>
                ))}
              </select>
            )}
          </HintedField>
          <HintedField
            label="Match pattern"
            hint={hint("matchPattern") ?? "A glob: * matches any run of characters, ? matches one. Capital letters are ignored."}
          >
            {(describedBy) => (
              <input
                value={draft.matchPattern}
                onChange={(event) => set("matchPattern", event.target.value)}
                placeholder="retail_*"
                maxLength={255}
                aria-invalid={hint("matchPattern") !== null}
                aria-describedby={describedBy}
                autoComplete="off"
              />
            )}
          </HintedField>
          <div className="own__fieldwrap">
            <Field label="Owner type">
              <select
                value={draft.ownerType}
                onChange={(event) => set("ownerType", event.target.value as OwnershipOwnerType)}
              >
                {OWNERSHIP_OWNER_TYPES.map((value) => (
                  <option key={value} value={value}>
                    {OWNER_TYPE_LABEL[value]}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <HintedField
            label="Owner principal"
            hint={hint("ownerPrincipal") ?? "The person or group each matching table would be assigned to."}
          >
            {(describedBy) => (
              <input
                value={draft.ownerPrincipal}
                onChange={(event) => set("ownerPrincipal", event.target.value)}
                placeholder="retail-data-stewards@tenant.example"
                maxLength={255}
                aria-invalid={hint("ownerPrincipal") !== null}
                aria-describedby={describedBy}
                autoComplete="off"
              />
            )}
          </HintedField>
        </div>
        {create.error ? <FormError detail={create.error} /> : null}
        {create.result ? (
          <FormSuccess>
            Rule “{create.result.display_name}” created. Nothing has been assigned: apply it to put the tables it matches
            up for review.
          </FormSuccess>
        ) : null}
        <div className="own__actions">
          <Button type="submit" variant="primary" disabled={!valid || create.submitting}>
            {create.submitting ? "Creating…" : "Create rule"}
          </Button>
        </div>
      </form>
    </section>
  );
}

/** What the apply confirmation states, as a list -- each line is a fact of `apply_ownership_rule`. */
function applyFacts(rule: OwnershipRuleRead, earlier: BulkStewardshipOperationRead | undefined) {
  const field = MATCH_FIELDS[rule.match_field].label.toLowerCase();
  return [
    <>
      It looks for active tables whose {field} matches <code>{rule.match_pattern}</code>, ignoring capital letters. At
      most {OWNERSHIP_RULE_APPLY_MAX_TABLES} tables go into one review (the first {OWNERSHIP_RULE_APPLY_MAX_TABLES}
      {" "}matches, in table-id order), and the server does not say when more matched.
    </>,
    <>
      There is no preview. The matching tables are found when you confirm, so this screen cannot show you which tables
      they will be.
    </>,
    <>
      <strong>Nothing changes yet.</strong> It opens one review asking to make {rule.owner_principal} (
      {rule.owner_type.toLowerCase()}) an owner of each table. A different reviewer has to approve it in the Review
      queue: you cannot approve your own request.
    </>,
    <>
      On approval this owner is added beside any owner a table already has; no existing owner is removed, and a table
      this owner already holds is skipped.
    </>,
    earlier ? (
      <>
        You already requested this rule at {stamp(earlier.created_at)}. Applying it again opens a second review; it does
        not replace the first.
      </>
    ) : (
      <>Applying the rule again later opens a second review; it does not replace this one.</>
    ),
    <>The request is recorded in the audit ledger.</>,
  ];
}

export function OwnershipRules() {
  const organizationId = useOrgId();
  const session = useSession();
  const roles = session.me?.roles;
  // The read is held while `/v1/me` is in flight and sent only once identity has answered, so a
  // session outside the list is never asked (`readDecision`); the server's 403 stays the authority.
  const read = readDecision(session, RULES_READ_ROLES);
  // Both writes are controls: offered only to a session KNOWN to hold a role (fail closed).
  const mayWrite = roleHolds(roles, RULES_WRITE_ROLES);
  const identityKnown = roles !== undefined;

  const rules = useAsyncResource<{ items: OwnershipRuleRead[]; total: number }>(
    (signal) => fetchOwnershipRules(organizationId, signal),
    [organizationId],
    { enabled: read === "ask" },
  );
  const reloadRules = rules.reload;

  const [target, setTarget] = useState<OwnershipRuleRead | null>(null);
  const apply = useSubmitAction<BulkStewardshipOperationRead>();
  const [latest, setLatest] = useState<{ operation: BulkStewardshipOperationRead; ruleName: string } | null>(null);
  // The last request THIS screen opened for each rule: what makes a second apply visible as a second.
  const [requested, setRequested] = useState<Record<string, BulkStewardshipOperationRead>>({});
  const [refreshKey, setRefreshKey] = useState(0);

  const ruleNames = useMemo(
    () => new Map((rules.data?.items ?? []).map((rule) => [rule.id, rule.display_name] as const)),
    [rules.data],
  );

  const openApply = (rule: OwnershipRuleRead) => {
    apply.reset();
    setTarget(rule);
  };
  const closeApply = () => {
    apply.reset();
    setTarget(null);
  };

  const confirmApply = async () => {
    if (!target) return;
    const rule = target;
    const operation = await apply.run(async () => {
      try {
        return await applyOwnershipRule(rule.id);
      } catch (failure) {
        // "active ownership rule not found": the list is showing a rule that is gone.
        if (failure instanceof ApiError && failure.status === 404) reloadRules();
        throw failure;
      }
    });
    if (operation === null) return; // the refusal is in `apply.error`, shown in the dialog
    apply.reset();
    setTarget(null);
    setLatest({ operation, ruleName: rule.display_name });
    setRequested((previous) => ({ ...previous, [rule.id]: operation }));
    setRefreshKey((key) => key + 1);
  };

  const items = rules.data?.items ?? [];
  const emptyHint = !identityKnown
    ? undefined
    : mayWrite
      ? "Create one above. Nothing is assigned until a rule is applied and a reviewer approves it."
      : `Creating a rule needs ${listOr(RULES_WRITE_ROLES)}.`;

  return (
    <div className="own__body">
      {mayWrite ? <CreateRuleForm organizationId={organizationId} onCreated={reloadRules} /> : null}
      {!mayWrite && identityKnown && read !== "skip" ? (
        <p className="own__note">
          Your roles can read ownership rules. Creating or applying one needs {listOr(RULES_WRITE_ROLES)}.
        </p>
      ) : null}

      {latest ? (
        <RequestedReview
          operation={latest.operation}
          noun="tables"
          headline={`Review requested for “${latest.ruleName}”`}
          remainder={
            latest.operation.subject_ids.length >= OWNERSHIP_RULE_APPLY_MAX_TABLES
              ? `${OWNERSHIP_RULE_APPLY_MAX_TABLES} is the most one apply puts in a review, and the server does not say whether more tables matched: this request may not cover every match.`
              : undefined
          }
          onDismiss={() => setLatest(null)}
        />
      ) : null}

      <section className="own__panel" aria-label="Ownership rules">
        <div className="own__panelhead">
          <div>
            <p className="own__eyebrow">OWNERSHIP RULES</p>
            <h2 className="own__h2">Standing rules</h2>
          </div>
          {read === "ask" && rules.data ? <Pill tone="mute">{items.length} active</Pill> : null}
        </div>
        <p className="own__note">
          Rules cannot be edited or retired: the API has no route for either. To change one, create a new rule under a
          new key.
        </p>
        {read === "skip" ? (
          <NotApplicable what="ownership rules" roles={RULES_READ_ROLES} />
        ) : read === "wait" || (rules.loading && !rules.data) ? (
          <p className="own__note" role="status">
            Loading ownership rules…
          </p>
        ) : rules.error ? (
          <ErrorState title="Ownership rules could not be loaded" detail={rules.error} onRetry={reloadRules} />
        ) : items.length === 0 ? (
          <Empty title="No ownership rules yet" hint={emptyHint} />
        ) : (
          <ul className="own__rules" aria-label="Ownership rules">
            {items.map((rule) => {
              const earlier = requested[rule.id];
              return (
                <li key={rule.id} className="own__rule">
                  <div className="own__rulehead">
                    <div>
                      <strong>{rule.display_name}</strong> <code className="own__code">{rule.rule_key}</code>
                    </div>
                    {mayWrite ? (
                      <Button onClick={() => openApply(rule)}>
                        Apply<span className="sr-only"> {rule.display_name}</span>…
                      </Button>
                    ) : null}
                  </div>
                  <p className="own__rulematch">
                    Tables whose {MATCH_FIELDS[rule.match_field].label.toLowerCase()} matches{" "}
                    <code className="own__code">{rule.match_pattern}</code> are assigned to{" "}
                    <Pill tone="mute">{rule.owner_type.toLowerCase()}</Pill> <strong>{rule.owner_principal}</strong>.
                  </p>
                  <p className="own__opmeta">
                    <span>
                      created by {rule.created_by} at {stamp(rule.created_at)}
                    </span>
                    {earlier ? (
                      <span>
                        review requested at {stamp(earlier.created_at)} for {earlier.subject_ids.length} table
                        {earlier.subject_ids.length === 1 ? "" : "s"}
                      </span>
                    ) : null}
                  </p>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <OwnershipRequests organizationId={organizationId} kind="rules" ruleNames={ruleNames} refreshKey={refreshKey} />

      {target ? (
        <OwnershipConfirm
          title={`Apply “${target.display_name}”?`}
          summary="This asks for a review. It does not assign anyone yet."
          facts={applyFacts(target, requested[target.id])}
          confirmLabel="Request review"
          busy={apply.submitting}
          error={apply.error}
          onConfirm={() => void confirmApply()}
          onCancel={closeApply}
        />
      ) : null}
    </div>
  );
}
