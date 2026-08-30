# Coding Standards

> Status: Authoritative. Owner: Engineering.
> Standards that are not mechanically enforced are suggestions. Everything here that can be checked, is checked in CI.

## 1. Toolchain

| Tool | Purpose | Gate |
|---|---|---|
| `ruff` | Lint + format | Fails CI — **wired** (`.github/workflows/ci.yml`, `quality` job) |
| `mypy --strict` | Type checking | Fails CI — **wired** (`quality` job) |
| `import-linter` | Module boundary contracts | Fails CI — **wired** (`quality` job); 4 contracts as of 2026-08-30, incl. INV-2 gateway exclusivity and the C4/ST-11 lineage→gateway direction. Cross-module contracts await the extraction — see `10-architecture/04-module-decomposition.md` §5.2 |
| `alembic` | Migrations | Single-head check fails CI — **wired** (`migrations` job) |
| `pytest` | Tests | Fails CI — **wired** (`tests` job); includes the Tier-0 invariant suite |
| `bandit` / SAST | Security lint | Fails CI on high — **not wired yet**; tool not in `dev` extras |
| `pip-audit` | Dependency vulnerabilities | Fails CI on critical — **not wired yet**; tool not in `dev` extras |

CI exists as of 2026-08-30 (`.github/workflows/ci.yml`, tracker ST-02). Before that date this
table described intent, not behaviour: there was no pipeline at all. The two unwired rows are
marked rather than removed because they are still the intended gate set — see the gap register.

## 2. Import-linter contracts

The mechanism that makes the modular monolith real. In `pyproject.toml`:

```toml
[[tool.importlinter.contracts]]
name = "Layered architecture"
type = "layers"
layers = [
    "atlas.modules.experience | atlas.modules.studio",
    "atlas.modules.context_products",
    "atlas.modules.agent_runtime | atlas.modules.retrieval | atlas.modules.tools | atlas.modules.model_gateway | atlas.modules.query_gateway",
    "atlas.modules.catalog | atlas.modules.profiling | atlas.modules.relationships | atlas.modules.semantics | atlas.modules.glossary | atlas.modules.lineage | atlas.modules.knowledge_graph | atlas.modules.quality",
    "atlas.modules.identity | atlas.modules.connectivity | atlas.modules.ingestion",
    "atlas.platform",
]
# governance and observability are cross-cutting and excluded from the layer check;
# their acyclicity is enforced by the "Cross-cutting acyclicity" contract below.

[[tool.importlinter.contracts]]
name = "Module privacy"
type = "forbidden"
source_modules = ["atlas.modules.*"]
forbidden_modules = [
    "atlas.modules.*.models",
    "atlas.modules.*.repository",
    "atlas.modules.*.service",
    "atlas.modules.*.schemas",
]
# only <module>.api and <module>.contracts are importable across modules

[[tool.importlinter.contracts]]
name = "Gateway exclusivity"          # INV-2
type = "forbidden"
source_modules = ["atlas.modules.*", "atlas.platform.*"]
forbidden_modules = ["atlas.modules.connectivity.execution"]
ignore_imports = ["atlas.modules.query_gateway.* -> atlas.modules.connectivity.execution"]

[[tool.importlinter.contracts]]
name = "Platform purity"
type = "forbidden"
source_modules = ["atlas.platform"]
forbidden_modules = ["atlas.modules"]

[[tool.importlinter.contracts]]
name = "Cross-cutting acyclicity"
type = "forbidden"
source_modules = ["atlas.modules.governance", "atlas.modules.observability"]
forbidden_modules = ["atlas.modules.catalog", "atlas.modules.semantics", "atlas.modules.agent_runtime"]
# the two cross-cutting modules must never call back into a domain module
```

**Exemptions require an ADR.** A silently added `ignore_imports` line is how a modular monolith becomes a monolith again.

## 3. Typing

| Rule | Reason |
|---|---|
| `mypy --strict`; no `Any` in public signatures | The interface is the contract |
| No untyped `def` | Strict mode |
| DTOs are `@dataclass(frozen=True, slots=True)` | Immutable, memory-efficient at scale |
| Domain types over primitives — `TableId`, not `str` | A misrouted ID becomes a type error |
| `Literal` for closed enums | Exhaustiveness checking |
| Errors are typed and exported from `contracts.py` | Callers handle known failures |

## 4. Tenancy

```python
# Always
def list_tables(scope: TenantScope, filt: TableFilter, page: Page) -> Page[TableDTO]: ...

# Never — there is no such function, by design
def list_tables(filt: TableFilter) -> list[Table]: ...
```

The repository base class **requires** a scope. There is no unscoped helper to reach for (INV-5).

## 5. Bounds

Every operation that can return more than one thing takes a bound and reports truncation.

```python
@dataclass(frozen=True, slots=True)
class BoundedResult[T]:
    items: list[T]
    truncated: bool
    truncation_reason: str | None
```

Never `-> list[T]` for something that could be a million rows. A caller cannot tell a complete small result from a silently truncated large one (P3).

## 6. Error handling

| Rule | Reason |
|---|---|
| Typed errors from `contracts.py` | Callers branch on types, not strings |
| Never swallow an exception silently | A caught-and-ignored error is a future incident |
| Never include values or secrets in a message | INV-6 |
| Denials do not detail which check failed | Do not hand an attacker the control map |
| Every error carries a correlation ID | Support and audit |
| `retryable` is explicit | Callers must not infer it |

```python
# Never
except Exception:
    pass

# Never
raise ValueError(f"Bad row: {row}")          # leaks values

# Correct
except ConnectorTimeout as exc:
    logger.warning("connector timeout", extra={"datasource_id": ds_id, "correlation_id": cid})
    raise SourceUnavailable(datasource_id=ds_id, retryable=True) from exc
```

## 7. Logging

| Rule | Detail |
|---|---|
| Structured only | No f-string log messages |
| Tenancy and correlation on every line | Via context, not passed manually |
| **Never log values, questions, credentials, or SQL literals** | INV-6 |
| Scrubbing middleware, not convention | A convention fails the first time someone logs an exception containing a row |
| Levels | DEBUG development · INFO lifecycle · WARNING recoverable · ERROR needs attention · CRITICAL fail-closed |

## 8. Database access

| Rule | Reason |
|---|---|
| Repository classes only — no ad-hoc sessions in services | Scoping and unit-of-work discipline |
| Own schema only | MD-1 |
| No cross-schema FK except into `identity` | ADR-0015 |
| Explicit transactions; one unit of work per request | Audit atomicity (INV-7) |
| Every filter column indexed | An unindexed filter is a future outage |
| No `SELECT *` | Column drift breaks silently |
| Pagination always cursor-based | Stable at scale |
| Bulk operations chunked | Avoid long-held locks |

## 9. Async

| Rule | Detail |
|---|---|
| `async def` for IO-bound work | API handlers, database, HTTP |
| No blocking calls in async context | Use a thread pool if unavoidable |
| Explicit timeouts on every external call | No unbounded await |
| Cancellation handled and propagated | Temporal cancellation must reach the source |
| No fire-and-forget tasks | Use the outbox or a Temporal activity |

## 10. Testing

Covered fully in `40-engineering/04-testing-strategy.md`. The standards that belong here:

| Rule | Reason |
|---|---|
| Every module's tests run standalone | Extraction readiness |
| Fakes are built from `contracts.py` and tested against the same suite as the real implementation | Prevents fake drift |
| Every endpoint has a foreign-tenant denial test | INV-5 |
| Every bound has a truncation test | P3 |
| Every failure path has a test | "Fail closed" untested is "fail open" |

## 11. Documentation in code

| Required | Not required |
|---|---|
| Docstring on every public `api.py` function: purpose, scope semantics, failure modes | Docstrings on obvious private helpers |
| Comment explaining **why**, where the reason is non-obvious | Comments restating the code |
| A reference to the ADR when code implements a specific decision | — |

## 12. Commit and PR standards

| Rule | Detail |
|---|---|
| Conventional commits | `feat(catalog): …`, `fix(query_gateway): …` |
| One logical change per PR | Reviewable |
| PR body states which module, which invariants are touched, and which ADR applies | Reviewers check the right things |
| Migrations reviewed separately from logic | Different failure mode |
| No merge with a new import-linter exemption | Requires an ADR |

## Related documents

- Development spec: `40-engineering/01-development-spec.md`
- Testing strategy: `40-engineering/04-testing-strategy.md`
- Internal module contracts: `30-contracts/03-internal-module-contracts.md`
