# ADR-0011 — Modular Monolith Over Microservices

**Status:** Accepted | **Date:** 2026-08-28 | **Owner:** Architecture

## Context

The current implementation is a **flat package monolith**: `src/aida/` with `models.py` (1,274 lines) and `schemas.py` (1,298 lines) holding the ORM models and DTOs for every domain, and `api.py` (1,530 lines) holding much of the HTTP surface. This has no enforceable boundaries, high change amplification, and no seam to extract along.

The obvious correction is microservices. That correction would be premature: the right boundary between `semantic-layer`, `glossary`, and `retrieval` is not yet known; maker-checker approval spans governance, semantics, and tools in one transaction; the interactive path has a 300 ms total overhead budget that network hops would consume; and the team has not yet certified one connector fleet, let alone twenty deployment pipelines.

## Decision

**A modular monolith with mechanically enforced boundaries and a pre-planned extraction path.**

1. **21 modules** with published interfaces (`<module>/api.py`, `<module>/contracts.py`). Cross-module access goes through those two files only.
2. **One PostgreSQL schema per module.** No cross-schema foreign keys except into `identity` (ADR-0015).
3. **Import-linter contracts** enforce layering, module privacy, gateway exclusivity, and acyclicity. Violations fail CI.
4. **Four deployment units from one image**: `atlas-api`, `atlas-worker`, `atlas-projector`, `atlas-scheduler` — split by scaling and failure characteristics, not by domain.
5. **Extraction triggers are defined in advance** (`10-architecture/05-service-extraction-plan.md` §2). A module is extracted only when a trigger fires and a readiness gate passes.

## Consequences

### Positive

- Most of the isolation benefit at a small fraction of the distributed cost.
- Boundaries are validated by use before they are frozen into network interfaces.
- Cross-module transactions (maker-checker) stay simple; no sagas for governance.
- Extraction later is a deployment change, not a rewrite — because of the schema rule.
- One stack trace debugs a cross-module bug.

### Negative — costs accepted

- Boundary enforcement depends on tooling discipline. A disabled import-linter rule silently returns the system to the current state.
- Independent scaling is limited to the four unit split until a real extraction.
- One process means a memory leak or crash in one module affects the others (mitigated by the worker/projector split for the heaviest paths).
- The refactor from the current flat package is significant work with no user-visible feature.
- Engineers coming from microservice backgrounds will read the single deployable as a step backwards.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Full microservices now | Boundaries are guesses; distributed transactions for approval; latency budget; operational surface for a team that has not certified one connector fleet |
| Keep the flat monolith | The three failure modes are already occurring |
| Package-only split, no schema split | Loses the extraction insurance; a later split becomes a data migration |
| Two services (API + workers) only | Already the plan — plus enforced internal modules, which is strictly better |

## Revisit trigger

An extraction trigger fires for a specific module: independent scaling need, independent release cadence, blast-radius isolation, different runtime requirement, team ownership boundary, or regulatory placement. Extraction is per module, not a wholesale migration.

## Related

- `10-architecture/04-module-decomposition.md`
- `10-architecture/05-service-extraction-plan.md`
- `40-engineering/06-refactor-plan.md`
