import type {
  DataSourceRead,
  GovernedToolVersionCreate,
  GovernedToolVersionRead,
  ToolParameterDefinition,
} from "../lib/types";
import { Button, Field, Pill } from "../components/primitives";
import { splitList } from "../components/screenState";

/* ---------------------------------------------------------------------------
   A governed tool as it is authored: the draft a steward types, the editor
   that produces it, and the one function that turns it into the
   `GovernedToolVersionCreate` body `tool_api.py:348` accepts.

   These three belong in one file because they are one contract seen from
   three sides. The draft holds every field as a string, because that is what
   an `<input>` yields; the request holds typed literals, because that is what
   the SQL boundary binds. `toToolVersionCreate` is the only place that
   crossing happens, and it is the only place that can explain a bad crossing
   to the person who typed it. It previously sat two hundred lines away from
   the editor, inside the screen's submit handler, so a parameter's editor and
   its serialization could disagree about what a field meant and nothing
   linked them.

   Selecting "New version" on an existing tool prefills this editor from that
   version exactly like legacy's `openToolAuthor(existing)` -- submitting under
   the same `slug` is what `_persist_tool_version_draft` (tool_api.py:201) uses
   to attach the draft to the existing `GovernedTool` as its next version, not
   a new tool.
--------------------------------------------------------------------------- */

export interface ParameterDraft {
  name: string;
  parameter_type: ToolParameterDefinition["parameter_type"];
  required: boolean;
  sensitive: boolean;
  allowedValues: string;
  defaultJson?: string;
  minimum?: number | null;
  maximum?: number | null;
  max_length?: number | null;
}

export const blankParameter = (): ParameterDraft => ({
  name: "",
  parameter_type: "STRING",
  required: true,
  sensitive: false,
  allowedValues: "",
});

export interface ToolDraft {
  slug: string;
  name: string;
  datasourceId: string;
  allowedRoles: string;
  description: string;
  sqlTemplate: string;
  semanticModelVersionId?: string | null;
}

export const INITIAL_DRAFT: ToolDraft = {
  slug: "",
  name: "",
  datasourceId: "",
  allowedRoles: "Analyst,ToolConsumer",
  description: "",
  sqlTemplate: "",
};

/** The draft an existing version would produce, for "New version". */
export function draftFromVersion(tool: GovernedToolVersionRead): {
  draft: ToolDraft;
  parameters: ParameterDraft[];
} {
  return {
    draft: {
      slug: tool.slug,
      name: tool.name,
      datasourceId: tool.datasource_id,
      allowedRoles: tool.allowed_roles.join(","),
      description: tool.description,
      sqlTemplate: tool.sql_template,
      semanticModelVersionId: tool.semantic_model_version_id,
    },
    parameters: tool.parameters.length
      ? tool.parameters.map((p) => ({
          name: p.name,
          parameter_type: p.parameter_type,
          required: p.required ?? true,
          sensitive: p.sensitive ?? false,
          allowedValues: p.allowed_values ? JSON.stringify(p.allowed_values) : "",
          defaultJson: p.default == null ? "" : JSON.stringify(p.default),
          minimum: p.minimum,
          maximum: p.maximum,
          max_length: p.max_length,
        }))
      : [blankParameter()],
  };
}

function parseAllowedValues(parameter: ParameterDraft): unknown[] {
  const raw = parameter.allowedValues.trim();
  if (!raw) return [];
  if (raw.startsWith("[")) {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (!Array.isArray(parsed)) throw new Error("not an array");
      return parsed;
    } catch {
      // A raw SyntaxError ("Unexpected token ] in JSON at position 4") does
      // not tell the author which of their parameters is wrong.
      throw new Error(`Parameter "${parameter.name}": allowed values must be a JSON array.`);
    }
  }
  return splitList(raw).map((value) => {
    if (parameter.parameter_type === "NUMBER" || parameter.parameter_type === "INTEGER") {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) throw new Error("Allowed numeric values must be numbers");
      return numeric;
    }
    if (parameter.parameter_type === "BOOLEAN") {
      if (!["true", "false"].includes(value)) throw new Error("Boolean values must be true or false");
      return value === "true";
    }
    return value;
  });
}

function parseDefault(parameter: ParameterDraft): unknown {
  const raw = parameter.defaultJson?.trim();
  if (!raw) return undefined;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    throw new Error(`Parameter "${parameter.name}": the default must be JSON, e.g. "NY" or 10.`);
  }
}

/**
 * The draft as the create route wants it.
 *
 * Throws with a sentence naming the offending parameter; the caller reports
 * that in its status strip rather than posting a body the server would reject
 * with a less specific message.
 *
 * Optional constraints are omitted rather than sent as null: `minimum: null`
 * and "no minimum" are different requests, and only the second is meant.
 */
export function toToolVersionCreate(
  draft: ToolDraft,
  parameters: ParameterDraft[],
): GovernedToolVersionCreate {
  return {
    slug: draft.slug,
    name: draft.name,
    description: draft.description,
    datasource_id: draft.datasourceId,
    sql_template: draft.sqlTemplate,
    ...(draft.semanticModelVersionId ? { semantic_model_version_id: draft.semanticModelVersionId } : {}),
    parameters: parameters
      .filter((p) => p.name.trim().length > 0)
      .map((p) => {
        const allowed = parseAllowedValues(p);
        const fallback = parseDefault(p);
        return {
          name: p.name,
          parameter_type: p.parameter_type,
          required: p.required,
          sensitive: p.sensitive,
          ...(allowed.length ? { allowed_values: allowed } : {}),
          ...(fallback !== undefined ? { default: fallback } : {}),
          ...(p.minimum != null ? { minimum: p.minimum } : {}),
          ...(p.maximum != null ? { maximum: p.maximum } : {}),
          ...(p.max_length != null ? { max_length: p.max_length } : {}),
        };
      }),
    allowed_roles: splitList(draft.allowedRoles),
  };
}

function ParameterBuilder({
  parameters,
  onChange,
}: {
  parameters: ParameterDraft[];
  onChange: (next: ParameterDraft[]) => void;
}) {
  const update = (i: number, patch: Partial<ParameterDraft>) => {
    onChange(parameters.map((p, idx) => (idx === i ? { ...p, ...patch } : p)));
  };
  const remove = (i: number) => onChange(parameters.filter((_, idx) => idx !== i));

  return (
    <div className="trparams">
      {parameters.map((p, i) => (
        <div className="trparams__row" key={i}>
          <label>
            Name
            <input
              required
              pattern="[a-z][a-z0-9_]{0,63}"
              value={p.name}
              onChange={(e) => update(i, { name: e.target.value })}
            />
          </label>
          <label>
            Type
            <select
              value={p.parameter_type}
              onChange={(e) => update(i, { parameter_type: e.target.value as ParameterDraft["parameter_type"] })}
            >
              {(["STRING", "INTEGER", "NUMBER", "BOOLEAN", "DATE"] as const).map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <label className="trparams__check">
            <input type="checkbox" checked={p.required} onChange={(e) => update(i, { required: e.target.checked })} />
            Required
          </label>
          <label className="trparams__check">
            <input type="checkbox" checked={p.sensitive} onChange={(e) => update(i, { sensitive: e.target.checked })} />
            Sensitive
          </label>
          <label>
            Allowed values (JSON array or comma-separated)
            <input placeholder="NY,NJ" value={p.allowedValues} onChange={(e) => update(i, { allowedValues: e.target.value })} />
          </label>
          <label>
            Default (JSON)
            <input value={p.defaultJson ?? ""} placeholder='"NY" or 10' onChange={(e) => update(i, { defaultJson: e.target.value })} />
          </label>
          {(["minimum", "maximum", "max_length"] as const).map((key) => (
            <label key={key}>
              {key.replace("_", " ")}
              <input
                type="number"
                value={p[key] ?? ""}
                onChange={(e) => update(i, { [key]: e.target.value === "" ? null : Number(e.target.value) })}
              />
            </label>
          ))}
          <button type="button" className="trparams__remove" aria-label="Remove parameter" onClick={() => remove(i)}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

export function CreateToolPanel({
  projectId,
  datasourceOptions,
  draft,
  setDraft,
  parameters,
  setParameters,
  creating,
  onSubmit,
  editingSlug,
  onCancelEdit,
}: {
  projectId: string | null;
  datasourceOptions: DataSourceRead[];
  draft: ToolDraft;
  setDraft: (f: ToolDraft) => void;
  parameters: ParameterDraft[];
  setParameters: (p: ParameterDraft[]) => void;
  creating: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
  editingSlug: string | null;
  onCancelEdit: () => void;
}) {
  const setField = <K extends keyof ToolDraft>(key: K, value: ToolDraft[K]) => setDraft({ ...draft, [key]: value });

  return (
    <article className="trform">
      <form onSubmit={onSubmit}>
        <header className="trform__head">
          <div>
            <p className="trform__eyebrow">TOOL CONTRACT</p>
            <h2 className="trform__h2">{editingSlug ? `New version of ${editingSlug}` : "New tool version"}</h2>
            <p className="trform__lede">Bind values as typed SQL literals. Identifiers cannot be parameters.</p>
          </div>
          <Pill tone="warn">DRAFT</Pill>
        </header>

        <div className="trform__grid">
          <Field label="Stable slug">
            {/* The slug is what attaches a draft to an existing tool, so it is
                fixed while authoring a new version of one. */}
            <input
              required
              pattern="[a-z][a-z0-9_]{1,99}"
              placeholder="customer_lookup"
              value={draft.slug}
              disabled={editingSlug !== null}
              onChange={(e) => setField("slug", e.target.value)}
            />
          </Field>
          <Field label="Version name">
            <input
              required
              minLength={2}
              placeholder="Customer lookup"
              value={draft.name}
              onChange={(e) => setField("name", e.target.value)}
            />
          </Field>
          <Field label="Data source">
            <select required value={draft.datasourceId} onChange={(e) => setField("datasourceId", e.target.value)}>
              <option value="">
                {projectId
                  ? datasourceOptions.length
                    ? "Select a data source…"
                    : "No sources in project"
                  : "Select a project first"}
              </option>
              {datasourceOptions.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Allowed roles">
            <input
              required
              placeholder="Analyst,ToolConsumer"
              value={draft.allowedRoles}
              onChange={(e) => setField("allowedRoles", e.target.value)}
            />
          </Field>
          <div className="trform__span2">
            <Field label="Description">
              <textarea
                required
                minLength={3}
                rows={3}
                placeholder="Purpose, permitted use, and business owner"
                value={draft.description}
                onChange={(e) => setField("description", e.target.value)}
              />
            </Field>
          </div>
          <div className="trform__span2">
            <Field label="SQL template">
              <textarea
                required
                rows={8}
                spellCheck={false}
                placeholder="SELECT customer_id FROM public.customers WHERE state = :state"
                className="trform__sql"
                value={draft.sqlTemplate}
                onChange={(e) => setField("sqlTemplate", e.target.value)}
              />
            </Field>
          </div>
        </div>

        <div className="trform__subhead">
          <div>
            <h3>Parameters</h3>
            <p>Bind values as typed SQL literals. Identifiers cannot be parameters.</p>
          </div>
          <Button onClick={() => setParameters([...parameters, blankParameter()])}>Add parameter</Button>
        </div>
        <ParameterBuilder parameters={parameters} onChange={setParameters} />

        <div className="trform__actions">
          {editingSlug ? <Button onClick={onCancelEdit}>Cancel</Button> : null}
          <Button type="submit" variant="primary" disabled={creating || !projectId}>
            {creating ? "Creating…" : "Create draft version"}
          </Button>
        </div>
      </form>
    </article>
  );
}
