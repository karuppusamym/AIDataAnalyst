# Repo hygiene — prepared, not executed

Status: **Awaiting a decision from the repository owner.** No git state was modified.

## What is there

`scratch/` is correctly listed in `.gitignore` (line 20), but `.gitignore` does not untrack
files that were already committed. Nineteen files under `scratch/` remain tracked:

| | |
|---|---|
| Tracked files under `scratch/` | 19 |
| Size in the current checkout | **7.9 MB** |
| Of which binary tarballs | `repo_bundle{,2..8}.tar.gz`, `repo_live.tar.gz` — 9 archives, incl. 2 under `scratch/_to_delete/` |
| Objects in history touching `scratch/` | 26 |
| Total `.git` size | 22 MB |

So roughly a third of the repository's git directory is snapshots of the repository.

## Why this was not done automatically

Two reasons, and only the first is about caution:

1. **Removing the blobs from history rewrites shared history.** The current branch is
   `feature/snowflake-dbt-lineage-mcp`. Anyone who has fetched it would need to re-clone or
   hard-reset. That is a call for whoever owns the remote, not for a review pass.
2. **The safe half still dirties the index.** `git rm --cached` stages 19 deletions, and the
   working tree already carries unrelated uncommitted edits (`ui/styles/*.css`). Mixing those
   into one staged state without being asked to commit would leave the tree in a shape nobody
   chose.

## The two options

### Option A — stop carrying them forward (safe, reversible, recommended)

Removes the files from the index and from all future commits. History keeps the blobs, so
`.git` does not shrink, but the working tree stops shipping 7.9 MB of archives and nothing
new accumulates. Fully reversible with `git reset`.

```bash
git rm -r --cached scratch/
git commit -m "Untrack scratch/: it is gitignored but was committed before the ignore rule"
```

The files stay on disk. `.gitignore` already prevents them coming back.

### Option B — remove them from history as well (destructive, coordinate first)

Only worth it if repository size actually matters — 22 MB is not painful today, so this is
optional and can wait for a natural moment such as a branch merge.

```bash
# requires git-filter-repo; run on a fresh clone, never on a working tree with local edits
git filter-repo --path scratch/ --invert-paths
# then force-push and tell every clone holder to re-clone
```

**Do not run Option B without confirming who else has this branch.** If in doubt, Option A is
enough: it stops the bleeding, and history size is a cosmetic problem until it isn't.

## One more thing to decide

`scratch/` currently holds nine `proof-gaps-*` / `status-matrix-verification*` markdown reports
alongside the archives. Those read like real working notes rather than disposable output. If any
of them are worth keeping, move them into `Docs/` before Option A untracks them — otherwise they
survive only in history, which is a poor place to look for a document you meant to keep.
