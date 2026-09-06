import { useCallback, useEffect, useRef, useState } from "react";
import type {
  DataSourceRead,
  DbtArtifactImportRead,
  DbtArtifactImportRequest,
  DbtProjectCreate,
  DbtProjectRead,
} from "../lib/types";
import { createDbtProject, importDbtManifest } from "../lib/api";
import { Button, Field } from "../components/primitives";
import { FormError, useSubmitAction } from "../components/screenState";

/* ---------------------------------------------------------------------------
   The two ways transformation metadata enters Atlas, and the one reason it
   cannot.

   Registering a dbt project and importing a manifest are one unit: the
   registration is what an import attaches to, and neither is reachable unless
   the organization's integration policy has a transformation-metadata adapter
   turned on. `DbtDisabledState` therefore belongs here rather than with the
   reading surfaces -- it is the answer to "why can I not ingest anything",
   and it prints the backend's own refusal (`_require_dbt_integration`,
   `dbt_api.py:138`) rather than a generic error banner, matching legacy's own
   `renderDbtDisabledState()`.

   Both forms state what is retained before they are submitted, because both
   ingest something a user could reasonably fear is being kept: a repository
   URL is stored without credentials, and an artifact is reduced to resource
   metadata, column descriptions, catalog types, test outcomes and
   literal-redacted SQL -- the raw file is never persisted.
--------------------------------------------------------------------------- */

export function DbtDisabledState({ detail }: { detail: string }) {
  return (
    <div className="txdisabled" role="alert">
      <div className="txdisabled__title">Transformation metadata is unavailable for this organization</div>
      <p className="txdisabled__body">{detail}</p>
      <p className="txdisabled__hint">
        An administrator enables transformation-metadata adapters from the legacy portal&rsquo;s
        Administration &rarr; &ldquo;Transformation metadata surfaces&rdquo; panel
        (<code>PUT /v1/organizations/&#123;id&#125;/integration-policy</code>). That control has not
        been ported to this shell&rsquo;s Administration screen yet (a stated scope cut there), so
        enabling dbt currently requires the legacy portal at :3000.
      </p>
    </div>
  );
}

export function RegisterProjectForm({
  orgProjectId,
  datasources,
  onCreated,
}: {
  orgProjectId: string;
  datasources: DataSourceRead[];
  onCreated: (project: DbtProjectRead) => void;
}) {
  const [projectKey, setProjectKey] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [datasourceId, setDatasourceId] = useState("");
  const [targetName, setTargetName] = useState("prod");
  const [repositoryUrl, setRepositoryUrl] = useState("");
  const action = useSubmitAction<DbtProjectRead>();

  // Keep a selection that still exists: switching delivery project replaces
  // the source list, and a stale id would post a datasource from the previous
  // project's estate.
  useEffect(() => {
    setDatasourceId((current) => (datasources.some((d) => d.id === current) ? current : (datasources[0]?.id ?? "")));
  }, [datasources]);

  const submit = useCallback(async () => {
    if (!datasourceId) {
      action.fail("This delivery project has no registered warehouse source to bind the dbt project to.");
      return;
    }
    const body: DbtProjectCreate = {
      project_key: projectKey,
      display_name: displayName,
      datasource_id: datasourceId,
      repository_url: repositoryUrl.trim() || null,
      target_name: targetName.trim() || "prod",
    };
    const created = await action.run(() => createDbtProject(orgProjectId, body));
    if (!created) return;
    setProjectKey("");
    setDisplayName("");
    setRepositoryUrl("");
    setTargetName("prod");
    onCreated(created);
  }, [orgProjectId, projectKey, displayName, datasourceId, targetName, repositoryUrl, action, onCreated]);

  return (
    <form
      className="txform"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className="txform__grid">
        <Field label="Project key">
          <input
            required
            pattern="[a-z][a-z0-9_-]{1,99}"
            placeholder="consumer_analytics"
            value={projectKey}
            onChange={(e) => setProjectKey(e.target.value)}
          />
        </Field>
        <Field label="Display name">
          <input
            required
            minLength={2}
            placeholder="Consumer analytics transformations"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </Field>
        <Field label="Warehouse data source">
          <select required value={datasourceId} onChange={(e) => setDatasourceId(e.target.value)}>
            {datasources.length === 0 ? <option value="">No sources in project</option> : null}
            {datasources.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Target name">
          <input required value={targetName} onChange={(e) => setTargetName(e.target.value)} />
        </Field>
        <div className="txform__span2">
          <Field label="Repository URL (optional)">
            <input
              placeholder="https://git.example/bank/consumer-analytics"
              value={repositoryUrl}
              onChange={(e) => setRepositoryUrl(e.target.value)}
            />
          </Field>
        </div>
      </div>
      <p className="txform__privacy">
        Repository credentials are not accepted. This registration stores only ownership and warehouse mapping.
      </p>
      {action.error ? <FormError detail={action.error} /> : null}
      <Button type="submit" variant="primary" disabled={action.submitting}>
        {action.submitting ? "Registering…" : "Register dbt project"}
      </Button>
    </form>
  );
}

/** dbt writes very large manifests; the backend rejects beyond this. */
const MAX_MANIFEST_BYTES = 32 * 1024 * 1024;

async function readJsonFile(file: File, label: string): Promise<Record<string, unknown>> {
  try {
    return JSON.parse(await file.text()) as Record<string, unknown>;
  } catch {
    throw new Error(`The ${label} file is not valid JSON.`);
  }
}

export function ImportManifestForm({
  dbtProjectId,
  onImported,
}: {
  dbtProjectId: string;
  onImported: (artifact: DbtArtifactImportRead) => void;
}) {
  const manifestRef = useRef<HTMLInputElement>(null);
  const catalogRef = useRef<HTMLInputElement>(null);
  const runResultsRef = useRef<HTMLInputElement>(null);
  const action = useSubmitAction<DbtArtifactImportRead>();

  const submit = useCallback(async () => {
    const manifestFile = manifestRef.current?.files?.[0];
    if (!manifestFile) {
      action.fail("Choose a dbt manifest.json file.");
      return;
    }
    if (manifestFile.size > MAX_MANIFEST_BYTES) {
      action.fail("The manifest exceeds the 32 MiB ingestion limit.");
      return;
    }
    // Reading and parsing happen inside the action, not before it: parsing a
    // 32 MiB manifest is slow enough that a form which looked idle until the
    // request started invited a second click.
    const imported = await action.run(async () => {
      const catalogFile = catalogRef.current?.files?.[0];
      const runResultsFile = runResultsRef.current?.files?.[0];
      const body: DbtArtifactImportRequest = {
        manifest: await readJsonFile(manifestFile, "manifest.json"),
        catalog: catalogFile ? await readJsonFile(catalogFile, "catalog.json") : null,
        run_results: runResultsFile ? await readJsonFile(runResultsFile, "run_results.json") : null,
      };
      return importDbtManifest(dbtProjectId, body);
    });
    if (!imported) return;
    if (manifestRef.current) manifestRef.current.value = "";
    if (catalogRef.current) catalogRef.current.value = "";
    if (runResultsRef.current) runResultsRef.current.value = "";
    onImported(imported);
  }, [dbtProjectId, action, onImported]);

  return (
    <form
      className="txform"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <div className="txform__grid">
        <div className="txform__span2">
          <Field label="manifest.json (required)">
            <input ref={manifestRef} type="file" accept="application/json,.json" required />
          </Field>
        </div>
        <Field label="catalog.json (optional — physical types)">
          <input ref={catalogRef} type="file" accept="application/json,.json" />
        </Field>
        <Field label="run_results.json (optional — test results)">
          <input ref={runResultsRef} type="file" accept="application/json,.json" />
        </Field>
      </div>
      <p className="txform__privacy">
        Atlas retains resource metadata, column descriptions, catalog data types, test execution
        outcomes, and literal-redacted compiled SQL. Raw artifacts are not persisted.
      </p>
      {action.error ? <FormError detail={action.error} /> : null}
      <Button type="submit" variant="primary" disabled={action.submitting}>
        {action.submitting ? "Validating artifact…" : "Validate and import"}
      </Button>
    </form>
  );
}
