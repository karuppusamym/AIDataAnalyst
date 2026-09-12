/* ---------------------------------------------------------------------------
   Semantics — published semantic model and metric versions, and the
   consumers each one has.

   Project-scoped, not org-wide: there is no "browse every published model"
   endpoint, so a project is picked first (see `SemanticsScreen`).

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr, get } from "./transport";
import type {
  ConsumerFooterRead,
  SemanticMetricVersionRead,
  SemanticModelVersionRead,
} from "../types";
import type { PageOf } from "../ui-types";

/* ---------------------------------------------------------------------------
   Semantics (UX-15/UX-16, `semantics` nav id) — `SemanticsScreen`'s own
   endpoints. See that screen's file-top comment for the honest scope: there
   is no org-wide "browse every published semantic model" endpoint, so this
   is a project picker (`fetchOrgProjects`, the real
   `GET /v1/organizations/{id}/projects`) feeding project-scoped model/metric
   lists — the same composition shape `fetchOrgDatasources` (`./identity.ts`)
   already uses to bridge a display name to an id `unified-lineage` needs.
--------------------------------------------------------------------------- */


export interface SemanticPageQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/projects/{id}/semantic-model-versions` (`semantic_api.py::list_semantic_model_versions`)
 *  — project-scoped, not org-wide (see this file's Semantics banner comment). */
export function fetchSemanticModelVersions(
  projectId: string,
  opts: SemanticPageQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<SemanticModelVersionRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureSemanticModelVersions(projectId, opts),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(opts.limit ?? 100));
      params.set("offset", String(opts.offset ?? 0));
      return get<PageOf<SemanticModelVersionRead>>(
        `/v1/projects/${projectId}/semantic-model-versions?${params}`,
        signal,
      );
    },
  );
}

/** `GET /v1/semantic-model-versions/{id}/metrics` (`semantic_api.py::list_metric_versions`)
 *  — every metric version defined on one semantic model version. */
export function fetchSemanticMetricVersions(
  modelVersionId: string,
  opts: SemanticPageQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<SemanticMetricVersionRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureSemanticMetricVersions(modelVersionId, opts),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(opts.limit ?? 100));
      params.set("offset", String(opts.offset ?? 0));
      return get<PageOf<SemanticMetricVersionRead>>(
        `/v1/semantic-model-versions/${modelVersionId}/metrics?${params}`,
        signal,
      );
    },
  );
}

/** `GET /v1/semantic-model-versions/{id}/consumers` (UX-18, `semantic_api.py::
 *  get_semantic_model_version_consumers`) — who/what currently consumes this
 *  exact model version, from CX-4 consumption lineage. */
export function fetchSemanticModelConsumers(
  modelVersionId: string,
  signal?: AbortSignal,
): Promise<ConsumerFooterRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureSemanticModelConsumers(modelVersionId),
    async () => {
      return get<ConsumerFooterRead>(
        `/v1/semantic-model-versions/${modelVersionId}/consumers`,
        signal,
      );
    },
  );
}

/** `GET /v1/semantic-metric-versions/{id}/consumers` (UX-18, `semantic_api.py::
 *  get_semantic_metric_version_consumers`) — same composition as the model
 *  consumer footer above, scoped to one metric version. */
export function fetchSemanticMetricConsumers(
  metricVersionId: string,
  signal?: AbortSignal,
): Promise<ConsumerFooterRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureSemanticMetricConsumers(metricVersionId),
    async () => {
      return get<ConsumerFooterRead>(
        `/v1/semantic-metric-versions/${metricVersionId}/consumers`,
        signal,
      );
    },
  );
}
