# Review reconciliation ? 11 September 2026

Decision baseline: repository `06b0b56`, the new September 11 review, September 5 remediation
tracker/roadmap, August delivery tracker, September 9 agent review with September 11 updates,
and graph/five-feature implementation reports. This is a documentation and scope pass;
it does not claim the underlying fixes, deployments or live certifications were completed.

## Where to look

- **Work status and next action:** [tracker section P](03-tracker.md#p-current-execution-queue-reconciled-2026-09-11).
- **Measured capability evidence:** [capability register](20-capability-register.md), with each row's own date and limits.
- **Summary:** [delivery status](00-status.md); older tables there are dated snapshots.
- **Findings and rationale:** [September 11 review](../review-2026-09-11/REVIEW.md) and original reviews. They are not additional task queues.

All 79 open/partial work-item rows in the old tracker were inspected and given an explicit
disposition: 78 merged into named R11 successors, one closed (UX-16). Completed portions are
retained. `MERGED` means the duplicate ticket is closed; the successor remains pending.
Historical DONE entries were not blanket re-certified. New defects can reopen the affected
property without discarding the earlier fix.

## Proceed, continue, defer and cancel

**Proceed first:** D1/D2/D3, agent security remainders C3/C4/C6/C7/C8 and enforcement B3/D9.
D4 needs reproduction before claiming a defect. Fix D5?D8/D11/D12 and repair CI D13 alongside
these. Prepare C1's migration verification without interfering with another database session.

**Finish the journey:** B1 and B7, then B8/B4 and B11; B2 measures an approved live route once
the deterministic journey works. Begin B5/B6 prerequisite collection now. B3 and B6 are
required before a customer pilot; they are not optional late customer enhancements. B9/B10
complete audit/delivery evidence. B12 must name a decision for each producer, not silently
mark all four complete. B13 retains BI/consumption-lineage work. B14 refreshes evidence.

**Continue partial work without restarting it:** retain archive/delivery providers, OIDC,
atomic decisions, descriptions, all implemented adapters, lineage producers and relocated
modules. Implement only the remainder in the successor row and verify the intended end-to-end
property. C2 preserves accessibility/visual acceptance; C9/B15 preserve security/recovery.

**Defer broad simplification and expansion:** S1?S12 and most P items follow correctness and
journey proof. X items are bounded triage tasks, not approval to drop every named endpoint or
table. Start X9 with profiles only. No new agents, autonomy, screens or routers except what is
necessary for existing journey/security acceptance until B1?B4 pass. C12 holds customer-driven
expansion with a reopen trigger. A fixed million-object gate is replaced by agreed estate size;
capacity, tenant fairness and restore evidence remain mandatory for the claimed deployment.

**Cancel X7's removal:** REST `src/aida/api.py` and MCP `src/aida/mcp_server.py` now resolve
`caller_contract` and pass `agent_asset_version_id` into `GovernedAgentOrchestrator.run`.
`tests/test_ar06_contract_on_live_paths.py` covers both boundaries. Token budgets and contract
checks are used; preserve AR-05's completed PostgreSQL evidence. Cancel further schema-per-module
expansion as delivery scope under S6, retaining completed extractions and binding ADRs until
explicitly superseded. UX-16 is DONE: `ui/` is absent and September 5 D05 documents retirement.

## Corrections and omissions in the new review

| Finding | Correction / disposition |
|---|---|
| X7 calls the agent branch unreachable | Cancel removal: current REST and MCP callers exist. This also corrects the short-version and don't-build statements. |
| AR-03 described as waiting for an evaluation | Evaluation exists and the recorded result is unsafe: seven false twins still approved. C3 remains PARTIAL; unattended review stays off. |
| A single question corpus replaces every evaluation | B2 measures answers; preserve independent adversarial reviewer, injection, authorization, contract and race tests under S2. They protect different properties. |
| B3 is P0 but listed only with first-customer work in section 9 | Implement readiness/denial now; target-environment proof before pilot. B6 has the same pre-pilot deployment dependency. |
| No deployment exists, therefore deletion/migration squash is safe | Historical absence of a production artifact does not establish no shared/deployed databases or API users. X2/X5/P5 need current inventory and rollback evidence. |
| Delete red CI gates; make docs non-gating | Repair D13; retain equivalent security/compatibility/control coverage. P3 is deferred, not authorization to weaken gates. |
| P2 proposes untracked generated types while section 8 says keep them | P2 is a candidate only. Current committed types and diff gate stay until replacement build guarantees exist. |
| Existing ontology/worksheet/graph work omitted from follow-through | C1 carries retained-chain deployment checkpoint, C2 live visual acceptance; no further ontology expansion is scheduled. Old lock IDs and migration heads must be rechecked before action. |
| F19 marked fixed although lifecycle producer is unfinished | Scheduler consumer implementation remains complete; upstream event production/configuration belongs to B12. |
| AR-04/06/10/11 partials absent from build list | C4/C6/C7/C8 retain concurrency, boundary enforcement, MCP output screening and corrective-action gaps. |
| Accessibility, release security, model governance and masking omitted | C2/C9/C11/C13 preserve these obligations; external prerequisites do not justify closing them. |
| D1 suggests banning all driver connections outside connectors | Distinguish source SQL from legitimate platform database access; test the source execution boundary rather than an overbroad substring ban. |

## September 5 crosswalk

The [old points tracker](../review-2026-09-05/POINTS-TRACKER.md) remains evidence of that pass.
The following dispositions cover its unresolved, partial and deferred sections, including its
T-series roadmap. Historical completed items stay closed at their recorded scope.

| Earlier work | Current disposition |
|---|---|
| F01 / T02 cloud archive | Continue B9; D3 separately fixes compliance-pack claim. F02/F03/T01/T03 remain completed. |
| F04 / T04 real collector | Continue B10; retain working transport/ledger, obtain destination proof. |
| F06 / T07 / fresh-browser auth | Continue B6 and B11: corporate issuer, reload and real proxy topology. |
| F11 / T09 enforcement | Keep completed readiness/posture code; B3/D9 finish operator rollout and denial proof. |
| F19 / T19 lifecycle | Keep scheduler work; B12 finishes upstream producer and enablement evidence. |
| F21 / T17 / interactive UX | Keep shared infrastructure; C2 completes manual keyboard/screen-reader/visual acceptance. |
| F08 / F15 / F01 regressions | New D5/D7/D3 respectively; earlier fixes retain their historical evidence. |
| F20 / T14 results | Complete for fresh results; reopened runs do not retain rows by design. B1 covers parameter handoff, not invented result retention. |
| R01/R04 / T20?T27 structural remainder | S5/S6/P6, consolidated-family moves; old schema-by-schema rollout superseded. Completed R02/R03/R05/R06/R07 changes retained; X8 covers new duplicates. |
| D01 remaining OrgPicker | X8 rechecks current references. |
| D02 inactive producers | B12; no assumption that default-off equals unused. |
| D03 shim register | Completed inventory retained; P6 removes shims only after callers move. |
| D04 artifacts / D05 legacy UI | Completed; no repeat cleanup. UX-16 duplicate closed. P12 is separate current triage. |
| D06 doc truth | B14/P7/P8/P9; prior correction was not permanent proof against drift. |
| Outbox contention / p95 / fairness / pools / DR / T28 | B15 target-scale evidence; preserve current implementations. C9 and B5/B6/B9/B10/C11 cover release certifications. |
| Broader retention and legal hold | B9/S7, contingent on BD-6/9; do not drop retained evidence while simplifying. |
| Identity concepts and per-path authorization | B3/C6/C9; surface matrix does not certify policy parity. |
| Untrusted model output and representative evaluation | B2/C3/C7; retain INV-3 and separate refusal/security corpora. |
| Frontend dependency scanning | D13; current gate exists, repair and measure it. |
| T15 setup / T16 navigation / T18 review detail | B7 / S10 / S10, with B11 protecting task handoffs. |
| Other route/journey suggestions and new functionality | Only B1/B4/B7/B8/B11/B13 and C2 now; saved investigations, optional retention and remaining UX breadth deferred under C12 until a named journey/customer needs them. |
| UX measurement baseline | B15 for performance; optional product telemetry deferred under C12 pending purpose/retention decision. |

## Agent review and older delivery crosswalk

AR-01/02/05/07/08 remain closed at their documented scope. AR-03 ? C3, AR-04 ? C4,
AR-06 ? C6, AR-09 ? B15, AR-10 ? C7, AR-11 ? C8, AR-12 ? P8. Read the
[agent review](../10-architecture/15-agent-architecture-critical-review.md) for recorded
experiments and limits; a new boundary fix does not close the entire enforcement matrix.

Every older open work ID now points directly to a successor in its original tracker row,
including CN/IN/ST/PF/TS/AT work. BD-1?BD-12 remain customer/operational decisions, with
owners and prerequisites carried into successor exits. Drill history is retained; no missed
drill is relabelled completed. Freeze optional feature expansion, not release controls.

## Closure rule and validation

A task closes only with evidence matching its remaining acceptance: code path and relevant
regression proof for a defect; real topology/provider receipt for an integration; recorded
migration/rollback proof for data changes. Cancellation records why the capability is no
longer needed; deferral records what reopens it. Neither is a synonym for verified.

This pass rechecked source for native-policy connections, raw signing-key callers, glossary
links, expiry identity, REST/MCP contract binding, legacy UI absence and existing review tests.
It did not rerun the full application suite, connect a live model/IdP/warehouse, inspect the
live database, or remeasure historical CI failure rates and repository size. The original
review measurements retain their baseline/date. Documentation validation results are recorded
in the delivery handoff for this pass.

## Concurrent implementation observed during reconciliation

Other work changed application files while this documentation pass was running. The final
diff inspection found candidate fixes for D2 (provider-backed digests), D3 (database-only
compliance wording), D5 (hash navigation), D6 (session principal) and D8 (backend-gated
reconciliation). Those five current queue rows are PARTIAL, naming the observed code and the
remaining verification. These edits were not made or tested by this documentation pass;
do not restart them or treat them as completed acceptance. Retrieval-stage edits were also
present and are left to their owning implementation task. The baseline commit remained
`06b0b56`; the observations include uncommitted working-tree changes.

## Validation results for this documentation pass

- `python scripts/check_docs_links.py`: passed; every relative link in 226 Markdown files resolves.
- `.venv/Scripts/python.exe -m pytest tests/test_doc_claims.py -q`: passed (exit 0); the suite's existing skips remain. This checks citations/document claims, not the application defects.
- Tracker consistency check: all 64 September 11 findings represented, 12 additional carry-forward packages, 78 valid legacy successor mappings, UX-16 closed, no duplicate R11 IDs.
- `git diff --check -- Docs README.md`: passed.

At handoff the 76 current work packages comprise 32 TODO, 11 PARTIAL, 8 BLOCKED,
24 DEFERRED and 1 CANCELLED. Deferred work is outside the current execution scope;
blocked items name required prerequisites. The canonical row statuses remain in tracker
section P; these counts are this pass's dated validation snapshot.
