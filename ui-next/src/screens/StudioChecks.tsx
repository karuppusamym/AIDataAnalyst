import type {
  StudioChangeItemRead,
  StudioContextProductValidateRequest,
  StudioContextProductValidateResult,
  StudioParameterContractValidateRequest,
  StudioParameterContractValidateResult,
} from "../lib/types";
import { validateStudioContextProduct, validateStudioParameterContract } from "../lib/api";
import { readDecision } from "../lib/roles";
import { useSession } from "../lib/session";
import { Button, Pill } from "../components/primitives";
import { FormError, useSubmitAction } from "../components/screenState";
import type { SubmitAction } from "../components/screenState";
import { STUDIO_READ_ROLES } from "./studioRoles";

/* ---------------------------------------------------------------------------
   The two "Check" actions (R11-AUD08): `POST /v1/studio/parameter-contracts/validate`
   for a TOOL item and `POST /v1/studio/context-products/validate` for a
   CONTEXT_PRODUCT item.

   WHAT THEY ARE. The very check the item's own test runs
   (`studio_test_harness.py`), run on demand and without a change set: stateless,
   no database session, nothing recorded. That is why they are offered to every
   role that may READ Studio and not only to the four that may write, although
   they are POSTs -- the surface-control matrix marks them "mutating verb, no write
   found" and the handlers agree. A Viewer can check an existing item; an author
   can check a draft before adding it, which is what the endpoints were written
   for ("validate incrementally while still drafting").

   WHAT THEY ARE NOT. A green check is not a passing test. The METRIC and TERM
   validators have no endpoint, so those types get no Check; a CONTEXT_PRODUCT
   check does not look its references up (that happens at submission); and a TOOL
   check covers the parameter contract but not the required-field checks the test
   also makes. The result says as much, in the words below, rather than letting a
   "valid" stand for more than it proves.

   WHY THIS EXISTS BESIDE "RUN TESTS". The test response carries totals and each
   item's PASSED/FAILED -- not the reason an item failed (`run_tests` returns
   `suite_result.evidence`, and the per-item failures it computes are dropped). For
   the two types with a validator, a Check is how an author finds out why.
--------------------------------------------------------------------------- */

export type CheckAnswer =
  | { readonly kind: "tool"; readonly result: StudioParameterContractValidateResult }
  | { readonly kind: "context"; readonly result: StudioContextProductValidateResult };

export type CheckRequest =
  | { readonly kind: "tool"; readonly body: StudioParameterContractValidateRequest }
  | { readonly kind: "context"; readonly body: StudioContextProductValidateRequest };

/** The part of an item (or of a draft of one) a validator reads. */
export interface CheckDraft {
  readonly object_type: string;
  readonly object_id: string;
  readonly operation: string;
  readonly after_snapshot: Record<string, unknown> | null;
}

/**
 * What an item says to a validator: the request, why it cannot be asked yet, or
 * `null` when the type has no validator at all.
 *
 *  - TOOL: the validator reads `sql_template`, `parameters` and `dialect` out of
 *    the after snapshot, exactly as the test harness does (a missing `parameters`
 *    is an empty list; a missing `dialect` is the server's default, so it is left
 *    out of the request rather than guessed). A DELETE is accepted by the test
 *    without a look, so there is nothing to check.
 *  - CONTEXT_PRODUCT: the validator takes the operation, the object id and the
 *    snapshot itself, and answers for DELETE too (the id must be a UUID).
 */
export function checkRequestFor(draft: CheckDraft): CheckRequest | { readonly unavailable: string } | null {
  const snapshot = draft.after_snapshot;
  if (draft.object_type === "TOOL") {
    if (draft.operation === "DELETE") return null;
    if (snapshot === null) return { unavailable: "There is no after snapshot to check." };
    const template = snapshot.sql_template;
    if (typeof template !== "string" || template === "") {
      return { unavailable: "The after snapshot has no sql_template to check." };
    }
    const parameters = snapshot.parameters ?? [];
    if (!Array.isArray(parameters)) return { unavailable: "The after snapshot's parameters is not a list." };
    const dialect = snapshot.dialect;
    return {
      kind: "tool",
      body: {
        sql_template: template,
        parameters: parameters as Record<string, unknown>[],
        ...(typeof dialect === "string" && dialect !== "" ? { dialect } : {}),
      },
    };
  }
  if (draft.object_type === "CONTEXT_PRODUCT") {
    const objectId = draft.object_id.trim();
    if (objectId === "") return { unavailable: "Enter the object id to check it." };
    return {
      kind: "context",
      body: {
        operation: draft.operation as StudioContextProductValidateRequest["operation"],
        object_id: objectId,
        ...(snapshot ? { snapshot } : {}),
      },
    };
  }
  return null;
}

/** Ask the validator `request` names. */
export function runCheck(request: CheckRequest): Promise<CheckAnswer> {
  return request.kind === "tool"
    ? validateStudioParameterContract(request.body).then((result) => ({ kind: "tool", result }))
    : validateStudioContextProduct(request.body).then((result) => ({ kind: "context", result }));
}

/** Run a check on `check`'s behalf, or record why it cannot run -- one path for both dialogs. */
export function startCheck(check: SubmitAction<CheckAnswer>, draft: CheckDraft): void {
  const request = checkRequestFor(draft);
  if (request === null) return;
  if ("unavailable" in request) {
    check.fail(request.unavailable);
    return;
  }
  void check.run(() => runCheck(request));
}

/** Whether an item of this type and operation has a validator to ask. */
export const hasCheck = (draft: Pick<CheckDraft, "object_type" | "operation">): boolean =>
  draft.object_type === "CONTEXT_PRODUCT" || (draft.object_type === "TOOL" && draft.operation !== "DELETE");

/** What a validator answered, in its own words: the verdict, every error, and what it proves. */
export function CheckOutcome({ answer }: { answer: CheckAnswer }) {
  const { valid, errors } = answer.result;
  const definitions =
    answer.kind === "tool" ? answer.result.definitions : answer.result.definition ? [answer.result.definition] : [];
  return (
    <div className="cschk" role="status">
      <div className="cschk__verdict">
        <Pill tone={valid ? "ok" : "bad"}>{valid ? "valid" : "not valid"}</Pill>
        <span className="cschk__what">
          {answer.kind === "tool" ? "Parameter contract" : "Context product definition"}
        </span>
      </div>
      {errors.length > 0 ? (
        <ul className="cschk__errors">
          {errors.map((error, index) => (
            <li key={index}>{error}</li>
          ))}
        </ul>
      ) : null}
      {valid ? (
        <p className="cs__none">
          {answer.kind === "tool"
            ? "The contract parses and renders with one representative value per parameter. A test also needs name, sql_template and allowed_roles."
            : "The definition has the shape a context product needs. Its references are not looked up here; they are checked when the change set is submitted."}
        </p>
      ) : null}
      {answer.kind === "tool" && answer.result.sample_rendered_sql ? (
        <>
          <div className="evp__sub">Sample render</div>
          <pre className="cs__pre">{answer.result.sample_rendered_sql}</pre>
        </>
      ) : null}
      {answer.kind === "context" && (answer.result.product_key || answer.result.project_id) ? (
        <p className="cs__none">
          {answer.result.product_key ? <>product key {answer.result.product_key}</> : null}
          {answer.result.product_key && answer.result.project_id ? " · " : null}
          {answer.result.project_id ? <>project {answer.result.project_id}</> : null}
        </p>
      ) : null}
      {definitions.length > 0 ? (
        <details className="cschk__defs">
          <summary>{answer.kind === "tool" ? "Parsed parameter definitions" : "Parsed definition"}</summary>
          <pre className="cs__pre">{JSON.stringify(answer.kind === "tool" ? definitions : definitions[0], null, 2)}</pre>
        </details>
      ) : null}
    </div>
  );
}

/**
 * Check on an existing item: a button and, under it, the validator's answer or refusal.
 *
 * Renders nothing for a type with no validator. The request is read-only in effect,
 * but it is still a request, so a session known to be outside the read list is not
 * offered it (`readDecision`; the detail pane is only reachable once the list has
 * been read, which makes this a second line rather than the first).
 */
export function ItemCheck({ item }: { item: StudioChangeItemRead }) {
  const session = useSession();
  const check = useSubmitAction<CheckAnswer>();
  if (!hasCheck(item) || readDecision(session, STUDIO_READ_ROLES) !== "ask") return null;
  const label = item.object_type === "TOOL" ? "Check contract" : "Check definition";
  return (
    <div className="cschk__item">
      <Button disabled={check.submitting} onClick={() => startCheck(check, item)}>
        {check.submitting ? "Checking…" : label}
        <span className="sr-only"> for {item.object_id}</span>
      </Button>
      {check.error ? <FormError detail={check.error} /> : null}
      {check.result ? <CheckOutcome answer={check.result} /> : null}
    </div>
  );
}
