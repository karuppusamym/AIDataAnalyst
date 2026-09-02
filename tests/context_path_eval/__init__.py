"""AT-8: context-path eval suite (pull forward from N17/ST-8).

This package is the "stored eval" the tracker row asks for. It is stored as
plain, version-controlled Python -- `cases.py`'s `CASES` tuple -- rather than
as rows in a new database table: an eval case is a request shape plus an
expectation about the *mechanism* that answered it, and a checked-out git
worktree already replays it deterministically (`git show <ref>:tests/...` for
any past revision, `pytest` for the current one) with no server, no
migration, and no extra persistence layer. See `Docs/60-delivery/03-tracker.md`
AT-8 and `Docs/60-delivery/06-accomplishment-log.md`'s AT-8 entry for the full
design rationale, including why this deliberately does not touch
`AgentEvaluationRun` (`agent_evals.py`'s existing eval mechanism asserts
deterministic *safety-control* scenarios with no database at all; this one
asserts the *context path* of a real `GovernedAgentOrchestrator.run()` against
real seeded metadata, which is a different axis entirely).

**What "context path" means here** (never a business-value/final-answer
assertion -- excluded by INV-6/ADR-0014, see
`Docs/10-architecture/adr/ADR-0014-value-free-control-plane.md` and INV-6 in
`Docs/10-architecture/01-principles-and-invariants.md`):

- which objects the retrieval stage resolved (`AgentRun.retrieval_evidence`'s
  object types/ids -- tables, business annotations, governed tools);
- which semantic/glossary version was pinned (`AgentRun.semantic_version`,
  the same "capture what was true at answer time" idiom AT-16's
  `answer_provenance.compose_lineage_provenance` uses for lineage, applied
  here to the semantic-model pin computed earlier in the same `run()` call);
- which governed tool or compiled plan strategy was selected
  (`AgentRun.plan_evidence`'s `strategy`/`selected_tool_version_id`/
  `tool_decisions`);
- what policy decision was made (`AgentRun.status`/`failure_reason`, and the
  prompt-risk decision folded into `plan_evidence` for a `BLOCKED` plan).

Modules:

- `scenario.py` -- builds a small, named, deterministic seeded environment
  (organization, datasource, one table with a business annotation, one
  governed tool requiring a parameter, and an optional published semantic
  model version) against an in-memory SQLite session, the same convention
  `tests/test_at6_context_receipts.py` and
  `tests/test_agent_orchestrator_retrieval_wiring.py` already use. Entities
  are addressed by stable slugs (`"order_lookup"`, not a UUID that changes
  every run) so eval cases never hard-code an id.
- `cases.py` -- the stored eval cases themselves: `ContextPathEvalCase`
  instances naming an input scenario and an expected context path, never an
  expected answer.
- `runner.py` -- `run_eval_case` (drives a real orchestrator run and
  compares the derived context path against a case's expectation), the
  replay mechanism itself. `ContextPath`/`derive_context_path` now live in
  `aida.context_path` (moved for N17 so production code can share them) and
  are re-exported here at the same names.
- `exemplars.py` (N17) -- converts a *promoted* exemplar
  (`aida.exemplar_store.ExemplarCase`, mined from a confirmed `AgentRun` or
  authored by a steward) into this package's own `ContextPathEvalCase`
  format and replays it through `runner.run_eval_case`, the same mechanism
  above -- the "benchmark suite" half of N17. See `aida.exemplar_store`'s
  module docstring for the full design (what "confirmed" means in this
  codebase's real data, and why only a steward-authored exemplar carries a
  live-replayable question).
"""

from __future__ import annotations
