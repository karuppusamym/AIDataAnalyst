/* ---------------------------------------------------------------------------
   Transformations — dbt projects, artifact imports, the resources a manifest
   declares, and the lineage parsed out of them.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson } from "./transport";
import type {
  DbtArtifactImportRead,
  DbtArtifactImportRequest,
  DbtLineageRead,
  DbtProjectCreate,
  DbtProjectRead,
} from "../types";
import type { PageOf } from "../ui-types";

/* ---------------------------------------------------------------------------
   Transformations -- nav id `transformations`, `TransformationsScreen`'s own
   routes (`src/aida/dbt_api.py`). See that screen's file-top comment for how
   this screen was actually located (the nav button is real but hidden
   behind an organization integration-policy flag, not missing) and for what
   was deliberately left out of scope (the legacy Cytoscape DAG canvas).
--------------------------------------------------------------------------- */

/** `GET /v1/projects/{project_id}/dbt-projects` (`list_dbt_projects`,
 *  `dbt_api.py:200`) -- delivery-project-scoped dbt project registrations,
 *  the same scoping level `fetchOrgProjects`'s callers already use one level
 *  up. Every dbt route this file calls scopes through `_project_scope`/
 *  `_dbt_project_scope`/`_artifact_scope` (`dbt_api.py:58-92`), each of
 *  which calls `_require_dbt_integration` (`dbt_api.py:138`) before doing
 *  anything else -- so THIS call, not only the create calls below, 403s with
 *  `"dbt integration is disabled for this organization"` whenever the
 *  organization's integration policy has not enabled dbt. Callers should
 *  render that detail string, not a generic error (`TransformationsScreen`'s
 *  `DbtDisabledState` does). */
export function fetchDbtProjects(
  projectId: string,
  signal?: AbortSignal,
): Promise<PageOf<DbtProjectRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureDbtProjects(projectId),
    async () => {
      return get<PageOf<DbtProjectRead>>(`/v1/projects/${projectId}/dbt-projects?limit=500`, signal);
    },
  );
}

/** `POST /v1/projects/{project_id}/dbt-projects` (`create_dbt_project`,
 *  `dbt_api.py:149`) -- registers ownership + warehouse mapping only. No
 *  repository credentials accepted, matching the legacy dialog's own
 *  privacy note (`ui/index.html#dbt-project-dialog`). */
export function createDbtProject(
  projectId: string,
  body: DbtProjectCreate,
  signal?: AbortSignal,
): Promise<DbtProjectRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCreateDbtProject(projectId, body),
    async () => {
      return postJson<DbtProjectRead>(`/v1/projects/${projectId}/dbt-projects`, body, signal);
    },
  );
}

/** `GET /v1/dbt-projects/{dbt_project_id}/artifact-imports`
 *  (`list_dbt_artifact_imports`, `dbt_api.py:432`) -- one dbt project's
 *  immutable import history, newest first. */
export function fetchDbtArtifactImports(
  dbtProjectId: string,
  signal?: AbortSignal,
): Promise<PageOf<DbtArtifactImportRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureDbtArtifactImports(dbtProjectId),
    async () => {
      return get<PageOf<DbtArtifactImportRead>>(
        `/v1/dbt-projects/${dbtProjectId}/artifact-imports?limit=100`,
        signal,
      );
    },
  );
}

/** `POST /v1/dbt-projects/{dbt_project_id}/artifact-imports`
 *  (`import_dbt_manifest`, `dbt_api.py:232`). The body carries the already
 *  PARSED JSON of manifest.json (required) plus optional catalog.json /
 *  run_results.json -- reading those `File` objects and calling
 *  `JSON.parse` is the screen's job (matching the legacy form's own
 *  `FileReader`/`JSON.parse`, `ui/app.js`'s `#dbt-import-form` handler);
 *  this call only posts the already-parsed objects. Idempotent by manifest
 *  fingerprint server-side: re-importing an unchanged manifest returns the
 *  existing artifact instead of creating a duplicate (`dbt_api.py:265-271`). */
export function importDbtManifest(
  dbtProjectId: string,
  body: DbtArtifactImportRequest,
  signal?: AbortSignal,
): Promise<DbtArtifactImportRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureImportDbtManifest(dbtProjectId, body),
    async () => {
      return postJson<DbtArtifactImportRead>(
        `/v1/dbt-projects/${dbtProjectId}/artifact-imports`,
        body,
        signal,
      );
    },
  );
}

/** Mirrors `schemas.py`'s `DbtResourceRead` (`dbt_api.py:467`'s response
 *  item shape). Not added to the shared `types.ts` -- `TransformationsScreen`
 *  is this port's only consumer, matching this file's existing precedent for
 *  a response shape with one caller (`AgentAskError`, `ReviewQueueQuery`)
 *  rather than growing the large shared file for it. */
export interface DbtResourceRead {
  id: string;
  artifact_import_id: string;
  unique_id: string;
  resource_type: string;
  package_name: string;
  name: string;
  database_name: string | null;
  schema_name: string | null;
  relation_name: string | null;
  materialization: string | null;
  original_file_path: string | null;
  description: string | null;
  compiled_sql_hash: string | null;
  compiled_sql_redacted: string | null;
  sql_parse_status: string;
  column_names: string[];
  column_descriptions: Record<string, string>;
  column_types: Record<string, string>;
  tags: string[];
  depends_on_unique_ids: string[];
  matched_table_id: string | null;
  test_status: string | null;
  test_failures: number | null;
  test_execution_time: number | null;
  extra_metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface DbtResourceQuery {
  resourceType?: string | null;
  matched?: boolean | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/dbt-artifact-imports/{artifact_id}/resources`
 *  (`list_dbt_resources`, `dbt_api.py:467`) -- one immutable artifact's
 *  parsed models/sources/tests/seeds/snapshots, each carrying its catalog
 *  match, SQL-parse evidence, and (for TEST resources) the last reconciled
 *  execution outcome (`reconcile_dbt_test_quality`, `dbt_quality_bridge.py`). */
export function fetchDbtResources(
  artifactImportId: string,
  query: DbtResourceQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<DbtResourceRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureDbtResources(artifactImportId, query),
    async () => {
      const params = new URLSearchParams();
      if (query.resourceType) params.set("resource_type", query.resourceType);
      if (query.matched !== undefined && query.matched !== null) {
        params.set("matched", String(query.matched));
      }
      params.set("limit", String(query.limit ?? 500));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<DbtResourceRead>>(
        `/v1/dbt-artifact-imports/${artifactImportId}/resources?${params}`,
        signal,
      );
    },
  );
}

/** `GET /v1/dbt-artifact-imports/{artifact_id}/lineage` (`get_dbt_lineage`,
 *  `dbt_api.py:507`) -- table-level dependency edges (`edge_type
 *  DEPENDS_ON`) plus column-level edges (`COLUMN_DEPENDS_ON`,
 *  `dbt_column_lineage.py::extract_column_lineage`) across the same
 *  resource set `fetchDbtResources` returns for this artifact. */
export function fetchDbtLineage(
  artifactImportId: string,
  signal?: AbortSignal,
): Promise<DbtLineageRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureDbtLineage(artifactImportId),
    async () => {
      return get<DbtLineageRead>(`/v1/dbt-artifact-imports/${artifactImportId}/lineage?limit=2000`, signal);
    },
  );
}
