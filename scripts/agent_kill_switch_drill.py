"""AG-10 / ADR-0027: an executable kill-switch drill.

`Docs/60-delivery/00-status.md` lists "kill-switch drill" as outstanding for
the model route and AI governance module. A drill is not a unit test: the
tests prove the function returns the right answer, the drill proves an
*operator* can stop an agent and see that it stopped, in the order and with
the evidence an incident would actually require.

What this exercises, end to end against a real database:

1. an agent with a contract runs -- so a later refusal means something;
2. each of the three kill scopes (`AGENT`, `TIER`, `ALL`) stops the very
   next run, and stops the right agents and no others;
3. the organization-wide model kill switch stops an agent whose own scope is
   `ALL`, i.e. the two switches compose rather than shadowing each other;
4. release restores service;
5. every engagement and release left an attributable audit row.

**Scope, stated honestly.** This runs against an in-memory SQLite database in
one process. It is evidence that the control's logic holds end to end; it is
**not** bank-scale evidence, it does not exercise a real deployment, a real
model provider, replication lag, or an operator's actual console, and it says
nothing about how fast a switch propagates across processes that cache
contract state (nothing does today -- every check is a live query, which is
the property that makes this drill meaningful at all).

Run:

    python scripts/agent_kill_switch_drill.py

Exit code 0 means every step behaved as the control requires; non-zero names
the first step that did not.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

import aida.models  # noqa: E402,F401 -- registers every table on the metadata
from aida.agent_contracts import (  # noqa: E402
    REASON_KILL_ENGAGED,
    agent_kill_blocking_reason,
)
from aida.db import Base  # noqa: E402
from aida.events import record_audit  # noqa: E402
from aida.model_gateway import GLOBAL_KILL_SWITCH_SCOPE  # noqa: E402
from aida.models import (  # noqa: E402
    AGENT_SAMPLING_RATE_FLOOR,
    AgentContract,
    AiAsset,
    AiAssetVersion,
    AuditEvent,
    KillSwitchState,
    Organization,
)
from aida.security import SecurityContext  # noqa: E402

#: One id for the whole drill, so every audit row it produces can be found
#: with a single query afterwards -- which is what an auditor asking "show
#: me the drill" actually needs.
_CORRELATION_ID = f"drill-{uuid4().hex}"


@dataclass(slots=True)
class Step:
    name: str
    passed: bool
    detail: str


class Drill:
    def __init__(self) -> None:
        self.steps: list[Step] = []

    def check(self, name: str, condition: bool, detail: str) -> None:
        self.steps.append(Step(name, bool(condition), detail))

    @property
    def ok(self) -> bool:
        return all(step.passed for step in self.steps)

    def report(self) -> str:
        lines = ["", "AG-10 kill-switch drill", "=" * 60]
        for step in self.steps:
            lines.append(f"[{'PASS' if step.passed else 'FAIL'}] {step.name}")
            lines.append(f"       {step.detail}")
        failed = [step for step in self.steps if not step.passed]
        lines.append("=" * 60)
        lines.append(
            f"{len(self.steps) - len(failed)}/{len(self.steps)} steps passed"
            + ("" if not failed else f" -- first failure: {failed[0].name}")
        )
        lines.append(
            "Scope: in-memory database, single process. Evidence that the "
            "control's logic holds end to end, not bank-scale evidence."
        )
        return "\n".join(lines)


def _operator(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id="operator-drill",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Operations"}),
    )


async def _seed_agent(
    session: AsyncSession,
    org: Organization,
    *,
    name: str,
    tier: str,
    kill_scope: str,
) -> AgentContract:
    asset = AiAsset(
        organization_id=org.id,
        asset_key=f"agent-{uuid4().hex[:8]}",
        asset_kind="AGENT",
        created_by="drill",
    )
    session.add(asset)
    await session.flush()
    version = AiAssetVersion(
        organization_id=org.id,
        asset_id=asset.id,
        version=1,
        status="APPROVED",
        name=name,
        description="Drill agent.",
        intended_use="Drill.",
        owner_principal="ops",
        provider_type="INTERNAL",
        risk_tier="LOW",
        context_product_version_ids=[],
        model_route_ids=[],
        policy_control_ids=[],
        evaluation_evidence={},
        runtime_evidence={},
        fingerprint=uuid4().hex,
        created_by="drill",
    )
    session.add(version)
    await session.flush()
    contract = AgentContract(
        organization_id=org.id,
        ai_asset_version_id=version.id,
        agent_principal_id=f"agent:{name.lower().replace(' ', '-')}-{uuid4().hex[:4]}",
        capability_envelope={
            "tool_slugs": ["drill-tool"],
            "context_product_ids": [],
            "write_lanes": [],
        },
        autonomy_tier=tier,
        supervisor_persona="OPERATOR",
        kill_scope=kill_scope,
        kill_engaged=False,
        sampling_rate=AGENT_SAMPLING_RATE_FLOOR,
        created_by="drill",
    )
    session.add(contract)
    await session.flush()
    return contract


async def _engage(session: AsyncSession, contract: AgentContract, reason: str) -> None:
    """What the operator's console does: flip the flag and audit it, in one
    transaction. The drill uses the same shape as `agent_contract_api`."""
    contract.kill_engaged = True
    record_audit(
        session,
        _operator(contract.organization_id),
        action="agent.kill_switch.engage",
        resource_type="agent_contract",
        resource_id=str(contract.id),
        outcome="SUCCESS",
        correlation_id=_CORRELATION_ID,
        details={"scope": contract.kill_scope, "reason": reason},
    )
    await session.flush()


async def _release(session: AsyncSession, contract: AgentContract, reason: str) -> None:
    contract.kill_engaged = False
    record_audit(
        session,
        _operator(contract.organization_id),
        action="agent.kill_switch.release",
        resource_type="agent_contract",
        resource_id=str(contract.id),
        outcome="SUCCESS",
        correlation_id=_CORRELATION_ID,
        details={"scope": contract.kill_scope, "reason": reason},
    )
    await session.flush()


async def run_drill() -> Drill:
    drill = Drill()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        org = Organization(name="Drill Bank", slug=f"drill-{uuid4().hex[:8]}")
        other_org = Organization(name="Other Bank", slug=f"other-{uuid4().hex[:8]}")
        session.add_all([org, other_org])
        await session.flush()

        subject = await _seed_agent(
            session, org, name="Steward agent", tier="T1", kill_scope="AGENT"
        )
        same_tier = await _seed_agent(
            session, org, name="Peer agent", tier="T1", kill_scope="TIER"
        )
        other_tier = await _seed_agent(
            session, org, name="Ops agent", tier="T2", kill_scope="TIER"
        )
        blanket = await _seed_agent(
            session, org, name="Red-team agent", tier="T3", kill_scope="ALL"
        )
        neighbour = await _seed_agent(
            session, other_org, name="Tenant agent", tier="T1", kill_scope="ALL"
        )

        # --- 1. baseline: everything runs ---------------------------------
        baseline = [
            await agent_kill_blocking_reason(session, contract)
            for contract in (subject, same_tier, other_tier, blanket, neighbour)
        ]
        drill.check(
            "1. Baseline -- no switch engaged, every agent runs",
            all(reason is None for reason in baseline),
            f"blocking reasons: {baseline}",
        )

        # --- 2. AGENT scope: stops itself, nothing else --------------------
        await _engage(session, subject, "drill: agent scope")
        drill.check(
            "2a. AGENT scope stops the agent it names",
            await agent_kill_blocking_reason(session, subject) == REASON_KILL_ENGAGED,
            "the switch takes effect on the very next check, not on redeploy",
        )
        drill.check(
            "2b. AGENT scope stops nothing else",
            await agent_kill_blocking_reason(session, same_tier) is None
            and await agent_kill_blocking_reason(session, other_tier) is None,
            "a per-agent switch that quietly stopped a peer would be unusable in an incident",
        )
        await _release(session, subject, "drill: restore")
        drill.check(
            "2c. Release restores service",
            await agent_kill_blocking_reason(session, subject) is None,
            "an operator must be able to undo a switch without a deployment",
        )

        # --- 3. TIER scope: stops the tier, not the neighbours -------------
        await _engage(session, same_tier, "drill: tier scope")
        drill.check(
            "3a. TIER scope stops every agent in that tier",
            await agent_kill_blocking_reason(session, subject) == REASON_KILL_ENGAGED
            and await agent_kill_blocking_reason(session, same_tier) == REASON_KILL_ENGAGED,
            "including agents whose own scope is narrower than the engaged one",
        )
        drill.check(
            "3b. TIER scope leaves other tiers running",
            await agent_kill_blocking_reason(session, other_tier) is None,
            "stopping T1 must not stop T2 -- that is what the tiers are for",
        )
        await _release(session, same_tier, "drill: restore")

        # --- 4. ALL scope: stops the organization, not the neighbour tenant -
        await _engage(session, blanket, "drill: organization scope")
        org_wide = [
            await agent_kill_blocking_reason(session, contract)
            for contract in (subject, same_tier, other_tier, blanket)
        ]
        drill.check(
            "4a. ALL scope stops every agent in the organization",
            all(reason == REASON_KILL_ENGAGED for reason in org_wide),
            f"blocking reasons: {org_wide}",
        )
        drill.check(
            "4b. ALL scope does not cross the tenant boundary (INV-5)",
            await agent_kill_blocking_reason(session, neighbour) is None,
            "one bank's incident must never stop another bank's agents",
        )
        await _release(session, blanket, "drill: restore")

        # --- 5. the two switches compose -----------------------------------
        session.add(
            KillSwitchState(
                organization_id=org.id,
                route_key=GLOBAL_KILL_SWITCH_SCOPE,
                engaged=True,
                reason="drill: organization-wide model kill switch",
                engaged_by="operator-drill",
                engaged_at=datetime.now(UTC),
            )
        )
        await session.flush()
        drill.check(
            "5. The organization-wide model kill switch also stops agents",
            await agent_kill_blocking_reason(session, blanket) == REASON_KILL_ENGAGED,
            "the AI kill switch and the agent kill switch compose; neither shadows the other",
        )

        # --- 6. every action is attributable -------------------------------
        audit_rows = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action.in_(
                        ["agent.kill_switch.engage", "agent.kill_switch.release"]
                    )
                )
            )
        ).all()
        engagements = [row for row in audit_rows if row.action.endswith("engage")]
        drill.check(
            "6. Every engagement and release is attributable (INV-7)",
            len(audit_rows) == 6
            and all(row.principal_id == "operator-drill" for row in audit_rows)
            and all(row.details.get("reason") for row in engagements),
            f"{len(audit_rows)} audit rows, each naming a principal and a reason",
        )

    await engine.dispose()
    return drill


def main() -> int:
    drill = asyncio.run(run_drill())
    print(drill.report())
    return 0 if drill.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
