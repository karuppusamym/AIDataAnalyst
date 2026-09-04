/* ---------------------------------------------------------------------------
   Hand-written types. NOT generated -- see ./types.ts (tracker UX-14) for the
   file that is, and scripts/generate_ui_types.py for the generator and gate.

   Everything in this file falls into one of two buckets:

   1. Front-end-only concepts with no server representation at all (`Persona`).
      There is nothing in `schemas.py` to generate these from, so they stay
      hand-written by definition.

   2. Types this app needs that ARE meant to come from the live API, but
      currently can't: `CatalogRowRead`, `MetadataTableRead` and the two
      literal unions below them. `GET /v1/organizations/{org}/catalog/rows`
      and `GET /v1/datasources/{id}/tables` (`src/aida/api.py`) both declare
      `response_model=CursorPage` un-parameterized, so FastAPI's schema
      walker never reaches `CatalogRowRead` or `MetadataTableRead` -- neither
      name appears in `app.openapi()`'s `components.schemas` today, even
      though both endpoints return exactly these shapes at runtime. Fixing
      that means changing a route's `response_model` in `src/aida/api.py`,
      which is backend business logic outside UX-14's scope (ui-next + CI
      config only). Tracked as a follow-up (see 03-tracker.md UX-14's exit
      note), not silently hidden: this comment, and the matching one in
      scripts/generate_ui_types.py's module docstring, are the paper trail.
      `certification`/`quality` are typed here as their known literal unions
      per `CatalogRowRead`'s own field comments in schemas.py (`certification:
      str  # CERTIFIED | EXPIRED | NONE | REVOKED`); the live schema (once
      reachable) will only widen them to `string`, so this narrowing is a
      strictly front-end convenience, not a claim the server enforces it.
--------------------------------------------------------------------------- */

/** `CursorPage` (./types.ts) narrowed to the two field types this app pins
 *  by hand until CatalogRowRead/MetadataTableRead are reachable from the
 *  OpenAPI document (see the file banner above). */
export type { CursorPage } from "./types";
import type { DataProductVersionRead } from "./types";

export type CertificationStatus = "CERTIFIED" | "EXPIRED" | "NONE" | "REVOKED";
export type QualityState = "PASSING" | "INCIDENT_OPEN" | "STALE" | "UNKNOWN";

/** `MetadataTableRead` -- schemas.py:712. Eight fields. Not in the live
 *  OpenAPI document yet; see this file's banner comment for why. */
export interface MetadataTableRead {
  id: string;
  datasource_id: string;
  schema_id: string;
  name: string;
  object_type: string;
  status: string;
  fingerprint: string;
}

/** `CatalogRowRead` -- schemas.py:3154 ("Mirrors `CatalogRowRead` in
 *  `ui-next/src/lib/types.ts` field-for-field; that file is the client
 *  already typed against this endpoint, so this schema follows it rather
 *  than the reverse" -- that comment now points at this file instead). Not
 *  in the live OpenAPI document yet; see this file's banner comment for why. */
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
  /** P3-09: small counts snapshot from the current certification's
   *  structured `evidence` blob -- what the certifier was implicitly
   *  attesting to (description version, active owner count,
   *  open-incidents-at-certify, glossary-term count). Null when the current
   *  cert is legacy (server has `evidence IS NULL`), when the cert is
   *  EXPIRED/REVOKED, or when there is no current cert. The catalog grid's
   *  certification cell reads this to render the "Based on: ..." hover
   *  tooltip; keep it nullable, no client should assume its presence. */
  certification_evidence_summary: CertificationEvidenceSummary | null;
  quality: QualityState;
  glossary_terms: string[];
  row_count_estimate: number | null;
  updated_at: string;
}

/** P3-09: matches `aida.schemas.CertificationEvidenceSummary`. Kept on
 *  `CatalogRowRead` (not on a separate detail endpoint) so the catalog
 *  grid can render the tooltip from one row payload without a follow-up
 *  fetch. Values are null/zero when the certifier's evidence is absent
 *  or empty; `backfilled=true` marks a row populated retrospectively by
 *  the `backfill_certification_evidence.py` CLI, not at certify time. */
export interface CertificationEvidenceSummary {
  description_version_id: string | null;
  active_owner_count: number;
  open_incident_count_at_certify: number;
  glossary_term_count: number;
  backfilled: boolean;
}

/** Front-end persona set (module 21 §5). No server enum backs this: in
 *  production a persona comes back as `MeRead.persona: string | null`
 *  (./types.ts) and must be checked against this list with `asPersona`
 *  below before it can be trusted as one of these five, exactly the same
 *  way any other untrusted external string would be narrowed. */
export type Persona = "Analyst" | "Steward" | "Reviewer" | "Operator" | "Auditor";

const PERSONAS: readonly Persona[] = ["Analyst", "Steward", "Reviewer", "Operator", "Auditor"];

/** Narrows a server-reported persona string to `Persona`, or `null` if it
 *  isn't one of the five the shell knows about (an unmapped OIDC principal,
 *  or a future persona the client hasn't shipped support for yet) -- fails
 *  closed to "no persona", never to a guess. */
export function asPersona(value: string | null | undefined): Persona | null {
  return value != null && (PERSONAS as readonly string[]).includes(value)
    ? (value as Persona)
    : null;
}

/** `MeRead.identity_provider` (./types.ts) is `string` on the wire -- the
 *  server's own prod/dev gate (`Settings.identity_provider`), but not a
 *  literal type there since schemas.py leaves it as `str`. `PersonaNav`
 *  needs the narrower two-value type module 21 §5 actually specifies; this
 *  narrows the same way `asPersona` does, and just as deliberately fails
 *  closed to `null` (== "render nothing yet") on anything else, rather than
 *  guessing which of the two real modes an unrecognised value means. */
export type IdentityProvider = "OIDC" | "DEVELOPMENT";

export function asIdentityProvider(value: string | null | undefined): IdentityProvider | null {
  return value === "OIDC" || value === "DEVELOPMENT" ? value : null;
}

/** UX-15: `Page` (./types.ts) is generated un-parameterized (`items: unknown[]`)
 *  for every route that declares `response_model=Page` without a generic
 *  argument -- the same reachability gap this file's banner documents for
 *  `CatalogRowRead`/`MetadataTableRead`. `search_marketplace`,
 *  `list_refusals` and `list_organization_datasources` (`product_marketplace_api.py`,
 *  `ai_decision_lineage_api.py`, `operational_api.py`) all return one of
 *  these; this narrows `items` to the item type each endpoint actually
 *  returns, exactly as front-end-only convenience, not a claim the server's
 *  own OpenAPI schema types it this way. */
export interface PageOf<T> {
  items: T[];
  limit: number;
  offset: number;
  total: number;
}

/** `AuditEventRead` -- `src/atlas/modules/observability_audit/schemas.py:47`,
 *  re-exported through `aida.schemas`. `GET
 *  /v1/organizations/{organization_id}/audit-events` (`list_audit_events`,
 *  `src/aida/operational_api.py:336`) declares `response_model=Page`
 *  un-parameterized -- the same reachability gap this file's banner documents
 *  for `CatalogRowRead`/`MetadataTableRead`/`PageOf`'s own comment, so
 *  `AuditEventRead` is hand-written here rather than pulled from
 *  ./types.ts. Note `id` is `int` on the wire (schemas.py), not a UUID like
 *  most of this app's other `*Read` ids. */
export interface AuditEventRead {
  id: number;
  organization_id: string | null;
  principal_id: string;
  principal_type: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  outcome: string;
  correlation_id: string;
  source_ip: string | null;
  details: Record<string, unknown>;
  occurred_at: string;
}

/** `MarketplaceProductRead` -- `platform_schemas.py:170`. Extends
 *  `DataProductVersionRead` (./types.ts, reachable) with the three CX-9
 *  ranking/access fields `search_marketplace` (`product_marketplace_api.py`)
 *  adds per requester. Not itself reachable from the live OpenAPI document:
 *  its route declares `response_model=Page` un-parameterized (see `PageOf`
 *  above), so FastAPI's schema walker never names this subclass -- only its
 *  base `DataProductVersionRead` is reachable, via other routes that use it
 *  directly. Hand-written for the same reason `CatalogRowRead` is. */
export interface MarketplaceProductRead extends DataProductVersionRead {
  access_status: "ROLE_GRANTED" | "REQUEST_APPROVED" | "REQUEST_PENDING" | "NOT_REQUESTED";
  domain_affinity: boolean;
  role_affinity: boolean;
}

/** `ViolationRead` -- `runtime_contracts_api.py:41` (Phase E runtime data
 *  contract enforcement). That file defines its response models inline
 *  rather than in `schemas.py`, so -- like `CatalogRowRead`/`MetadataTableRead`
 *  above -- it has no counterpart in `types.ts`'s generated OpenAPI mirror;
 *  hand-written here for the same reason. One row per detected contract
 *  violation, returned by `GET /v1/data-contracts/{contract_id}/violations`. */
export interface ViolationRead {
  id: string;
  organization_id: string;
  contract_id: string;
  violation_type: string;
  severity: string;
  evidence: Record<string, unknown>;
  detected_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string;
  updated_at: string;
}
