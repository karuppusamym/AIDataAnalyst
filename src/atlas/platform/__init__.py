"""Shared infrastructure with no domain knowledge
(`Docs/10-architecture/04-module-decomposition.md` Sec.8).

Status: scaffold only (tracker ST-01) -- empty container. Phase 1 of
`Docs/40-engineering/06-refactor-plan.md` moves `db.py`, `config.py`,
`logging.py`, `context.py`, `events.py` (outbox mechanics), and `main.py`
(app assembly) here, plus pagination, idempotency, error-taxonomy, and
telemetry scaffolding.

The test for correct placement (per the decomposition doc): if a file here
mentions a business concept -- table, metric, tool, lineage, steward -- it is
in the wrong place. Nothing has moved here yet, so nothing to check.
"""
