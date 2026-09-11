# Graph exploration and query review

Update 2026-09-11: see the [five-feature implementation report](../60-delivery/18-five-feature-implementation.md)
for expanded language support, ontology governance, worksheet and review details,
including the unresolved database deployment checkpoint. The original assessment
below records the earlier scope, not the latest feature inventory.

Review date: 2026-09-10. This is an implementation-level supplement to the
[agent architecture review](15-agent-architecture-critical-review.md), not a
certification of enterprise readiness.

## Implemented in this pass

Unified lineage now offers asset-name/qualified-name search, node-kind filtering,
and an edge-confidence threshold alongside its existing lineage-layer controls.
Edges whose endpoints are filtered out are hidden. Filters apply to the bounded,
already-loaded graph; they are not a search of the entire catalog. The Nodes tab
and diagram-cap message reflect matching nodes.

The guided question field accepts `upstream of <asset>` or `downstream of <asset>`,
optionally prefixed by `show` and followed by `within N hops` or `up to N hops`
where N is 1 through 5. The default is 3 hops. Assets must match a loaded label,
qualified name or ID exactly (case-insensitive); ambiguous names are rejected.
Users preview the resolved asset/direction/depth before explicitly running it.
Execution uses the existing authorized impact API with a 200-node limit. Direction
filters the returned impact rows. Domain-view impact remains within the selected
asset's own source; this is not arbitrary cross-source traversal.

This is a deterministic guided-language parser, **not** general LLM-generated
Cypher, SQL, ontology reasoning or database mutation. Arbitrary statements and
out-of-range depths are rejected. Asset filters are presentation controls, not
authorization controls, and do not limit the independent impact request.

## Ontology: what exists and what remains

The repository already has a typed technical graph: the projector maps catalog,
schema, table, column and constraint hierarchy and references, plus unified
lineage nodes and edges. Business taxonomy and typed business-to-asset mappings
also exist in `business_graph.py`. Thus, it is inaccurate to say there is no
semantic structure simply because there is no new ontology page.

That structure does not establish a complete governed ontology lifecycle.
Before claiming one, validate and document versioned concept/relation definitions,
allowed endpoint types and cardinalities, aliases, provenance, ownership,
approval, deprecation, mappings to physical assets and migration compatibility.
Add conformance fixtures demonstrating invalid mappings are refused and existing
projections can be rebuilt across ontology versions. Formal ontology import and
reasoning remain unvalidated; they are not delivered by this UI change.

PostgreSQL remains authoritative. Neo4j is an optional rebuildable projection,
not a second authorization authority. `graph_store.py` explicitly gates Neo4j
reads behind operator enablement and documents outstanding conformance evidence.
Do not enable it merely because these graph controls work. Validate adapter
parity, authorization, bounds, outage behavior, projection lag and rebuilds on
real Neo4j before making readiness claims.

## Next safe extension

A model may propose a typed query plan containing authorized asset IDs, a
supported operation, direction and limits. Deterministic validation must resolve
scope, reject ambiguity, enforce permissions and resource limits, and require
confirmation before execution. Keep free-form Cypher out of the execution path
until there is a separately reviewed read-only execution boundary. Read-only
alone does not prevent disclosure or expensive queries.

Evaluate questions with ambiguous terminology, hostile metadata, nonexistent
assets, denied sources, stale projections, cycles and truncated results. Measure
semantic correctness and refusal quality separately from syntactic validity.

## Other review items: do not overclaim completion

The earlier laptop layout screenshots, column-description generation discovery,
source-versus-analyst workflow placement, workbook save-back and reviewer error
messages require their own end-to-end acceptance checks. Graph tests do not
revalidate those journeys. In particular, manual workbook import/export does not
establish automatic Excel save-back; that needs an explicit editing integration,
identity binding, concurrency/conflict handling and the existing approval gate.

The agent review's production-scale, independent semantic evaluation and
human-audit recovery evidence remain separate work.

**Provider-timeout accounting** uses an input-only estimate on generation
failure (`agent_budget.settle_unresolved_run_budget`) and releases the output
allowance. Admission reserves planned input plus output, but settlement is not
provider-reported usage. A timeout or unparseable response does not prove the
provider generated no billable output, and planned attempts do not prove every
attempt was sent. Treat this as an estimated accounting policy, not a verified
hard provider-spend ceiling. Usage reconciliation remains necessary for that
stronger claim. This follow-up corrects the earlier wording without changing
the implemented settlement policy.

**Multi-worker revocation** was reported measured the same day:
`tests/test_reviewer_agent_postgres_suspension.py` puts four agents on four
PostgreSQL connections and suspends them from a fifth, giving a maximum stop
delay of 0 items at READ COMMITTED and 6 — the whole remaining batch — at
REPEATABLE READ. Contention between workers on one queue remains untested.

## Regression coverage

`graphQuestion.test.ts` covers supported plans, ambiguity, invalid statements and
scope refusal. `UnifiedLineageScreen.test.tsx` covers preview without execution,
bounded execution with directional results, and combined asset/type filtering
with reset. These tests mock the API; they do not certify live backend permissions,
browser layout or Neo4j interoperability.

Executed this pass: 15 graph/parser UI tests and 26 Sources/Review Queue UI tests
passed; the production UI build passed. Targeted backend lint also passed.

See the [follow-up validation ledger](../60-delivery/17-follow-up-validation.md)
for the later workbook, column-panel and review-queue fixes and remaining gaps.
