import { get, postJson } from "./transport";

/** R11-FP09: what one mapping's catalog target is now. */
export interface OntologyMappingValidity {
  concept: string; subject_type: string; subject_id: string;
  status: "VALID" | "TARGET_MISSING" | "TARGET_DEPRECATED" | "KIND_MISMATCH";
  datasource_id: string | null; superseded_by_id: string | null;
}
export interface OntologyVersionRead {
  id: string; ontology_id: string; ontology_key: string; version: number; base_version: number;
  published_version: number; status: string; definition: Record<string, unknown>;
  created_by: string; approved_by: string | null; governance_review_id: string | null;
  mapping_validity?: OntologyMappingValidity[];
}
export function listOntologyVersions(org: string, offset = 0, signal?: AbortSignal): Promise<OntologyVersionRead[]> {
  return get(`/v1/organizations/${encodeURIComponent(org)}/ontology-versions?limit=50&offset=${offset}`, signal);
}
export function createOntologyVersion(org: string, body: {ontology_key: string; base_version: number; definition: Record<string, unknown>}): Promise<OntologyVersionRead> {
  return postJson(`/v1/organizations/${encodeURIComponent(org)}/ontology-versions`, body);
}
export function submitOntologyVersion(id: string): Promise<OntologyVersionRead> {
  return postJson(`/v1/ontology-versions/${encodeURIComponent(id)}/submit`, undefined);
}
