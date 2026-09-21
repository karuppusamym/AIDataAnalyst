import { useCallback, useState } from "react";
import type {
  ContextProductCreate,
  ContextProductRead,
  ContextProductVersionCreate,
  ContextProductVersionRead,
} from "../lib/types";
import {
  createContextProduct,
  demoOr,
  fetchCatalogRows,
  fetchContextProductRoutineOptions,
  fetchSemanticModelVersions,
  fetchTools,
  postJson,
} from "../lib/api";
import { listGlossaryTerms } from "../lib/_api_append";
import { listOntologyVersions } from "../lib/api/ontology";
import { readDecision } from "../lib/roles";
import { useSession } from "../lib/session";
import { ReferencePicker, usePickerOptions } from "../components/ReferencePicker";
import { Button, Field, Pill } from "../components/primitives";
import { splitList } from "../components/screenState";
import type { StatusChannel } from "../components/screenState";

/* ---------------------------------------------------------------------------
   Create draft -- assembling one bounded package out of already-approved
   objects.

   The four reference pickers and the request body are one unit, and that is
   the whole point of the screen: what a steward can put in a package is
   exactly what the platform has already approved, because each picker reads
   the same list as the screen that owns those objects, and the selections are
   held as ordered id arrays -- which is precisely the shape
   `ContextProductCreate` wants. There is no parse step at submit time and
   therefore no way for a typo to become a 422, and no id ever passes through
   a human's clipboard.

   Tables are organization-scoped; semantics, glossary terms, tools and
   routines are project-scoped, so those stay empty (and say so) until a
   project is chosen, rather than offering another project's objects.

   Routines (R11-FP12) are the fifth picker. A view needs none of its own: it
   is a table, and compiling the product reports its definition coverage.
   `routine_ids` is sent only when one is picked, so a draft without routines
   posts the same body it always did.

   `policy_summary` is fixed to gateway-only / no-raw-context, exactly as the
   legacy form hard-codes it: a Context Product is a description of approved
   metadata, and the source values it describes are never part of it.

   R11-FP12 (2026-09-18): the pickers are also the second half of this file,
   `NewVersionPanel`. Routines could only be chosen when a product was first
   created -- there was no way to add a version at all from this screen, so a
   product that should now name a procedure, or should stop naming one the
   source retired, had to be re-created under a new key. The panel posts
   `POST /v1/context-products/{id}/versions` (`create_context_product_version`)
   with the latest version as its base, pre-filled from it, and renders the
   very same six pickers through `GovernedReferencePickers` -- the routine one
   fed by the same `context-product-routine-options` route -- so what a new
   version may name is exactly what a new product may name.
--------------------------------------------------------------------------- */

interface DraftState {
  productKey: string;
  name: string;
  ownerType: "GROUP" | "INDIVIDUAL";
  ownerPrincipal: string;
  description: string;
  purpose: string;
  tableIds: string[];
  semanticIds: string[];
  glossaryIds: string[];
  toolIds: string[];
  routineIds: string[];
  ontologyVersionIds: string[];
  consumerRoles: string;
  lineageDepth: string;
  minimumScore: string;
  denyOnCriticalIncident: boolean;
}

const INITIAL_DRAFT: DraftState = {
  productKey: "",
  name: "",
  ownerType: "GROUP",
  ownerPrincipal: "",
  description: "",
  purpose: "",
  tableIds: [],
  semanticIds: [],
  glossaryIds: [],
  toolIds: [],
  routineIds: [],
  ontologyVersionIds: [],
  consumerRoles: "Analyst",
  lineageDepth: "2",
  minimumScore: "85",
  denyOnCriticalIncident: true,
};

/** The six governed-reference groups, as the draft state holds them. */
type ReferenceKey = "tableIds" | "semanticIds" | "glossaryIds" | "toolIds" | "routineIds" | "ontologyVersionIds";

/**
 * The roles `GET /v1/organizations/{organization_id}/ontology-versions` admits.
 *
 * Copied from the surface-control matrix row for
 * `aida.ontology_api.list_ontology_versions`
 * (`Docs/50-security/surface-control-matrix.md`): DataSteward, MetadataAdmin,
 * PlatformAdmin, Reviewer. An AgentDeveloper, ToolDeveloper, Analyst or Viewer
 * bundle is refused it -- and this panel sits in the screen's rail, so the
 * picker below asked on EVERY load of Context products, whether or not anyone
 * ever opened the ontology list (R11-AUD01, found for `sam.agentdev`).
 */
const ONTOLOGY_READ_ROLES = ["DataSteward", "MetadataAdmin", "PlatformAdmin", "Reviewer"];

/**
 * The roles `GET /v1/projects/{project_id}/context-product-routine-options` admits.
 *
 * Copied from the matrix row for
 * `aida.context_product_api.list_context_product_routine_options`:
 * DataSteward, PlatformAdmin, SemanticAdmin -- narrower than the table, semantic
 * and tool pickers beside it, which the same AgentDeveloper / ToolDeveloper /
 * Analyst / Viewer bundle IS admitted to. Found while proving the ontology fix
 * above against the live API as `sam.agentdev` (GET, 403): the demo rehearsal
 * never selects a project, so it never reaches this one, but the first presenter
 * who does gets the server's "one of these roles is required" in the picker.
 */
const ROUTINE_OPTIONS_ROLES = ["DataSteward", "PlatformAdmin", "SemanticAdmin"];

const joinOr = (items: readonly string[]): string =>
  items.length < 2 ? (items[0] ?? "") : `${items.slice(0, -1).join(", ")} or ${items[items.length - 1]}`;

/**
 * The one sentence a picker shows in place of a list this session may not read.
 *
 * Naming the roles says what to ask for. `carried` is what the draft already
 * holds: a new version is pre-filled from its base and `definitionBody` sends
 * what the draft holds, so those references STAY -- and saying so matters,
 * because an empty control otherwise reads as "this version will have none".
 */
function withheldReason(
  roles: readonly string[],
  what: string,
  verb: string,
  carried: number,
  carriedPhrase: string,
): string {
  return (
    `Only sessions holding ${joinOr(roles)} can read ${what}, so none can be ${verb} here; yours holds none of those.` +
    (carried > 0 ? ` The ${carried} already ${carriedPhrase} stay ${verb}.` : "")
  );
}

/** Every picker's options, loaded once per project. The same reads the
 *  screens that own each object make, so a draft -- a new product's or a new
 *  version's -- can only name what the platform has already approved. */
function useGovernedReferenceOptions(orgId: string, projectId: string | null) {
  // Each read is held while `/v1/me` is in flight and sent only once identity has answered,
  // admitted or unavailable (`readDecision`, `lib/roles.ts`); the server's 403 stays the
  // authority. A session known to be outside a list is never asked.
  const session = useSession();
  const ontologyRead = readDecision(session, ONTOLOGY_READ_ROLES);
  const routinesRead = readDecision(session, ROUTINE_OPTIONS_ROLES);
  const mayReadOntology = ontologyRead !== "skip";
  const mayReadRoutines = routinesRead !== "skip";
  // A held read is loading, not empty: an empty list reads as "nothing has been approved",
  // which is a claim about the estate that nobody has checked yet.
  const ontologyHeld = ontologyRead === "wait";
  const routinesHeld = routinesRead === "wait";

  const tableOptions = usePickerOptions(
    (signal) => fetchCatalogRows({ organizationId: orgId, limit: 200 }, signal).then((p) => p.items),
    (row) => ({
      id: row.id,
      label: `${row.schema_name}.${row.name}`,
      hint: row.owner ? `owner ${row.owner}` : "unowned",
      badge: row.certification === "CERTIFIED" ? "certified" : undefined,
    }),
    [orgId],
  );

  const semanticOptions = usePickerOptions(
    (signal) => fetchSemanticModelVersions(projectId ?? "", { limit: 200 }, signal).then((p) => p.items),
    (v) => ({ id: v.id, label: v.name, hint: `v${v.version}`, badge: v.status.toLowerCase() }),
    [projectId],
    { enabled: Boolean(projectId) },
  );

  const glossaryOptions = usePickerOptions(
    (signal) => listGlossaryTerms(orgId, { status: "APPROVED", limit: 200 }, signal).then((p) => p.items),
    (term) => ({ id: term.id, label: term.display_name, hint: term.term_key, badge: `v${term.version}` }),
    [orgId],
  );

  const toolOptions = usePickerOptions(
    (signal) => fetchTools(projectId ?? "", { status: "PUBLISHED", limit: 200 }, signal).then((p) => p.items),
    (tool) => ({ id: tool.id, label: tool.name, hint: `${tool.slug} v${tool.version}`, badge: tool.status.toLowerCase() }),
    [projectId],
    { enabled: Boolean(projectId) },
  );

  const routineOptions = usePickerOptions(
    (signal) => fetchContextProductRoutineOptions(projectId ?? "", signal),
    (routine) => ({
      id: routine.id,
      label: `${routine.schema_name}.${routine.name}${routine.signature}`,
      hint: `${routine.routine_type.toLowerCase()} on ${routine.datasource_name}`,
    }),
    [projectId],
    { enabled: Boolean(projectId) && routinesRead === "ask" },
  );

  /* R11-FP09: only APPROVED versions can be bound; the picker offers exactly those. */
  const ontologyOptions = usePickerOptions(
    (signal) =>
      listOntologyVersions(orgId, 0, signal).then((rows) => rows.filter((row) => row.status === "APPROVED")),
    (row) => ({
      id: row.id,
      label: `${row.ontology_key} v${row.version}`,
      hint: row.published_version === row.version ? "current published version" : "earlier approved version",
    }),
    [orgId],
    { enabled: ontologyRead === "ask" },
  );

  return {
    tableOptions, semanticOptions, glossaryOptions, toolOptions, routineOptions, ontologyOptions,
    mayReadOntology, mayReadRoutines, ontologyHeld, routinesHeld,
  };
}

/** The six reference pickers, bound to one draft's id arrays. Shared by the
 *  create panel and the new-version panel so the two cannot drift apart --
 *  above all the routine picker, which is the reason the second one exists. */
function GovernedReferencePickers({
  orgId,
  projectId,
  value,
  onChange,
}: {
  orgId: string;
  projectId: string | null;
  value: Pick<DraftState, ReferenceKey>;
  onChange: (key: ReferenceKey, ids: string[]) => void;
}) {
  const {
    tableOptions, semanticOptions, glossaryOptions, toolOptions, routineOptions, ontologyOptions,
    mayReadOntology, mayReadRoutines, ontologyHeld, routinesHeld,
  } = useGovernedReferenceOptions(orgId, projectId);
  // One honest sentence and no control, for each picker this session may not read.
  const ontologyUnavailableReason = mayReadOntology
    ? undefined
    : withheldReason(ONTOLOGY_READ_ROLES, "ontology versions", "bound", value.ontologyVersionIds.length, "bound to the previous version");
  const routinesUnavailableReason = mayReadRoutines
    ? undefined
    : withheldReason(ROUTINE_OPTIONS_ROLES, "stored procedures and functions", "named", value.routineIds.length, "named by the previous version");
  return (
    <>
      <div className="cpform__span2">
        <ReferencePicker
          label="Governed tables"
          options={tableOptions.options}
          loading={tableOptions.loading}
          error={tableOptions.error}
          selected={value.tableIds}
          onChange={(ids) => onChange("tableIds", ids)}
          searchPlaceholder="Filter by table or schema…"
          emptyHint="This organization has no catalogued tables yet. Run a scan from Sources first."
        />
      </div>
      <div className="cpform__span2">
        <ReferencePicker
          label="Semantic model versions"
          options={semanticOptions.options}
          loading={semanticOptions.loading}
          error={semanticOptions.error}
          selected={value.semanticIds}
          onChange={(ids) => onChange("semanticIds", ids)}
          emptyHint="No semantic model versions in this project yet."
          visibleRows={4}
        />
      </div>
      <div className="cpform__span2">
        <ReferencePicker
          label="Glossary terms"
          options={glossaryOptions.options}
          loading={glossaryOptions.loading}
          error={glossaryOptions.error}
          selected={value.glossaryIds}
          onChange={(ids) => onChange("glossaryIds", ids)}
          emptyHint="No approved glossary terms yet. Author them in Business meaning."
          visibleRows={4}
        />
      </div>
      <div className="cpform__span2">
        <ReferencePicker
          label="Eligible tools"
          options={toolOptions.options}
          loading={toolOptions.loading}
          error={toolOptions.error}
          selected={value.toolIds}
          onChange={(ids) => onChange("toolIds", ids)}
          emptyHint="No published tools in this project. Publish one from Tool registry."
          visibleRows={4}
        />
      </div>
      <div className="cpform__span2">
        <ReferencePicker
          label="Stored procedures and functions"
          options={routineOptions.options}
          loading={routineOptions.loading || routinesHeld}
          error={routineOptions.error}
          selected={value.routineIds}
          onChange={(ids) => onChange("routineIds", ids)}
          emptyHint="No active routines on this project's sources. Scan a source that exposes them first."
          visibleRows={4}
          unavailableReason={routinesUnavailableReason}
        />
      </div>
      <div className="cpform__span2">
        <ReferencePicker
          label="Ontology versions"
          options={ontologyOptions.options}
          loading={ontologyOptions.loading || ontologyHeld}
          error={ontologyOptions.error}
          selected={value.ontologyVersionIds}
          onChange={(ids) => onChange("ontologyVersionIds", ids)}
          emptyHint="No approved ontology versions yet. Publish one from Unified lineage, Manage ontology."
          visibleRows={4}
          unavailableReason={ontologyUnavailableReason}
        />
      </div>
    </>
  );
}

/** The governance fields both panels send, from one draft. `routine_ids` and
 *  `ontology_version_ids` only when one is picked, so a body without them is
 *  byte-for-byte the body the create call always sent. */
function definitionBody(draft: DraftState) {
  return {
    name: draft.name,
    description: draft.description,
    purpose: draft.purpose,
    owner_type: draft.ownerType,
    owner_principal: draft.ownerPrincipal,
    table_ids: draft.tableIds,
    semantic_model_version_ids: draft.semanticIds,
    glossary_term_version_ids: draft.glossaryIds,
    eligible_tool_version_ids: draft.toolIds,
    ...(draft.routineIds.length > 0 ? { routine_ids: draft.routineIds } : {}),
    ...(draft.ontologyVersionIds.length > 0 ? { ontology_version_ids: draft.ontologyVersionIds } : {}),
    allowed_consumer_roles: splitList(draft.consumerRoles),
    lineage_depth: Number(draft.lineageDepth || 2),
    quality_requirements: {
      minimum_score: Number(draft.minimumScore || 0),
      deny_on_critical_incident: draft.denyOnCriticalIncident,
    },
  };
}

/** Every field a definition carries, in the order the create form always
 *  showed them. `leading` is the create form's stable key: a new version keeps
 *  its product's key, so it has none. */
function DraftFields({
  orgId,
  projectId,
  draft,
  setField,
  leading = null,
}: {
  orgId: string;
  projectId: string | null;
  draft: DraftState;
  setField: <K extends keyof DraftState>(key: K, value: DraftState[K]) => void;
  leading?: React.ReactNode;
}) {
  return (
    <div className="cpform__grid">
      {leading}
      <Field label="Name">
        <input
          required
          minLength={3}
          placeholder="Consumer risk analysis"
          value={draft.name}
          onChange={(e) => setField("name", e.target.value)}
        />
      </Field>
      <Field label="Owner type">
        <select
          value={draft.ownerType}
          onChange={(e) => setField("ownerType", e.target.value as DraftState["ownerType"])}
        >
          <option value="GROUP">Group</option>
          <option value="INDIVIDUAL">Individual</option>
        </select>
      </Field>
      <Field label="Owner principal">
        <input
          required
          minLength={2}
          placeholder="risk-data-stewards"
          value={draft.ownerPrincipal}
          onChange={(e) => setField("ownerPrincipal", e.target.value)}
        />
      </Field>
      <div className="cpform__span2">
        <Field label="Description">
          <textarea
            required
            minLength={3}
            rows={2}
            placeholder="What this package contains"
            value={draft.description}
            onChange={(e) => setField("description", e.target.value)}
          />
        </Field>
      </div>
      <div className="cpform__span2">
        <Field label="Approved purpose">
          <textarea
            required
            minLength={10}
            rows={2}
            placeholder="Bounded purpose for agent and analyst consumption"
            value={draft.purpose}
            onChange={(e) => setField("purpose", e.target.value)}
          />
        </Field>
      </div>
      <GovernedReferencePickers
        orgId={orgId}
        projectId={projectId}
        value={draft}
        onChange={(key, ids) => setField(key, ids)}
      />
      <Field label="Consumer roles">
        <input required value={draft.consumerRoles} onChange={(e) => setField("consumerRoles", e.target.value)} />
      </Field>
      <Field label="Lineage depth">
        <input
          type="number"
          min={0}
          max={4}
          value={draft.lineageDepth}
          onChange={(e) => setField("lineageDepth", e.target.value)}
        />
      </Field>
      <Field label="Minimum quality score">
        <input
          type="number"
          min={0}
          max={100}
          value={draft.minimumScore}
          onChange={(e) => setField("minimumScore", e.target.value)}
        />
      </Field>
      <label className="cpform__checkbox cpform__span2">
        <input
          type="checkbox"
          checked={draft.denyOnCriticalIncident}
          onChange={(e) => setField("denyOnCriticalIncident", e.target.checked)}
        />
        Deny consumption while a referenced table has an active critical incident
      </label>
    </div>
  );
}

export function CreateDraftPanel({
  orgId,
  projectId,
  channel,
  onCreated,
}: {
  orgId: string;
  projectId: string | null;
  channel: StatusChannel;
  onCreated: () => void;
}) {
  const [draft, setDraft] = useState<DraftState>(INITIAL_DRAFT);
  const [creating, setCreating] = useState(false);
  const setField = useCallback(<K extends keyof DraftState>(key: K, value: DraftState[K]) => {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }, []);

  const submit = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (!projectId) {
        channel.failure("Select a project before creating a draft.");
        return;
      }
      const body: ContextProductCreate = {
        product_key: draft.productKey,
        ...definitionBody(draft),
        policy_summary: {
          source_values: "GATEWAY_ONLY",
          retention: "NO_RAW_CONTEXT",
          permitted_actions: ["READ_CONTEXT", "INVOKE_ELIGIBLE_TOOLS"],
        },
      };
      setCreating(true);
      channel.info("Validating governed references...");
      try {
        await createContextProduct(projectId, body);
        setDraft(INITIAL_DRAFT);
        channel.success("Draft created. Submit it for independent review when ready.");
        onCreated();
      } catch (reason) {
        channel.failure(reason);
      } finally {
        setCreating(false);
      }
    },
    [projectId, draft, channel, onCreated],
  );

  return (
    <article className="cpform">
      <form onSubmit={(e) => void submit(e)}>
        <header className="cpform__head">
          <div>
            <p className="cpform__eyebrow">NEW GOVERNED PACKAGE</p>
            <h2 className="cpform__h2">Create draft</h2>
            <p className="cpform__lede">
              Assemble only approved tables, semantics, glossary terms, and eligible tools into one bounded package.
            </p>
          </div>
          <Pill tone="warn">DRAFT</Pill>
        </header>

        <DraftFields
          orgId={orgId}
          projectId={projectId}
          draft={draft}
          setField={setField}
          leading={
            <Field label="Stable key">
              <input
                required
                pattern="[a-z][a-z0-9_-]{1,99}"
                placeholder="consumer-risk-context"
                value={draft.productKey}
                onChange={(e) => setField("productKey", e.target.value)}
              />
            </Field>
          }
        />

        <p className="cpform__privacy">
          The control plane stores identifiers and approved metadata only. Source values remain gateway-only and are
          never retained in Context Products.
        </p>

        <Button type="submit" variant="primary" disabled={creating || !projectId}>
          {creating ? "Creating…" : "Create governed draft"}
        </Button>
      </form>
    </article>
  );
}

/* ---------------------------------------------------------------------------
   New version -- R11-FP12.

   A product's next version starts from its latest one: every field and every
   pinned reference pre-filled, the stable key fixed (it is the product), and
   `based_on_version_id` naming the base so the review shows what changed. The
   draft it creates is a DRAFT like any other and reaches consumers only through
   Submit and an independent approval; nothing here publishes.

   The base's `policy_summary` and `support_window_days` are carried unchanged
   rather than offered as fields: the create form fixes the first to
   gateway-only for the reason its header gives, and neither is something a
   steward should change by accident while adding a routine.
--------------------------------------------------------------------------- */

/** The draft state a new version starts from: its base, exactly. */
function draftFromVersion(product: ContextProductRead): DraftState {
  const base = product.latest_version;
  const quality = base.quality_requirements;
  return {
    productKey: product.product_key,
    name: base.name,
    ownerType: base.owner_type,
    ownerPrincipal: base.owner_principal,
    description: base.description,
    purpose: base.purpose,
    tableIds: [...(base.table_ids ?? [])],
    semanticIds: [...(base.semantic_model_version_ids ?? [])],
    glossaryIds: [...(base.glossary_term_version_ids ?? [])],
    toolIds: [...(base.eligible_tool_version_ids ?? [])],
    routineIds: [...(base.routine_ids ?? [])],
    ontologyVersionIds: [...(base.ontology_version_ids ?? [])],
    consumerRoles: base.allowed_consumer_roles.join(", "),
    lineageDepth: String(base.lineage_depth ?? 2),
    minimumScore: String(quality?.minimum_score ?? 0),
    denyOnCriticalIncident: quality?.deny_on_critical_incident ?? true,
  };
}

/** `POST /v1/context-products/{product_id}/versions` (`create_context_product_version`).
 *  Demo mode answers with the draft the server would create, and sends nothing. */
function createContextProductVersion(
  product: ContextProductRead,
  body: ContextProductVersionCreate,
): Promise<ContextProductVersionRead> {
  const base = product.latest_version;
  return demoOr(
    async () => ({
      ...base,
      ...body,
      id: `${base.id}-next`,
      version: base.version + 1,
      status: "DRAFT",
      approved_by: null,
      approved_at: null,
      published_at: null,
      based_on_version_id: base.id,
    }),
    () => postJson<ContextProductVersionRead>(`/v1/context-products/${product.id}/versions`, body),
  );
}

export function NewVersionPanel({
  orgId,
  product,
  channel,
  onCreated,
  onClose,
}: {
  orgId: string;
  product: ContextProductRead;
  channel: StatusChannel;
  onCreated: () => void;
  onClose: () => void;
}) {
  const base = product.latest_version;
  const [draft, setDraft] = useState<DraftState>(() => draftFromVersion(product));
  const [creating, setCreating] = useState(false);
  const setField = useCallback(<K extends keyof DraftState>(key: K, value: DraftState[K]) => {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }, []);

  const submit = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      const body: ContextProductVersionCreate = {
        ...definitionBody(draft),
        policy_summary: base.policy_summary,
        support_window_days: base.support_window_days,
        based_on_version_id: base.id,
      };
      setCreating(true);
      channel.info("Validating governed references...");
      try {
        const created = await createContextProductVersion(product, body);
        channel.success(
          `Draft v${created.version} created from v${base.version}. Submit it for independent review when ready.`,
        );
        onCreated();
      } catch (reason) {
        channel.failure(reason);
      } finally {
        setCreating(false);
      }
    },
    [draft, base, product, channel, onCreated],
  );

  return (
    <article className="cpform" aria-label={`New version of ${product.product_key}`}>
      <form onSubmit={(e) => void submit(e)}>
        <header className="cpform__head">
          <div>
            <p className="cpform__eyebrow">NEXT VERSION</p>
            <h2 className="cpform__h2">
              New version of {product.product_key}
            </h2>
            <p className="cpform__lede">
              Starts from v{base.version}. Change what it names -- tables, semantics, terms, tools, routines,
              ontology -- and submit the draft for review; the published version keeps serving until then.
            </p>
          </div>
          <div className="cprollout__headactions">
            <Pill tone="warn">DRAFT</Pill>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
          </div>
        </header>

        <DraftFields orgId={orgId} projectId={product.project_id} draft={draft} setField={setField} />

        <Button type="submit" variant="primary" disabled={creating}>
          {creating ? "Creating…" : "Create version draft"}
        </Button>
      </form>
    </article>
  );
}
