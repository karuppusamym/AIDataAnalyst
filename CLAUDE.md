# Atlas — working agreement for sessions on this repository

Four rules. They exist because this repository is worked by several agent
sessions at once, and the failure modes below have all already happened here.

1. **The work queue is tracker section P.**
   [`Docs/60-delivery/03-tracker.md`](Docs/60-delivery/03-tracker.md#p-current-execution-queue-reconciled-2026-09-11)
   section P is the only current status authority. Start work from a section P
   row. The [capability register](Docs/60-delivery/20-capability-register.md)
   is the companion authority for dated implementation/verification evidence —
   status lives in section P, evidence lives in the register.

2. **Reviews and session logs are evidence, not queues.**
   `Docs/review-*/` and the numbered session logs under `Docs/60-delivery/` are
   dated snapshots, measured against the tree at the time. They are deliberately
   *not* updated in place, so a claim in one can be stale without being wrong.
   Corrections and new work go into section P. Do not open a second queue: two
   live queues is what produced the duplicate work section P was created to end.
   The [2026-09-11 reconciliation](Docs/60-delivery/23-review-reconciliation-2026-09-11.md)
   records how the prior reviews were folded in and why each item was kept,
   merged or dropped.

3. **Several sessions share this branch.**
   HEAD moves under you. Re-run `git log --oneline -3` and `git status` before
   asserting what HEAD contains, before claiming a file is unchanged, and before
   writing any summary — a report that was accurate when drafted can be stale by
   the time it is sent. A red gate or an unfamiliar edit is often a peer's
   in-flight work, so attribute it from the failure's own text before acting.

4. **Never stage another session's in-flight files.**
   Commit the paths you edited, by name. `git add -A` and `git commit -a` sweep
   up whatever a peer happens to have open — including untracked drafts and
   regenerated artifacts (the OpenAPI baseline, `ui-next/src/lib/types.ts`, the
   surface-control matrix are the usual collisions). If a file you need is also
   being edited elsewhere, commit your hunk rather than the file.
