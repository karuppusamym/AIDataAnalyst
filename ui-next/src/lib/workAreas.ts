/* ---------------------------------------------------------------------------
   Personas and work areas are two different things (review 2026-09-05, F22).

   THE DEFECT this module exists to remove: the shell's navigation was grouped
   into eight headings it called personas, two of which -- Consumer and
   Developer -- do not exist in the `Persona` union that the persona switcher,
   the onboarding wizard and `GET /v1/me` all share. The landing map keyed a
   "Consumer" workbench off a persona nobody could ever be, so that entry was
   unreachable code and the Consumer group could only be found by scrolling to
   it. Two concepts wearing one name.

   THE DISTINCTION, stated once:

     PERSONA   -- who you are. Derived from identity (OIDC groups) in
                  production, five values, one per person, and NOT selectable
                  in a way that grants anything (module 21 §5). It lives in
                  `ui-types.ts` because the server reports it.

     WORK AREA -- what you are doing right now. A grouping of screens. A
                  person opens as many as they are entitled to; an Analyst
                  answering a question is in the Analyst area, and the same
                  Analyst requesting access to a data product is in the
                  Consumer area. Nothing is gated on it: it is navigation.

   Entitlement is not expressed here and must not be. Every screen a work area
   lists is authorized by the backend on every request; this table only decides
   what the sidebar offers and where a fresh session lands.
--------------------------------------------------------------------------- */

import type { Journey } from "./routes";
import type { Persona } from "./ui-types";

export const WORK_AREAS = [
  "Inbox",
  "Analyst",
  "Consumer",
  "Developer",
  "Steward",
  "Reviewer",
  "Operator",
  "Auditor",
] as const;

export type WorkArea = (typeof WORK_AREAS)[number];

/* R11-S10: a work area is now also a ROUTE segment.
 *
 * `lib/routes.ts` owns the slug (`analyst`), because the route table is what
 * has to parse a URL; this file owns the label (`Analyst`), because the
 * sidebar is what has to render one. The two tables below tie them together
 * explicitly rather than by `toLowerCase()`, so adding a work area whose label
 * is two words -- which the slug could not round-trip -- is a compile error
 * here instead of a route that silently stops resolving.
 *
 * `Record<WorkArea, Journey>` and `Record<Journey, WorkArea>` are both
 * exhaustive by type, so neither direction can quietly go missing an entry. */
export const JOURNEY_OF_WORK_AREA: Record<WorkArea, Journey> = {
  Inbox: "inbox",
  Analyst: "analyst",
  Consumer: "consumer",
  Developer: "developer",
  Steward: "steward",
  Reviewer: "reviewer",
  Operator: "operator",
  Auditor: "auditor",
};

export const WORK_AREA_OF_JOURNEY: Record<Journey, WorkArea> = {
  inbox: "Inbox",
  analyst: "Analyst",
  consumer: "Consumer",
  developer: "Developer",
  steward: "Steward",
  reviewer: "Reviewer",
  operator: "Operator",
  auditor: "Auditor",
};

/**
 * The work areas a persona is expected to open, most relevant first.
 *
 * The first entry is where a fresh session lands when the URL names no
 * screen. The rest are ordinary navigation: every area stays visible to
 * everyone, because hiding a group the backend would happily serve teaches
 * people the product is smaller than it is.
 *
 * Consumer and Developer appear here rather than as personas, which is what
 * makes the Consumer landing entry reachable at last: an Analyst lands in
 * Analyst and has Consumer one click away, and a Steward packaging context
 * has Developer in the same list.
 */
export const PERSONA_WORK_AREAS: Record<Persona, readonly WorkArea[]> = {
  Analyst: ["Analyst", "Consumer", "Inbox"],
  Steward: ["Steward", "Developer", "Reviewer", "Inbox"],
  Reviewer: ["Reviewer", "Steward", "Inbox"],
  Operator: ["Operator", "Developer", "Inbox"],
  Auditor: ["Auditor", "Reviewer", "Inbox"],
};

/** Where a persona's first session lands. Overview when the persona is unknown. */
export function landingWorkArea(persona: Persona | null): WorkArea | null {
  if (!persona) return null;
  return PERSONA_WORK_AREAS[persona]?.[0] ?? null;
}

/** True when this work area is one the persona is expected to use often. */
export function isPrimaryWorkArea(persona: Persona | null, area: WorkArea): boolean {
  if (!persona) return false;
  return (PERSONA_WORK_AREAS[persona] ?? []).includes(area);
}
