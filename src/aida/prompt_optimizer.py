"""R11-MP08: propose a better SQL-generation instruction from evidence, offline.

A reflective, Pareto-selecting search in the style of GEPA (DataPilot's
`gepa.py`), with every part that touches a model or a source injected, so the
loop itself is deterministic and testable:

1. **Baseline.** The current guidance is scored on every case.
2. **Select.** A parent is drawn from the Pareto front -- candidates that score
   best on at least one case -- weighted by how many cases each wins.
3. **Reflect.** The parent runs on a small minibatch; the reflection function
   (a model call, in live use) reads what went wrong and proposes revised
   guidance.
4. **Accept.** The child is kept only if it beats its parent on that minibatch;
   it is then scored on every case and joins the pool.
5. **Propose.** The candidate with the best mean is the proposal.

It proposes; it never activates. The output is evidence for a PROMPT-kind AI
asset version (`aida.prompt_registry`), which is used only once a person
approves it, and whose guidance always follows the fixed safety clause.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class OptimizationCase:
    id: str
    question: str
    gold_sql: str | None = None


@dataclass(frozen=True, slots=True)
class CaseScore:
    """One case under one instruction. `score` in [0, 1]; `unsafe` when the
    statement was refused for a security reason (never repairable)."""

    score: float
    unsafe: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


ScoreFn = Callable[[str, OptimizationCase], Awaitable[CaseScore]]
ReflectFn = Callable[[str, list[dict[str, Any]]], Awaitable[str]]


@dataclass(slots=True)
class _Candidate:
    guidance: str
    scores: dict[str, CaseScore]

    @property
    def mean(self) -> float:
        return sum(s.score for s in self.scores.values()) / max(len(self.scores), 1)

    @property
    def unsafe(self) -> int:
        return sum(1 for s in self.scores.values() if s.unsafe)


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    baseline_guidance: str
    best_guidance: str
    baseline_mean: float
    candidate_mean: float
    unsafe_cases: int
    evaluated_cases: int
    iterations: int
    accepted_children: int
    history: list[dict[str, Any]]

    def evidence(self, instruction_sha256: str) -> dict[str, Any]:
        return {
            "instruction_sha256": instruction_sha256,
            "baseline_mean": round(self.baseline_mean, 4),
            "candidate_mean": round(self.candidate_mean, 4),
            "unsafe_cases": self.unsafe_cases,
            "evaluated_cases": self.evaluated_cases,
            "iterations": self.iterations,
            "accepted_children": self.accepted_children,
            "history": self.history,
        }


MINIBATCH: Final = 3


async def _score_all(
    guidance: str, cases: Sequence[OptimizationCase], score: ScoreFn
) -> dict[str, CaseScore]:
    return {case.id: await score(guidance, case) for case in cases}


def _pareto_parent(
    pool: list[_Candidate], cases: Sequence[OptimizationCase], rng: random.Random
) -> _Candidate:
    wins = [0] * len(pool)
    for case in cases:
        best = max(c.scores[case.id].score for c in pool)
        for index, candidate in enumerate(pool):
            if candidate.scores[case.id].score == best:
                wins[index] += 1
    front = [c for c, w in zip(pool, wins, strict=True) if w > 0]
    weights = [w for w in wins if w > 0]
    chosen: _Candidate = rng.choices(front, weights=weights, k=1)[0]
    return chosen


async def optimize_instruction(
    baseline_guidance: str,
    cases: Sequence[OptimizationCase],
    *,
    score: ScoreFn,
    reflect: ReflectFn,
    iterations: int = 3,
    minibatch: int = MINIBATCH,
    seed: int = 0,
) -> OptimizationResult:
    """Search for guidance that scores better than `baseline_guidance`."""
    if not cases:
        raise ValueError("an optimisation needs at least one case")
    rng = random.Random(seed)  # noqa: S311 -- sampling, not cryptography
    baseline = _Candidate(baseline_guidance, await _score_all(baseline_guidance, cases, score))
    pool = [baseline]
    history: list[dict[str, Any]] = [
        {"step": 0, "event": "baseline", "mean": round(baseline.mean, 4)}
    ]
    accepted = 0
    for step in range(1, iterations + 1):
        parent = _pareto_parent(pool, cases, rng)
        batch = rng.sample(list(cases), k=min(minibatch, len(cases)))
        failures = [
            {
                "question": case.question,
                "gold_sql": case.gold_sql,
                "score": parent.scores[case.id].score,
                **parent.scores[case.id].detail,
            }
            for case in batch
            if parent.scores[case.id].score < 1.0
        ]
        if not failures:
            history.append({"step": step, "event": "parent_perfect_on_minibatch"})
            continue
        child_guidance = (await reflect(parent.guidance, failures)).strip()
        if not child_guidance or child_guidance == parent.guidance:
            history.append({"step": step, "event": "no_new_guidance"})
            continue
        child_batch = {case.id: await score(child_guidance, case) for case in batch}
        parent_batch_mean = sum(parent.scores[c.id].score for c in batch) / len(batch)
        child_batch_mean = sum(s.score for s in child_batch.values()) / len(batch)
        if child_batch_mean <= parent_batch_mean:
            history.append(
                {
                    "step": step,
                    "event": "child_rejected",
                    "parent_minibatch": round(parent_batch_mean, 4),
                    "child_minibatch": round(child_batch_mean, 4),
                }
            )
            continue
        remaining = [case for case in cases if case.id not in child_batch]
        full = {**child_batch, **await _score_all(child_guidance, remaining, score)}
        child = _Candidate(child_guidance, full)
        pool.append(child)
        accepted += 1
        history.append({"step": step, "event": "child_accepted", "mean": round(child.mean, 4)})
    # The proposal: best mean among candidates with no unsafe case; the baseline
    # always qualifies as a fallback proposal only if it is itself safe.
    safe = [c for c in pool if c.unsafe == 0] or pool
    best = max(safe, key=lambda c: c.mean)
    return OptimizationResult(
        baseline_guidance=baseline_guidance,
        best_guidance=best.guidance,
        baseline_mean=baseline.mean,
        candidate_mean=best.mean,
        unsafe_cases=best.unsafe,
        evaluated_cases=len(cases),
        iterations=iterations,
        accepted_children=accepted,
        history=history,
    )
