# Relocating one bounded context out of `models.py` / `schemas.py`

> Status: Authoritative procedure. Owner: Engineering.
> Written 2026-09-06 from the `profiling` relocation (review-2026-09-05 point
> **R04**), immediately after doing it, so the next context is a mechanical
> repeat rather than a rediscovery. Every step below was actually executed; the
> "what went wrong" notes are things that actually went wrong, not hypotheticals.

R04's instruction is *"relocate one bounded context at a time with compatibility
exports. Separate persistence, API DTOs, and domain values."* This file is the
"one at a time" part written down. It is deliberately narrow: it covers moving a
context's **ORM models and API DTOs**, not its routes (that is the ST-07 Commit C
step the first five contexts went through separately) and not its tables into
their own PostgreSQL schema (refactor plan §5 steps 2.3/2.4, still deferred).

## What this procedure is not allowed to change

Four things, and each has a gate that proves it:

| Must not change | Proof |
|---|---|
| `Base.metadata` — tables, columns, types, constraints, indexes, and the single registry they live in | Mechanical before/after dump and diff (step 8), then `tests/test_migration_orm_drift.py` against a real PostgreSQL |
| The generated OpenAPI schema — not merely "no breaking changes" | Byte-identical `json.dumps(app.openapi(), sort_keys=True)` before/after, plus `scripts/openapi_diff.py` reporting **0** classified changes of any kind |
| Any caller | Nothing outside the moved files is edited. The `aida.models` / `aida.schemas` re-exports are the whole compatibility mechanism |
| Migration history | Nothing under `migrations/versions/` is touched. **If your change makes a migration necessary, you have changed the schema — stop.** |

## Before you start: is this context already relocated?

Read `src/atlas/modules/` first. Six directories exist as of 2026-09-06
(`catalog`, `connectivity`, `identity_tenancy`, `ingestion`,
`observability_audit`, `profiling`), and the first five already hold real models
and schemas. The generated
[architecture map](../10-architecture/14-generated-architecture-map.md)'s
*Bounded contexts* table lists every context with its owned-table count, which is
the fastest way to see what has already moved. `catalog` in particular looks like
an obvious first move and is already done.

## The procedure

### 1. Pick the context, and write down its edges

Use `Docs/10-architecture/04-module-decomposition.md` §4 (the module register:
what each module *owns*) and §9 (current file → target module). Then read the
docstrings of the contexts already relocated: they name what they deliberately
left behind and for whom. `atlas.modules.catalog.models` explicitly parks
`ClassificationEvidence` for module 05 — that sentence is what selected
`profiling` as this pass's context.

Decide the boundary cases **before** touching code, and write the decision into
the new module's docstring, including the ones you decided *against* moving.
Three shapes of boundary case came up:

- **A neighbour that copied your shape.** `PolicyNativeSyncRequest` sits between
  two profiling models and mirrors `ProfilingExceptionPolicy`'s maker-checker
  fields exactly — and belongs to the query gateway. Shape similarity is not
  ownership.
- **An aggregate DTO that mentions you.** `FleetSummaryRead` reports datasource
  statuses, analysis-run statuses, scan-policy counts and outbox backlog: four
  modules in one payload. It stays put until the surface that produces it moves.
- **A phrase in the register that points two ways.** §4 gives module 05 "key
  inferences" and module 06 "relationship candidates". `CompositeKeyCandidate`
  answers to both. Leave it for the module whose neighbourhood it lives in and
  say so, rather than splitting a cluster on one ambiguous word.

### 2. Capture the baselines — before any edit

This is step 2 and not step 9 for a reason: once you have edited the files, the
"before" is gone, and reconstructing it costs a `git worktree` (see "What went
wrong", item 1).

```bash
# 2a. Base.metadata, canonicalised and sorted.
AIDA_ENVIRONMENT=development ./.venv/Scripts/python.exe <dump script> > metadata_before.txt

# 2b. The generated OpenAPI spec, key-sorted so the diff is textual.
AIDA_ENVIRONMENT=development ./.venv/Scripts/python.exe -c "
import json, sys; sys.path.insert(0, 'scripts'); import openapi_diff
print(json.dumps(openapi_diff._load_current_spec(), indent=2, sort_keys=True))" > openapi_before.json
```

The metadata dump must import **every** module under `src/aida` and `src/atlas`
(`pkgutil.walk_packages`), not just `aida.models` — a table registered by a
module nobody imports is exactly the kind of thing this diff exists to catch. It
must emit, sorted and deterministically:

- `sorted(Base.metadata.tables)` and the table count;
- per table: every column with its type repr, nullability, primary-key flag,
  `unique`/`index` flags, autoincrement, and Python and server defaults;
- per table: every constraint (kind, name, columns; FK targets with `ondelete`
  and `onupdate`; check constraints with their SQL text);
- per table: every index (name, uniqueness, columns, and postgresql dialect
  options);
- `len({id(cls.registry) for cls in Base.registry.mappers})` — this is the
  single most important line. A relocated model that registers on a *second*
  declarative base is the silent catastrophe this whole procedure guards
  against, and it shows up here as `2` and nowhere else;
- the mapped class → table name list, twice: once qualified by the class's
  top-level package and once bare. The qualified list is *expected* to change —
  that is the move. The bare list must not.

### 3. Scaffold the module

```bash
./.venv/Scripts/python.exe scripts/generate_module.py <context>
```

Thirteen files, the uniform anatomy from
`10-architecture/04-module-decomposition.md` §7. Do not hand-roll it; the
generator is the reason all six modules look identical. It refuses to overwrite
an existing module directory.

### 4. Move the models verbatim

Cut the class bodies out of `src/aida/models.py` and paste them, byte-for-byte,
into `src/atlas/modules/<context>/models.py` under a docstring in the house style
(copy `catalog`'s: status, the tracker/review row, owned tables with one line
each on *why*, and an explicit "not moved here despite living next door" list).

Verbatim means verbatim: no reordering of columns, no reformatting of
`__table_args__`, no renaming, no tightening a `nullable`, no touching a
cross-module `ForeignKey`. Replacing cross-module FKs with plain ID columns is
refactor-plan step 2.4 — a separate, independently revertible change — and doing
it here would alter `Base.metadata`.

Import `Base`, `TimestampMixin` and `utc_now` from `atlas.platform.db`, which is
where the already-relocated modules get them and, critically, is **the same
`Base` object** `aida.models` uses through the `aida.db` shim. One base, one
registry.

Then add the re-export block to `src/aida/models.py`, alphabetised, in the
`X as X` form the five existing blocks use (the redundant alias is what makes it
an explicit re-export for mypy rather than an unused import):

```python
# Re-exported for backward compatibility -- <review/tracker row> moved the
# classes below to `atlas.modules.<context>.models` ...
from atlas.modules.<context>.models import (
    AnalysisRun as AnalysisRun,
    ...
)
```

### 5. Move the DTOs verbatim

Same operation on `src/aida/schemas.py`. Two constraints specific to this file:

- The re-export block **must sit below `ApiModel`'s definition**. The moved
  module imports `ApiModel` back from `aida.schemas`, so this is a genuine
  circular import that resolves only in that order. All five existing blocks
  carry `# noqa: E402, I001` for exactly this reason; yours needs it too.
- The module gets `from __future__ import annotations` (house style, and the
  generator template emits it), which `aida.schemas` does not have. Ruff will
  then flag the quoted self-referential return annotations on `model_validator`
  methods (`-> "ScanPolicyUpsert"`) as `UP037`. Unquoting them is safe — under
  PEP 563 every annotation is a string anyway and pydantic does not use a
  validator's return annotation — but it is the one place in this procedure
  where the moved text is *not* byte-identical, so it is the one place worth
  confirming against the OpenAPI diff rather than assuming.

### 6. Add the import-linter contract

Copy an existing `<context> module privacy` contract in `pyproject.toml`,
changing only the names. `protected_modules` is the six private files;
`allowed_importers` is the module's own files, its tests, plus `aida.models` and
`aida.schemas` — the two sanctioned shims and nothing else.

This contract is the reason the relocation is worth anything. Without it, the
next person who needs `AnalysisRun` imports
`atlas.modules.profiling.models` directly, and `models.py` shrinks while the
coupling it represented spreads over more files.

Note what does **not** go in the list: a sibling module that already imports a
relocated class through `aida.models` (as
`atlas.modules.connectivity.router` does for `ScanPolicy`) needs no entry. The
`protected` contract type checks **direct** imports only, so a chain through the
shim is fine — and adding the sibling would create a real module-to-module
coupling where today there is only a shim.

```bash
./.venv/Scripts/lint-imports.exe   # must report N+1 contracts kept, 0 broken
```

### 7. Update the generated docs and the hand-written index

Three of these are CI gates, and one of them fails *silently* — see "What went
wrong", item 2.

```bash
AIDA_ENVIRONMENT=development ./.venv/Scripts/python.exe scripts/shim_register.py
AIDA_ENVIRONMENT=development ./.venv/Scripts/python.exe scripts/generate_architecture_map.py
./.venv/Scripts/python.exe scripts/check_docs_links.py
```

Also, by hand:

- `scripts/generate_architecture_map.py` — **add the context to
  `BOUNDED_CONTEXTS`.** This tuple is hand-maintained and `group_of()` silently
  falls back to "aida domain modules"; `tests/test_architecture_map_contexts.py`
  now fails if a module directory is missing from it.
- `scripts/shim_register.py` — add the context to the `owner` string on both the
  `aida.models` and `aida.schemas` `Shim` entries, then regenerate.
- `Docs/20-modules/domain-guides/<context>.md` — a new guide, same four
  questions as the other five. The generated architecture map links to this path,
  so a missing guide is a broken link in a generated file.
- `Docs/20-modules/00-module-index.md` — the count in the status note, the
  domain-guide table, the `Module dir?` column for that module's row.

### 8. Prove `Base.metadata` did not change

Re-run the step-2a dump and diff it against the baseline:

```bash
diff metadata_before.txt metadata_after.txt
```

The **only** acceptable difference is the package-qualified mapped-class lines —
`aida:AnalysisRun->analysis_run` becoming `atlas:AnalysisRun->analysis_run`, once
per moved class. Every `TABLE` / `COL` / `CONSTRAINT` / `INDEX` line, the bare
class→table list, `table_count`, and `distinct_registries=1` must be identical.
If anything else moved, you did not do a verbatim move.

For the profiling pass this was 4,803 lines of dump, 192 tables, and exactly 18
differing lines — nine `aida:` removals and nine `atlas:` additions.

### 9. Run the gates

```bash
./.venv/Scripts/ruff.exe check .
./.venv/Scripts/mypy.exe src sdk/aida_tool_sdk
./.venv/Scripts/lint-imports.exe
# pytest needs AIDA_ENVIRONMENT unset; the scripts need it set.
./.venv/Scripts/python.exe -m pytest tests/ -q
./.venv/Scripts/python.exe -m pytest tests/test_migration_orm_drift.py -q
AIDA_ENVIRONMENT=development ./.venv/Scripts/python.exe scripts/openapi_diff.py
AIDA_ENVIRONMENT=development ./.venv/Scripts/python.exe scripts/shim_register.py --check
AIDA_ENVIRONMENT=development ./.venv/Scripts/python.exe scripts/generate_architecture_map.py --check
```

Run the **full** suite. With 155 direct importers of `aida.models`, a subset
proves very little.

`test_migration_orm_drift.py` is the strongest single piece of evidence
available: it applies every migration to a real PostgreSQL and diffs the result
against `Base.metadata`. Confirm it **ran** rather than skipped — it skips
silently when no PostgreSQL is reachable, and a skipped drift gate looks exactly
like a passing one in `-q` output. Point
`AIDA_MIGRATION_DRIFT_TEST_DATABASE_URL` at a scratch database, or run a local
PostgreSQL matching `Settings.database_url`'s default.

For the OpenAPI gate, "no breaking changes" is not the bar. Check the count of
changes of **any** classification:

```python
changes = openapi_diff.diff_specs(baseline, openapi_diff._load_current_spec())
assert len(changes) == 0
```

and diff the two spec dumps from step 2b directly. Byte-identical is the answer
you want.

Do **not** run `ruff format --check .` — it is red repo-wide on unmodified HEAD
and is not a CI gate.

## What went wrong, so it does not go wrong twice

1. **The metadata baseline was captured with a non-deterministic dump, and had
   to be recaptured from a `git worktree` after the edits.** Partial-index
   `where=` clauses hold a `TextClause` whose default `repr()` embeds a memory
   address, so seven index lines "differed" on every run for no reason. Render
   dialect options as `str(value)`. And because the edits were already made by
   the time this surfaced, the real baseline had to be produced from a clean
   checkout:

   ```bash
   git worktree add <scratch>/base-wt HEAD
   cd <scratch>/base-wt
   AIDA_ENVIRONMENT=development PYTHONPATH="$(cygpath -w "$PWD/src")" \
     /path/to/repo/.venv/Scripts/python.exe <dump script>
   ```

   `PYTHONPATH` wins over the editable-install `.pth`, so this really does
   import the pristine tree. It works, but capturing the baseline first is
   cheaper.

2. **`scripts/generate_architecture_map.py --check` stayed green while the map
   went wrong.** `BOUNDED_CONTEXTS` is a hand-maintained tuple and `group_of()`
   falls back to `"domain"` for unrecognised modules. The first regeneration
   after the move therefore reported the twelve new `atlas.modules.profiling.*`
   modules as **aida domain modules** — so "aida domain modules" went from 199 to
   211 in the same commit that made the monolith smaller, and the `--check` gate
   was perfectly happy. The map was wrong in the direction that makes a refactor
   look like a regression. `tests/test_architecture_map_contexts.py` now closes
   this both ways.

3. **`BigInteger` became an unused import in `aida/models.py`.** The moved
   classes took the file's only users of it with them. Ruff catches it; expect
   the same for whatever `sqlalchemy` name your context happens to monopolise,
   and check the reverse too (the new module needs the exact import set its
   classes use, no more).

4. **`ScanPolicy` is profiling's model but its two endpoints live in
   `atlas.modules.connectivity.router`.** That router imports it from
   `aida.models`, which after the move is a re-export — and that is the
   compatibility export working as designed, not something to "fix" by pointing
   the router at the private module. Resist the tidy-up; it would convert a shim
   dependency into a module-to-module one and require weakening the new contract
   on day one.

5. **The scaffold's `router.py` defines an `APIRouter` that nothing mounts.**
   That is correct for a models-and-DTOs-only pass, but say so in the docstring,
   and leave `api.py` *not* re-exporting it — otherwise the next reader assumes
   the routes moved too. The generated architecture map will show the context as
   "not mounted from `aida.main`" with 0 routes, which is the honest reading.

## What is still true after this procedure, and what is not

**True:** the context's persistence and its API DTOs live in one directory with a
mechanically enforced boundary; no caller changed; the schema is provably
untouched; the shim register counts exactly how many callers still come through
the old door and states the condition for closing it.

**Not true:** the context does not own its database schema (still one shared
PostgreSQL schema, one `Base`, one migration space), does not own its routes
unless a separate ST-07 pass moved them, and its `service.py` / `repository.py` /
`contracts.py` / `events.py` / `workers/` are empty scaffolds until behaviour
moves too. Say all of that in the domain guide. A module directory with real
models and empty everything-else is progress, not completion, and the guides are
the place that distinction is kept honest.

## Related documents

- [Refactor plan](06-refactor-plan.md) — the phase this belongs to (§6, Phase 3)
- [Module decomposition](../10-architecture/04-module-decomposition.md) — §4 the
  ownership register, §9 the file→module map
- [Compatibility shim register](09-compatibility-shim-register.md) — who still
  comes through the old door, and what must be true before it closes
- [Generated architecture map](../10-architecture/14-generated-architecture-map.md)
- [Domain guides](../20-modules/domain-guides/)
- Review point R04 in [`../review-2026-09-05/REVIEW.md`](../review-2026-09-05/REVIEW.md)
