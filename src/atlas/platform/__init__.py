"""Shared infrastructure with no domain knowledge
(`Docs/10-architecture/04-module-decomposition.md` Sec.8).

Status: partially extracted (tracker ST-04, Phase 1 of
`Docs/40-engineering/06-refactor-plan.md`). Moved so far: `db`, `config`,
`logging`, `context` -- each still re-exported from its old `aida.*` location
for backward compatibility, so every existing caller keeps working
unchanged. Not yet moved: `main.py` (app assembly) -- it currently imports nearly
every domain router and does not yet satisfy `platform-purity`, so moving it
is deferred to Phase 5 (the `api.py` router split) rather than done as a
same-module-shape file move. `events.py` does NOT belong here at all -- it
directly constructs `AuditEvent`/`OutboxEvent` (module 20's owned tables),
so it moves to `atlas.modules.observability_audit` in Phase 3/4 instead (see
`Docs/40-engineering/06-refactor-plan.md` Phase 1's 2026-08-29 correction).
Also not yet built: pagination, idempotency, error-taxonomy, and telemetry
scaffolding, plus a generic outbox-write primitive -- none of these exist as
separate files in `aida` yet.

The test for correct placement (per the decomposition doc): if a file here
mentions a business concept -- table, metric, tool, lineage, steward -- it is
in the wrong place. `db`, `config`, `logging`, and `context` all pass that
test; the `platform-purity` import-linter contract (tracker ST-02) makes it
mechanical.
"""
