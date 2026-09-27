#!/usr/bin/env python3
"""R11-MP08: propose better SQL-generation guidance for one organization, offline.

Runs `aida.prompt_optimizer` against a datasource: every candidate guidance is
scored by drafting each corpus question through the governed stages (nothing
executes), and revisions come from an approved SQL_GENERATION route. The best
guidance is recorded as a DRAFT PROMPT-kind AI asset version with its evaluation.

Nothing is activated. The version is submitted and approved through the ordinary
AI asset review (`POST /v1/ai-asset-versions/{id}/submit`, then a different
person decides the review); approval is refused unless the evidence shows the
guidance scored no worse than the current one over enough cases with no unsafe
statement (`prompt_registry.prompt_approval_problem`).

It makes model calls -- one draft per case per candidate, plus one reflection per
iteration -- so it needs model generation enabled and an approved route, and it
spends the organization's model-token quota like any other caller.

Usage::

    AIDA_ENVIRONMENT=development python scripts/optimize_sql_instruction.py \\
        --organization-id <uuid> --datasource-id <uuid> --principal <operator> \\
        [--corpus tests/fixtures/quality_benchmark_corpus/execution_match_corpus.json] \\
        [--iterations 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.config import get_settings
from aida.db import session_factory
from aida.models import DataSource
from aida.prompt_optimization_run import LiveReflector, LiveScorer, record_prompt_version
from aida.prompt_optimizer import OptimizationCase, optimize_instruction
from aida.prompt_registry import SQL_SAFETY_CLAUSE, active_sql_instruction
from aida.security import SecurityContext

DEFAULT_CORPUS = (
    Path(__file__).resolve().parent.parent
    / "tests/fixtures/quality_benchmark_corpus/execution_match_corpus.json"
)


def load_cases(path: Path) -> list[OptimizationCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data["cases"] if isinstance(data, dict) else data
    return [
        OptimizationCase(id=str(c["id"]), question=str(c["question"]), gold_sql=c.get("gold_sql"))
        for c in raw
    ]


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    organization_id = UUID(args.organization_id)
    cases = load_cases(Path(args.corpus))
    async with session_factory() as session:
        datasource = await session.get(DataSource, UUID(args.datasource_id))
        if datasource is None or datasource.organization_id != organization_id:
            print("datasource not found in that organization", file=sys.stderr)
            return 2
        routes = await GovernedAgentOrchestrator(settings)._approved_model_routes(
            session, organization_id
        )
        if not routes:
            print("no approved SQL_GENERATION route for this organization", file=sys.stderr)
            return 2
        active = await active_sql_instruction(session, organization_id)
        baseline = (
            active.text[len(SQL_SAFETY_CLAUSE) :].strip() if active.version_id is not None else ""
        )
        context = SecurityContext(
            principal_id=args.principal,
            principal_type="USER",
            organization_id=organization_id,
            roles=frozenset({"Analyst"}),
        )
        result = await optimize_instruction(
            baseline,
            cases,
            score=LiveScorer(session, settings, datasource, context),
            reflect=LiveReflector(session, settings, organization_id, routes[0]),
            iterations=args.iterations,
        )
        version = await record_prompt_version(
            session, organization_id=organization_id, principal_id=args.principal, result=result
        )
        await session.commit()
    print(
        json.dumps(
            {
                "prompt_version_id": str(version.id),
                "version": version.version,
                "baseline_mean": round(result.baseline_mean, 4),
                "candidate_mean": round(result.candidate_mean, 4),
                "unsafe_cases": result.unsafe_cases,
                "evaluated_cases": result.evaluated_cases,
                "accepted_children": result.accepted_children,
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--datasource-id", required=True)
    parser.add_argument("--principal", required=True, help="who is proposing the version")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--iterations", type=int, default=3)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
