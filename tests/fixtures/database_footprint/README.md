# SQL Server and PostgreSQL footprint samples

These fixtures exercise the **current** Atlas parsers and ontology validation and execute
real SQL against the existing local sample containers. They do not implement the proposed
context platform extensions or certify every database language construct.

## The business example

Two synthetic customers have three orders. Customer revenue is the sum of `amount -
discount`, grouped by customer and region. Expected revenue is **150 for customer 1** and
**70 for customer 2**. Customers without orders have no result because the view uses an
inner join. This is a sample definition, not a universal meaning of revenue.

| File in each engine directory | Purpose |
|---|---|
| `setup.sql` | Creates a dedicated schema, three tables, a foreign key and synthetic rows |
| `view.sql` | Joins customers/orders and calculates customer revenue |
| `read.sql` | SQL Server read procedure / PostgreSQL SQL function; a restricted extracted SELECT candidate |
| `function.sql` | SQL Server table-valued function / two PostgreSQL function overloads |
| `refresh.sql` | Multi-statement procedure that uses a temporary table and writes customer totals |
| `dynamic.sql` | Dynamic SQL boundary; defined but not invoked by the live verifier |
| `nested.sql` | Nested-call boundary; defined but not invoked by the live verifier |
| PostgreSQL `materialized.sql` | Materialized revenue snapshot, checked against expected row count |

SQL Server does not use the PostgreSQL materialized-view fixture. An indexed-view example
would require a separate native contract; its absence is not a claim of equivalent coverage.

## Ontology: what is dynamic and what is controlled?

[ontology.json](ontology.json) supplies three proposed concepts: Customer, Order and Customer
Revenue, with aliases and relationships. The current API accepts new concepts in a versioned
definition and validates graph consistency. Existing ontology tests exercise draft creation,
independent approval, stale-base rejection and invalid mappings.

Physical mappings would bind these concepts to **real catalog IDs after ingestion**:

| Concept | Intended physical mapping in each engine |
|---|---|
| Customer | `footprint_context_sample.customers` and its `customer_id` column |
| Order | `footprint_context_sample.orders` and its `order_id` column |
| Customer Revenue | `footprint_context_sample.customer_revenue.net_revenue` |

The fixture intentionally contains no made-up catalog IDs. Its mapping list is empty;
the generated report labels the ontology `SCHEMA_VALIDATED_ONLY`. Routine mappings and
ontology-version bindings in request context remain proposed extensions. No ontology,
context product or tool is published by these scripts.

## Run offline analysis and tests

From the repository root, using the project environment:

```powershell
.\.venv\Scripts\python.exe scripts/database_footprint_sample.py --output tests/fixtures/database_footprint/analysis-results.json
.\.venv\Scripts\python.exe -m pytest tests/test_database_footprint_samples.py tests/test_ontology.py tests/test_ontology_api.py -o addopts= -q
```

[analysis-results.json](analysis-results.json) records actual parser output, source-file
references, unresolved edges and blueprint/blocker results. It is a reproducible review
artifact over these synthetic fixtures, not an agent context product. The ontology text
is author-supplied; no LLM is called and no answer-quality claim is made.

## Run native SQL verification

The verifier targets only the existing local `aida-platform-sample-source-1` (PostgreSQL)
and `aida-platform-sample-mssql-source-1` (SQL Server) containers. Credentials stay inside
those containers. It uses the configured PostgreSQL database and SQL Server's
`bank_demo_mssql`, creates `footprint_context_sample` inside a transaction, checks native
results and routine inventory, rolls back, and verifies schema absence using a new session.
If that schema already exists, creation fails; the verifier does not drop or replace it.

```powershell
.\.venv\Scripts\python.exe scripts/verify_database_footprint_live.py
```

[live-results.json](live-results.json) records engine versions, verification times and
rollback results. No existing business rows are changed. No production source credentials
or platform database are used. This validates native SQL separately from Atlas's parser;
it does not exercise connector ingestion or metadata visibility under least privilege.

## Results and improvement backlog

Initial local run: **30 sample/ontology tests passed**. The broader run, including existing
connector, procedure and view-tool tests, passed **96 tests in 11.70 seconds**. Both native
verifiers passed on PostgreSQL **17.10** and SQL Server **16.0.4265.3**; both schemas were
confirmed absent after rollback. See generated artifacts for details.

| Case | Current observed result | Required interpretation |
|---|---|---|
| Revenue view, both dialects | Amount and discount contribute to revenue; customer region lineage captured | Useful calculation evidence; ontology meaning remains separately supplied |
| Read routine, both dialects | Resolved view output and extracted SELECT blueprint | Candidate only; not approval or native routine invocation |
| SQL Server temp-table procedure | Direct and transitive lineage into customer totals | It writes data, so read-tool generation is refused |
| PostgreSQL temp procedure | Since 2026-09-15 `ON COMMIT DROP AS` parses and the temp table is an intermediate, so `orders.discount` reaches `customer_totals.net_revenue` through it. Before that date the statement was explicitly unparsed | Same transitive evidence as SQL Server. The procedure writes data, so read-tool generation is refused |
| Dynamic SQL, both dialects | Explicit `DYNAMIC_SQL` evidence in both dialects; tool generation refused. Before 2026-09-15 PostgreSQL reported an unsupported statement shape instead | Dynamic text remains a named gap |
| Nested calls, both dialects | Since 2026-09-15 the call is read through the captured callee (`routine_call_descent`), and `nested_revenue` has no gap left on either engine (`tests/test_nested_callee_lineage_live.py`); tool generation is still refused | A callee that is not captured, ambiguous, cyclic or too deep stays a named gap |
| Ontology evolution | New concept accepted; invalid relation target and routine mapping rejected | Business vocabulary is extensible; target types remain controlled |

The read/temp-flow samples qualify selected columns with table aliases. Unqualified columns
can remain unresolved by the current parser without additional schema resolution; a fully
parsed statement is not necessarily fully resolved column lineage. The analysis report
therefore also records unresolved-edge counts.

## Stored form, and the defect it exposed (2026-09-15)

The analysis and tests now run over the text ingestion **stores** (`redact_for_storage`),
not the raw fixture, because the lineage agent and a person's parse read only the stored text.
`analysis-results.json` records each object's `stored_redaction_status`.

Running the samples that way exposed an INV-6 defect, tracked as R11-D16. A dollar-quoted
PostgreSQL body kept its literals while labelled `PARSED`. The PostgreSQL connector stores
`pg_get_functiondef` output, which tags bodies `$function$`/`$procedure$` rather than the `$$`
these fixtures use, so every live PL/pgSQL routine was affected. After the fix those bodies
are `LEXICAL`: value-free and still parseable.

The PL/pgSQL gaps this pack measured are fixed under R11-FP07: temp-table intermediates,
`EXECUTE` as dynamic SQL, `PERFORM`, `RETURN QUERY`, `SELECT INTO` as routine-local state,
`EXCEPTION` handlers and `FOR ... IN <query> LOOP`. Tests are in
`tests/test_procedure_lineage_plpgsql.py` and `tests/test_sql_redaction_program_bodies.py`.

Priority follow-up is tracked in section P as R11-FP01–FP18: typed ontology mappings,
approved ontology-version context binding, routine retrieval and a full ingestion-to-answer
test. The broader six-adapter program remains a proposal in the
[design document](../../../Docs/10-architecture/20-database-footprint-and-agent-context.md).
