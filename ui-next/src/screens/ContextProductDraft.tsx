import { useCallback, useState } from "react";
import type { ContextProductCreate } from "../lib/types";
import { createContextProduct, fetchCatalogRows, fetchSemanticModelVersions, fetchTools } from "../lib/api";
import { listGlossaryTerms } from "../lib/_api_append";
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

   Tables are organization-scoped; semantics, glossary terms and tools are
   project-scoped, so those three stay empty (and say so) until a project is
   chosen, rather than offering another project's objects.

   `policy_summary` is fixed to gateway-only / no-raw-context, exactly as the
   legacy form hard-codes it: a Context Product is a description of approved
   metadata, and the source values it describes are never part of it.
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
  consumerRoles: "Analyst",
  lineageDepth: "2",
  minimumScore: "85",
  denyOnCriticalIncident: true,
};

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

  const submit = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (!projectId) {
        channel.failure("Select a project before creating a draft.");
        return;
      }
      const body: ContextProductCreate = {
        product_key: draft.productKey,
        name: draft.name,
        description: draft.description,
        purpose: draft.purpose,
        owner_type: draft.ownerType,
        owner_principal: draft.ownerPrincipal,
        table_ids: draft.tableIds,
        semantic_model_version_ids: draft.semanticIds,
        glossary_term_version_ids: draft.glossaryIds,
        eligible_tool_version_ids: draft.toolIds,
        allowed_consumer_roles: splitList(draft.consumerRoles),
        lineage_depth: Number(draft.lineageDepth || 2),
        quality_requirements: {
          minimum_score: Number(draft.minimumScore || 0),
          deny_on_critical_incident: draft.denyOnCriticalIncident,
        },
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

        <div className="cpform__grid">
          <Field label="Stable key">
            <input
              required
              pattern="[a-z][a-z0-9_-]{1,99}"
              placeholder="consumer-risk-context"
              value={draft.productKey}
              onChange={(e) => setField("productKey", e.target.value)}
            />
          </Field>
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
          <div className="cpform__span2">
            <ReferencePicker
              label="Governed tables"
              options={tableOptions.options}
              loading={tableOptions.loading}
              error={tableOptions.error}
              selected={draft.tableIds}
              onChange={(ids) => setField("tableIds", ids)}
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
              selected={draft.semanticIds}
              onChange={(ids) => setField("semanticIds", ids)}
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
              selected={draft.glossaryIds}
              onChange={(ids) => setField("glossaryIds", ids)}
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
              selected={draft.toolIds}
              onChange={(ids) => setField("toolIds", ids)}
              emptyHint="No published tools in this project. Publish one from Tool registry."
              visibleRows={4}
            />
          </div>
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
