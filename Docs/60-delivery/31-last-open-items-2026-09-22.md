# The last open items of the pre-demo pass: 2026-09-22

A dated addendum to the [2026-09-21 evening session log](30-round-12-review-and-demo-finalization-2026-09-21.md):
evidence, not a queue (`CLAUDE.md`, rule 2). Status lives in
[tracker section P](03-tracker.md#p-current-execution-queue-reconciled-2026-09-11).

**Asked for:** for the two items left open, "if needed then implement, else remove completely", and a
summary of the overall counts.

**Decided.** R11-AUD11's last item (the proxy read timeout) was needed: a slow success on an upload was a
504 for a batch the API went on to record. R11-VAL06 had two halves: coverage by business domain or line of
business was needed (the API already answered it and the view could not ask), and a screen that creates a
reviewed bulk operation directly was not (every operation type the route takes already has a purpose-built
flow in the UI), so it was dropped from the queue rather than built. The route stays, because a verification
script, the glossary contract tests and those flows use it.

## What changed

| Commit | Change | Row |
|---|---|---|
| `3168c6d` | The envelope and upload locations wait 300 s for the API (`proxy_read_timeout`, `proxy_send_timeout`), measured first: the workbook's worst case, a sheet just under the reader's 128 MiB uncompressed cap (58,649 rows), parsed in 9 to 11 s here. `check_proxy_contract.py` refuses a shorter or missing timeout on those routes | R11-AUD11 DONE |
| `67131e9` | Found while measuring: that sheet parsed to 50,000 rows, and the import recorded a batch from them without a word, although the reader flagged the sheet as cut. A cut Tables or Columns sheet is now refused with a 422 before any batch is written | R11-AUD11 |
| `cf835e7` | The Coverage view's Scope select offers business domains (from the business map, which admits exactly the coverage roles) and lines of business (from their own list, offered only to the roles it admits), alongside the organization and the data sources | R11-VAL06 DONE |
| this commit | This addendum, section P (two rows closed, a recount), the register, `00-status.md` and the walkthrough pack | |

## Checks

| Check | Result |
|---|---|
| The 18 static CI gates, at the same source as `cf835e7` | **18 of 18 pass** |
| Tests of the changed areas | proxy contract 70; model import and body limits 242; coverage screen, client and demo 80, three mutants caught; journey stub contract 4 |
| UI suite, whole, alone | 148 files, 2,158 tests passed; `tsc` clean |
| Playwright journeys (isolated stub stack, UI built from `cf835e7`) | 46 passed |
| The stack rebuilt from a clean worktree of the same source (see below) | parity **9 of 9** (source digest `7f50718814af`); role matrix 5,547 and 2,405 probes, **0 unexpected**; read sweep 2,496 calls, **0 server errors**; accessibility audit 40 screens, **0 violations** in either theme; rehearsal **50 screens, 0 issues**; the deployed nginx carries `proxy_read_timeout 300s` in both long-request blocks; in a browser the Customer domain read 25.00% over 4 tables and Retail Banking 9.65% over 19 |

## The counts, after this

92 DONE, 27 PARTIAL, 7 BLOCKED, 0 TODO, 22 DEFERRED, 2 CANCELLED over 150 rows; 34 of the 126 active rows
are open, and every one of them is PARTIAL or BLOCKED. Of the 27 PARTIAL rows, most wait on something outside
this repository -- a real customer system (B5's connectors behind C14 and FP02), a person (C2's accessibility
acceptance), an operator's setting or port (FP16, FP17), an owner's decision or a paid run that has not been
authorised (FP04, FP05, FP06, FP13, SQL01, OKF03, GQL01's calibration), or a real destination (B9, B10, I1,
D17's other environment). The ones whose remainder is code are FP01, FP03, FP07, FP09, FP12, FP15, OKF02,
REV01 and S13.

**The build and the branch.** The three commits were made in a clean worktree of `b357500` and deployed from it as `4f15579`. Meanwhile a peer session committed and pushed `0fc3ceb` (documentation only: the CI result, in `00-status.md` and session log 30). The commits were replayed onto it unchanged, as `3168c6d`, `67131e9` and `cf835e7`, with no source difference from `4f15579`, which is why the parity digest of the running stack matches the branch.
