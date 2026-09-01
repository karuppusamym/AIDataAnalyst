# ADR-0022 — Open Semantic Interchange: Interoperability Target, Not a Foundation

**Status:** Accepted | **Date:** 2026-09-01 | **Owner:** Architecture

## Context

Open Semantic Interchange (OSI) is a semantic-model interchange effort originated by
Snowflake, publicly positioned alongside partners including dbt Labs and Salesforce, to
give BI and semantic-layer tools a common way to describe tables, dimensions, and
metrics instead of each vendor re-defining the same measures in its own proprietary
format. `Docs/00-product/03-market-landscape.md` §3.2 already names the trade-off in one
line: OSI is a threat, because a widely adopted interchange format commoditizes the
value of any single vendor's proprietary semantic model, and an opportunity, because a
platform that speaks it can consume semantics authored anywhere and export its own
semantics to anywhere. `Docs/00-product/04-competitive-feature-matrix.md` §4 already
scores this as `PARITY / P2` with Snowflake marked "originator" — this ADR is not
introducing the decision, it is closing a decision the tracker (`SM-6`) and the decision
log (`Docs/90-reference/02-decision-log.md` §SM-6) already flagged as owed.

**The codebase is ahead of the tracker row.** `SM-6` reads `TODO` and
`Docs/20-modules/07-semantic-layer.md`'s parity table lists Open Semantic Interchange as
"Not implemented," but `src/aida/context_compiler.py` already has an `OSI` branch: the
`ContextCompilerTarget` literal in `src/aida/platform_schemas.py` includes `"OSI"`
alongside `"MCP"`, `"REST"`, `"YAML"`, `"ODCS"`, `"SNOWFLAKE_SEMANTIC_VIEW"`, and
`"DATABRICKS_METRIC_VIEW"`, `_artifact_payload` emits an
`{"specification": "OpenSemanticInterchange", "specificationVersion": "1.0",
"semanticContext": ...}` envelope for it, and `validate_compiled_artifact` requires
`("specification", "specificationVersion", "semanticContext")` for that target. So the
implicit decision — Atlas treats OSI the same way it treats Snowflake Semantic Views,
Databricks Metric Views, and ODCS: as one more deterministic, value-free projection out
of the context compiler, never as a second source of truth — was already made in code
during CP-5 (`Docs/60-delivery/02-epic-backlog.md` EA.10c / EE.9). This ADR records that
decision explicitly and states what "evaluation" actually still means now that a target
exists.

**What exists is a placeholder, not spec conformance.** The `OSI` branch wraps the same
`common` metadata dict every other target wraps — product key, version, fingerprint,
owner, references, quality requirements, policy summary — under a bare
`specification`/`specificationVersion`/`semanticContext` envelope. It does not map
Atlas's semantic model versions, dimensions, or metrics into whatever entity/dimension/
measure shape OSI's own schema actually defines. Compare this to
`SNOWFLAKE_SEMANTIC_VIEW` and `DATABRICKS_METRIC_VIEW`, which at least project `tables`
into vendor-shaped `logicalName`/`physicalName` and `sourceTables` structures — the
`OSI` branch does not do the equivalent work of shaping `semanticContext` into OSI's own
entity model. No test in this repository exercises the `OSI` target by name;
`tests/test_agentic_platform.py` proves determinism and drift-detection for `ODCS` and
`YAML` only. So today's `OSI` target satisfies Atlas's own bounded structural validator
and nothing external — a consumer that assumed OSI-schema fidelity from this target
would be misled, which is exactly the honesty problem `Docs/20-modules/07-semantic-layer.md`
and the platform's `INV-9` posture (no capability overstated) exist to prevent.

Three things this ADR must not do, given the hard constraints on this task: it does not
change `src/aida/context_compiler.py`, `platform_schemas.py`, or any test — it evaluates
what is there and records the decision about where the target goes next.

## Decision

**Open Semantic Interchange stays what it already is: one thin, deterministic export
projection out of the governed context compiler — never the platform's internal
semantic model, and never a second source of truth for metrics or dimensions.**

Concretely:

1. **Atlas's own semantic layer (module 07/08) remains canonical.** `SemanticMetricVersion`,
   `TermSemanticBinding`, and the maker-checker publication path in `semantic_api.py` /
   `domain_service.py` stay the one governed representation of a metric or dimension.
   OSI — like ODCS, Snowflake Semantic Views, Databricks Metric Views, and custom YAML —
   is something the compiler *projects to*, compiled deterministically and version-pinned
   from that canonical model (`compile_context_product`), the same way every other target
   already works. Nothing about this decision asks Atlas to accept OSI-defined entities as
   an input to governance, only as one more output shape.
2. **The `OSI` target is kept, not removed.** It costs nothing to keep — one branch in
   `_artifact_payload`, one entry in the target-shape table in
   `validate_compiled_artifact` — and multi-vendor backing (Snowflake plus named
   partners, per the market-landscape research) makes the option worth holding even
   though no customer or partner has asked for it yet.
3. **Full spec conformance is deliberately not funded now.** Mapping Atlas's semantic
   model versions, glossary-bound dimensions, and metric definitions into OSI's actual
   published entity/dimension/measure schema — rather than the current bare metadata
   envelope — is real work, and OSI is early: a standard led by one vendor with named
   partners, not yet a settled multi-year interoperability layer with a stable schema
   Atlas can commit to without expecting churn. Spending that effort before either the
   schema has stabilized or a concrete customer/partner need exists would be paying for
   conformance nobody can consume yet. `SM-6`'s exit criterion — "decision recorded as an
   ADR" — is satisfied by this document; a follow-on tracker item for schema-conformant
   OSI mapping is deliberately not opened by this ADR (see Consequences).
4. **The placeholder is documented as a placeholder, not silently upgraded in
   perception.** `Docs/20-modules/07-semantic-layer.md`'s parity table and
   `Docs/60-delivery/03-tracker.md`'s `SM-6` row are updated by this same change (tracker
   row closure, §4 below) to say what is actually true: an `OSI` export target exists in
   `context_compiler.py` as an unconformed placeholder, evaluated and intentionally left
   thin, rather than either "not implemented" (wrong — a branch exists) or "implemented"
   (overstated — it does not conform to OSI's schema).
5. **Import is out of scope.** Consuming externally authored OSI documents into Atlas's
   own semantic model is a materially different feature — it would mean treating
   OSI-shaped input as evidence for a governed metric or dimension proposal, running
   through the same maker-checker review path `SM-4`'s metric suggestions use — and is
   not evaluated or decided by this ADR. If it is ever wanted, it needs its own ADR: it
   changes what counts as evidence into governance, which OSI export does not.

## Consequences

### Positive

- No new dependency, no new schema to track for churn, no vendor lock-in: Atlas's
  canonical semantic model is untouched, and OSI is one interchangeable projection among
  several the compiler already produces the same way.
- Optionality is kept at near-zero cost — if OSI adoption accelerates industry-wide (the
  scenario the market-landscape research flags as the real risk), Atlas already has a
  named target to harden rather than a green-field integration to design.
- Symmetric treatment with ODCS, Snowflake Semantic Views, and Databricks Metric Views
  keeps `context_compiler.py` simple: one function, one branch per target, one entry in
  the validator's required-field table — no special-cased target gets its own code path
  or governance model.
- Closes a decision-log item (`Docs/90-reference/02-decision-log.md` §SM-6) that was
  otherwise going to keep resurfacing as "should Atlas adopt OSI" without a recorded
  answer.

### Negative — costs accepted

- The `OSI` target is not spec-conformant today and this ADR does not fund making it so.
  Anyone pointing an external OSI-aware consumer at Atlas's `OSI` export before that
  mapping work happens gets Atlas's own metadata envelope, not an OSI entity/dimension/
  measure document — the gap must stay visible in module 07's docs rather than being
  quietly implied as done by CP-5's "implemented foundation" language.
- Deferring conformance work means Atlas cannot yet claim OSI interoperability as a
  competitive answer to Snowflake's own OSI-native tooling; the competitive-matrix `PARITY`
  score for "Open semantic interchange (OSI)" stays aspirational rather than proven until
  the mapping is real.
- The standard is still forming. If OSI's schema changes materially before Atlas ever
  invests in conformance, some of today's understanding of its shape (entity/dimension/
  measure documents, YAML-first) may need revisiting alongside the eventual mapping work
  — this ADR is deliberately not pinning a schema version to build against.

### Neutral

- No code, schema, model, or test changes accompany this ADR — it is a decision record
  over an export target that already exists, not a build task. `SM-6`'s exit criterion
  was "decision recorded as an ADR," not "OSI conformance shipped."

## Alternatives considered

| Option | Why rejected |
|---|---|
| Adopt OSI as Atlas's internal canonical semantic model, replacing `SemanticMetricVersion`/dimension authoring | Subordinates a governed, maker-checker, tenancy- and classification-aware internal model to an externally controlled schema originated by a competitor; loses the versioned publication/supersession behaviour `SM-1`/`SM-4`/`GL-2`..`GL-4` depend on, for a standard that does not model tenancy, classification, or approval state at all |
| Remove the `OSI` target entirely (treat commoditization risk as a reason to abstain) | Costs nothing to keep an export projection that already exists and compiles; removing it forecloses an option the market-landscape research explicitly names as valuable for free, in exchange for no measurable benefit |
| Invest now in full OSI-schema-conformant mapping (entities/dimensions/measures matched to the real published spec) | No customer or partner has requested OSI import/export; the spec is early and vendor-led, so building exact conformance now risks paying twice if the schema moves before anyone consumes it — P2 priority already reflects this being real but not urgent |
| Build OSI *import* now (consume external OSI documents as governance evidence) | A materially different feature from export — it feeds external, ungoverned definitions into a maker-checker review path — and deserves its own evaluation and its own ADR rather than being decided as a side effect of an export-target review |

## Revisit trigger

Revisit this decision — specifically, fund conformant OSI schema mapping in
`context_compiler.py`, or evaluate OSI import — when any one of:

1. A customer or partner integration concretely requires OSI-conformant export or
   import (not hypothetical interoperability, an actual named consumer);
2. OSI's specification reaches a stable, publicly versioned 1.0 schema that a mapping
   can be built against without expecting near-term breaking churn; or
3. A competitive assessment shows OSI adoption has become a genuine buying criterion in
   deals Atlas is in, rather than a roadmap talking point.

Until one of those is true, the existing thin `OSI` target is the right amount of
investment: present, honestly described as unconformed, and cheap to extend later.

## Related

- `src/aida/context_compiler.py` — the `OSI` branch this ADR evaluates
- `src/aida/platform_schemas.py` — `ContextCompilerTarget` literal
- `Docs/20-modules/19-context-products-and-mcp.md` §CP-5 / §CP-S6 — context compiler module spec
- `Docs/20-modules/07-semantic-layer.md` — semantic layer parity table and `SM-6` open-work row
- `Docs/00-product/03-market-landscape.md` §3.2 — the threat/opportunity framing this ADR resolves
- `Docs/00-product/04-competitive-feature-matrix.md` §4 — OSI parity scoring
- `Docs/90-reference/02-decision-log.md` §SM-6 — the decision-log entry this ADR closes
