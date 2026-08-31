/* ---------------------------------------------------------------------------
   Types mirroring the live API contract in src/aida/schemas.py.
   Kept hand-written and small on purpose: the moment this file drifts, the
   right fix is to generate it from the FastAPI OpenAPI document (see
   Docs/40-engineering — planned as UX-11), not to patch it by hand.
--------------------------------------------------------------------------- */

/** `CursorPage` — schemas.py:2926. `total` is null whenever a cursor was used,
 *  because keyset paging deliberately does not pay for a count. */
export interface CursorPage<T> {
  items: T[];
  limit: number;
  offset: number;
  total: number | null;
  next_cursor: string | null;
}

/** `MetadataTableRead` — schemas.py:781. Eight fields. This is the whole row
 *  the catalog list endpoint returns today. */
export interface MetadataTableRead {
  id: string;
  datasource_id: string;
  schema_id: string;
  name: string;
  object_type: string;
  status: string;
  fingerprint: string;
}

export type CertificationStatus = "CERTIFIED" | "EXPIRED" | "NONE" | "REVOKED";
export type QualityState = "PASSING" | "INCIDENT_OPEN" | "STALE" | "UNKNOWN";

/** ---------------------------------------------------------------------------
 *  `CatalogRowRead` — the read-model row this screen actually needs, and the
 *  endpoint this work proposes: GET /v1/organizations/{org}/catalog/rows
 *
 *  Everything below EXISTS in the platform today, but on five different
 *  endpoints keyed by table id:
 *    description        intelligence/business-meaning
 *    owner              governance ownership (GL-2)
 *    certification      GET /tables/{id}/certification
 *    quality            data_quality
 *    glossary_terms     GET /metadata/tables/{id}/glossary-links
 *    row_count_estimate GET /tables/{id}/profile
 *
 *  Rendering 100 rows the way this screen does costs 1 + (100 x 5) = 501
 *  requests against the current API. Composing it server-side makes it 1.
 *  That is the read-model layer, and it is the only backend work this
 *  front-end rebuild strictly requires.
 * ------------------------------------------------------------------------- */
export interface CatalogRowRead {
  id: string;
  name: string;
  schema_name: string;
  datasource_name: string;
  object_type: string;
  status: string;
  description: string | null;
  /** True when the description was model-proposed and not yet approved.
   *  ADR-0001: models propose, humans and deterministic services decide — so
   *  the UI must never render a proposal as though it were established fact. */
  description_is_proposed: boolean;
  owner: string | null;
  certification: CertificationStatus;
  certification_expires_at: string | null;
  quality: QualityState;
  glossary_terms: string[];
  row_count_estimate: number | null;
  updated_at: string;
}

/** Evidence pane payload — Module 21 §7 requires every surface to show why a
 *  thing exists, and §7 requires that view to be permalinkable. */
export interface EvidenceItem {
  label: string;
  value: string;
  /** Where the claim came from, so a reviewer can go argue with the source. */
  source: string;
  kind?: "ok" | "warn" | "bad" | "info";
}

export interface CatalogAssetEvidence {
  table_id: string;
  items: EvidenceItem[];
}

export type Persona =
  | "Analyst"
  | "Steward"
  | "Reviewer"
  | "Operator"
  | "Auditor";

/** `MeRead` — persona_api.py. Module 21 §5: in production this is the ONLY source of
 *  persona — the client never picks it. `identity_provider` is the exact prod/dev gate
 *  `aida.security.get_security_context` already branches on (`Settings.identity_provider`),
 *  echoed here so the shell checks the one flag the server itself checks rather than
 *  inferring its own. `persona` is null under the development identity provider, and
 *  also under OIDC when the principal's groups map to no configured persona. */
export interface MeRead {
  principal_id: string;
  principal_type: string;
  organization_id: string | null;
  roles: string[];
  persona: Persona | null;
  identity_provider: "OIDC" | "DEVELOPMENT";
}
