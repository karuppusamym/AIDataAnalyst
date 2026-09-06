import { describe, expect, it } from "vitest";

import { PERSONA_WORK_AREAS, WORK_AREAS, isPrimaryWorkArea, landingWorkArea } from "./workAreas";

/* ---------------------------------------------------------------------------
   F22. The defect these lock down: "Consumer" was a navigation group AND a
   landing-map key, but never a `Persona`, so the entry was unreachable. The
   two vocabularies are now separate and the areas are reachable independently
   of who you are.
--------------------------------------------------------------------------- */

describe("personas and work areas are separate vocabularies", () => {
  it("keeps Consumer and Developer as work areas, not personas", () => {
    expect(WORK_AREAS).toContain("Consumer");
    expect(WORK_AREAS).toContain("Developer");
    expect(Object.keys(PERSONA_WORK_AREAS)).toEqual([
      "Analyst",
      "Steward",
      "Reviewer",
      "Operator",
      "Auditor",
    ]);
  });

  it("makes the Consumer area reachable from a persona that actually exists", () => {
    expect(isPrimaryWorkArea("Analyst", "Consumer")).toBe(true);
  });

  it("gives every persona a landing area that is a real work area", () => {
    for (const persona of Object.keys(PERSONA_WORK_AREAS) as (keyof typeof PERSONA_WORK_AREAS)[]) {
      const area = landingWorkArea(persona);
      expect(area).not.toBeNull();
      expect(WORK_AREAS).toContain(area!);
    }
  });

  it("lands nowhere in particular when no persona is known", () => {
    expect(landingWorkArea(null)).toBeNull();
    expect(isPrimaryWorkArea(null, "Analyst")).toBe(false);
  });

  it("lets one persona open several work areas", () => {
    expect(PERSONA_WORK_AREAS.Steward.length).toBeGreaterThan(1);
    expect(PERSONA_WORK_AREAS.Analyst.length).toBeGreaterThan(1);
  });
});
