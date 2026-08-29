# Reactivation Gap — Closed

## What was actually missing

Re-checking the code (not just my earlier notes) showed the *logic* was never missing. `_get_or_create_catalog`, `_get_or_create_schema`, `_get_or_create_table`, `_get_or_create_column`, and `_get_or_create_constraint` in `src/aida/workflows/activities.py` all look up existing rows by name **without filtering on status**, so a `DEPRECATED` row is found again on re-discovery — and the `else` branch unconditionally resets `status = "ACTIVE"` and `deprecated_at = None`. My earlier update ("I found no matching reactivation logic... anywhere in the current tree") was wrong; I'd only grepped for the word "reactivat*" and it isn't spelled that way in the code. What was genuinely missing was a **test** proving this behavior — the same pattern as every other gap in this audit.

## What I added

Two new tests in `tests/test_high_stakes_behaviors.py` (already an untracked, in-progress file — extended it rather than creating a parallel one):

- `test_rediscovered_catalog_reactivates_a_previously_tombstoned_record`
- `test_rediscovered_table_reactivates_a_previously_tombstoned_record`

Both follow the codebase's existing precedent of importing and calling the private `_get_or_create_*` functions directly (same pattern `test_connectors_oracle.py` already uses). Each builds a `MetadataCatalog`/`MetadataTable` with `status="DEPRECATED"` and a set `deprecated_at`, a fake session whose `.scalar()` returns that row, and a matching `Discovered*` fixture, then asserts: the row is reused (not recreated), `status == "ACTIVE"`, `deprecated_at is None`, the fingerprint changes, `tracker.created == 0` / `tracker.changed == 1`.

## Verification (clean environment, not the dev tree)

Bundled the current working tree fresh, built a new `python3.13` venv, `pip install -e ".[dev]"`, then:

| Check | Result |
|---|---|
| `pytest tests/test_high_stakes_behaviors.py` | 7 passed (5 existing + 2 new) |
| `pytest` (full suite) | **239 passed, 0 failed** (237 → 239) |
| `ruff check .` | All checks passed |
| `mypy src` | Success: no issues found in 70 source files |

## UI check

This is backend-only reconciliation logic with no dedicated UI surface, so there's nothing new to click through. What I did check: reloaded `localhost:3000` after the change — dashboard renders fully (live source counts, metadata runs, review queue, trust-controls panel all populated), zero console errors. Confirms the test-only addition didn't disturb the running stack.

## Bottom line on the original gap register

Both halves of the catalog-inventory gap ("tombstones missing objects" / "reactivates rediscovered ones") are now real, tested, and verified in a clean environment: tombstoning was closed in the work already in progress when I picked this up; reactivation is closed now.

## Two things worth your attention before anything gets committed

1. **`.git/index.lock` is stuck.** It's a 0-byte lock file with no owning process, and `git status` already warns it can't remove it (`Operation not permitted` — this sandbox's shell can't delete files by default). Any `git add`/`git commit` will fail until it's cleared. Delete it yourself (`del .git\index.lock` in PowerShell from the project root), or tell me to request delete permission and I'll clear it.
2. **Nothing has been committed.** The working tree still has the pre-existing uncommitted changes (tombstoning refactor, coverage scoring, UI decomposition) plus this reactivation test. I haven't touched git beyond `status`/`diff` — say the word if you want it committed, and let me know whether you want it as one commit or split (e.g. backend gap-closures vs. the UI refactor).
