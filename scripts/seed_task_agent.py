#!/usr/bin/env python3
"""Register a task agent the way the platform requires one to be registered.

Setting `AIDA_STEWARD_AGENT_INTERVAL_MINUTES=1` does not make the steward
agent run. `task_agent_schedule.registered_organizations` starts an agent only
in organizations where its workload identity holds an `AgentContract` whose
`AiAssetVersion` is **APPROVED** -- and on a freshly seeded estate there are
no AI asset versions and no agent contracts at all, so every task agent is
correctly and silently inert. That is the right behaviour and it was
undocumented: the acceptance guide told a reader to set an interval and watch
the agent work, which could not happen.

This script is the missing path. It drives the real handlers, in order, with
three identities:

    create_ai_asset                (maker: an AI author)
    create_ai_asset_version        (same maker)
    evaluate_agent_eval_gate       (steward: authors the exemplar corpus)
    submit_ai_asset_version        (same maker -- submitting is not deciding)
    decide_governance_review APPROVE  (checker: a *different* reviewer)
    put_agent_contract             (the version's registered owner)

Four controls make this longer than an `INSERT`, and each one is the point:

**An AGENT-kind version cannot be approved without a passing evaluation
gate.** `_decide_ai_asset_version` recomputes it live on every APPROVE and
never trusts stored evidence, so there is no way to manufacture a publish. The
gate needs at least one exemplar verdict at an 80% match rate, and a steward
authors that corpus explicitly (`--exemplars`, `--fail-one` to watch a failing
corpus refuse the approval).

**Maker may not be checker.** The decision runs with a second identity;
`--same-identity` makes the platform refuse it, which is the cheapest proof
the control is live.

**The contract is bound to the version's owner.** R11-C6 narrowed the direct
PUT to corrections and bound it to `owner_principal`, so the contract is
written as the owner and not as an administrator who happens to have a role.

**The capability envelope is a closed allowlist.** A contract with no
`tool_slugs` authorises no governed tool -- pass `--tool-slug` for each tool
the agent may execute, and nothing else becomes reachable.

Usage:
    python scripts/seed_task_agent.py --org sample-bank --agent steward
    python scripts/seed_task_agent.py --org sample-bank --agent steward --same-identity
    python scripts/seed_task_agent.py --org sample-bank --agent lineage --tier T1
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from sqlalchemy import select

from aida.agent_contract_api import AgentContractWrite, put_agent_contract
from aida.agent_eval_gate import (
    DEFAULT_AGENT_EVAL_GATE_THRESHOLD,
    AgentEvalGateEvaluateRequest,
)
from aida.ai_registry_api import (
    create_ai_asset,
    create_ai_asset_version,
    evaluate_agent_eval_gate_endpoint,
    submit_ai_asset_version,
)
from aida.config import get_settings
from aida.db import session_factory
from aida.models import AiAsset, AiAssetVersion, Organization
from aida.platform_schemas import AiAssetCreate, AiAssetDefinition
from aida.schemas import GovernanceDecisionRequest
from aida.security_types import SecurityContext
from aida.semantic_api import decide_governance_review
from aida.task_agent_registry import TASK_AGENTS

MAKER = "seed:ai-author"
STEWARD = "seed:steward"
CHECKER = "seed:reviewer"


def _context(
    organization_id: UUID, principal_id: str, roles: frozenset[str]
) -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type="USER",
        organization_id=organization_id,
        roles=roles,
    )


def _agent_spec(key: str):
    for registered in TASK_AGENTS:
        if registered.spec.key == key:
            return registered.spec
    raise SystemExit(
        f"unknown agent {key!r}; the registry holds "
        f"{sorted(a.spec.key for a in TASK_AGENTS)}"
    )


async def _create_asset(session, organization_id: UUID, agent_key: str, maker):
    """The asset the agent's versions hang off. Separate only so the re-run
    branch above stays readable."""
    return await create_ai_asset(
        organization_id,
        AiAssetCreate(
            asset_key=f"task-agent-{agent_key}",
            asset_kind="AGENT",
            name=f"{agent_key.title()} task agent",
            description=f"The platform's {agent_key} task agent. Proposes; never decides.",
            intended_use=(
                "Propose governed metadata changes for human review, within the "
                "tier and envelope its contract names."
            ),
            owner_principal=MAKER,
            provider_type="SELF_HOSTED",
            risk_tier="LOW",
        ),
        maker,
        session,
    )


async def seed(
    *,
    organization_slug: str,
    agent_key: str,
    tier: str,
    tool_slugs: list[str],
    exemplars: int,
    fail_one: bool,
    same_identity: bool,
) -> int:
    settings = get_settings()
    spec = _agent_spec(agent_key)
    principal = spec.principal(settings)

    async with session_factory() as session:
        organization = await session.scalar(
            select(Organization).where(Organization.slug == organization_slug)
        )
        if organization is None:
            raise SystemExit(
                f"no organization with slug {organization_slug!r}; "
                "run scripts/seed_sample_estate.py first"
            )
        organization_id = organization.id
        # Each handler below is written for FastAPI's request-scoped session,
        # which commits when the request ends. Driven in-process they share one
        # session, so each step is committed before the next reads it back --
        # otherwise `create_ai_asset_version` answers 404 for the asset that
        # was just created.

        maker = _context(
            organization_id, MAKER, frozenset({"AiAssetAuthor", "AgentDeveloper"})
        )
        steward = _context(
            organization_id, STEWARD, frozenset({"AiAssetAuthor", "DataSteward"})
        )
        checker = _context(
            organization_id,
            MAKER if same_identity else CHECKER,
            frozenset({"Reviewer", "PlatformAdmin"}),
        )
        owner = _context(organization_id, MAKER, frozenset({"AgentDeveloper"}))

        existing = await session.scalar(
            select(AiAssetVersion)
            .join(AiAsset, AiAsset.id == AiAssetVersion.asset_id)
            .where(
                AiAssetVersion.organization_id == organization_id,
                AiAsset.asset_key == f"task-agent-{agent_key}",
                AiAssetVersion.status == "APPROVED",
            )
        )
        if existing is not None:
            print(f"  an APPROVED version already exists ({existing.id}); reusing it")
            version_id = existing.id
        else:
            # Re-runnable, and not only for convenience: a run that gets as
            # far as creating the asset and then fails the evaluation gate has
            # committed the asset, and an operator's second attempt must
            # continue rather than collide with its own first one.
            asset = await session.scalar(
                select(AiAsset).where(
                    AiAsset.organization_id == organization_id,
                    AiAsset.asset_key == f"task-agent-{agent_key}",
                )
            )
            if asset is None:
                # `create_ai_asset` registers the asset **and its first
                # version** in one call and returns the *version*. Treating
                # that return value as an asset and asking for another version
                # off it answers 404 -- which is what happened here, and was
                # hidden by the reuse branch below on every re-run.
                print("1. create the AI asset and its v1 (maker)")
                version = await _create_asset(
                    session, organization_id, agent_key, maker
                )
                await session.commit()
                print(f"   asset {version.asset_id}, version {version.id}")
            else:
                print(f"1. the AI asset already exists ({asset.id}); adding a version")
                version = await create_ai_asset_version(
                    asset.id,
                    AiAssetDefinition(
                        name=f"{agent_key.title()} task agent",
                        description=f"A further version of the {agent_key} task agent.",
                        intended_use=(
                            "Propose governed metadata changes for human review."
                        ),
                        owner_principal=MAKER,
                        provider_type="SELF_HOSTED",
                        risk_tier="LOW",
                    ),
                    maker,
                    session,
                )
                await session.commit()
                print(f"   version {version.id}")
            version_id = version.id
            print(f"2. version {version_id} status={version.status}")
            print(
                f"3. a steward authors the exemplar corpus "
                f"({exemplars} verdict(s), threshold "
                f"{DEFAULT_AGENT_EVAL_GATE_THRESHOLD})"
            )
            verdicts = [
                {
                    "case_id": f"exemplar-{index + 1}",
                    # `--fail-one` flips exactly one, which at the shipped
                    # threshold is enough to hold the approval back -- the
                    # cheapest demonstration that the gate decides.
                    "matched": not (fail_one and index == 0),
                    "drift": [] if not (fail_one and index == 0) else ["seeded-drift"],
                    "detail": "authored by scripts/seed_task_agent.py",
                }
                for index in range(max(1, exemplars))
            ]
            gate = await evaluate_agent_eval_gate_endpoint(
                version_id,
                AgentEvalGateEvaluateRequest(steward_authored_verdicts=verdicts),
                steward,
                session,
            )
            await session.commit()
            print(f"   gate verdict: {gate.verdict} (pass_rate={gate.pass_rate})")

            print("4. submit for review (same maker -- submitting is not deciding)")
            review = await submit_ai_asset_version(version_id, maker, session)
            await session.commit()
            print(f"   review {review.id}")

            who = "the SAME identity" if same_identity else "a different identity"
            print(f"5. decide APPROVE as {who}")
            try:
                await decide_governance_review(
                    review.id,
                    GovernanceDecisionRequest(
                        decision="APPROVE", reason="seeded task agent"
                    ),
                    checker,
                    session,
                )
            except Exception as exc:  # noqa: BLE001 -- the refusal is the point
                detail = getattr(exc, "detail", exc)
                if same_identity:
                    print(f"   REFUSED, as it must be: {detail}")
                    print(
                        "\nmaker-checker is live. Re-run without --same-identity "
                        "to complete the registration."
                    )
                    return 0
                print(f"   approval failed: {detail}")
                return 1
            await session.commit()
            refreshed = await session.get(AiAssetVersion, version_id)
            print(f"   version status={refreshed.status if refreshed else '?'}")
            if refreshed is None or refreshed.status != "APPROVED":
                print(
                    "\nThe version did not reach APPROVED, so no contract will be "
                    "written -- the evaluation gate is the usual reason. Re-run "
                    "without --fail-one."
                )
                return 1

        print(f"6. write the agent contract as the version's owner ({MAKER})")
        contract = await put_agent_contract(
            organization_id,
            version_id,
            AgentContractWrite(
                agent_principal_id=principal,
                capability_envelope={
                    "tool_slugs": tool_slugs,
                    "context_product_ids": [],
                    "write_lanes": [],
                },
                autonomy_tier=tier,
                supervisor_persona="STEWARD",
                kill_scope="AGENT",
                sampling_rate=0.05,
            ),
            owner,
            session,
        )
        await session.commit()
        print(f"   contract {contract.id} for {principal}, tier {tier}")

    print(
        f"\n{agent_key} agent registered. It runs once "
        f"AIDA_{agent_key.upper()}_AGENT_INTERVAL_MINUTES is above 0 and the\n"
        "fleet-scheduler has been recreated to see it:\n\n"
        f"    AIDA_{agent_key.upper()}_AGENT_INTERVAL_MINUTES=1  (in .env)\n"
        "    docker compose up -d fleet-scheduler\n"
        "    docker compose logs -f fleet-scheduler\n\n"
        "Its proposals arrive in the review queue as proposals, attributed to "
        f"{principal}."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="sample-bank", help="organization slug")
    parser.add_argument(
        "--agent",
        default="steward",
        help=f"one of {sorted(a.spec.key for a in TASK_AGENTS)}",
    )
    parser.add_argument("--tier", default="T1", choices=["T0", "T1", "T2", "T3"])
    parser.add_argument(
        "--tool-slug",
        action="append",
        default=[],
        dest="tool_slugs",
        help="a governed tool slug the agent may execute; repeatable",
    )
    parser.add_argument("--exemplars", type=int, default=3)
    parser.add_argument(
        "--fail-one",
        action="store_true",
        help="mark one exemplar unmatched, so the gate holds the approval back",
    )
    parser.add_argument(
        "--same-identity",
        action="store_true",
        help="decide with the maker's identity, to watch maker-checker refuse it",
    )
    args = parser.parse_args()

    return asyncio.run(
        seed(
            organization_slug=args.org,
            agent_key=args.agent,
            tier=args.tier,
            tool_slugs=list(args.tool_slugs),
            exemplars=args.exemplars,
            fail_one=args.fail_one,
            same_identity=args.same_identity,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
