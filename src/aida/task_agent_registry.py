"""ADR-0029: every task agent the platform runs, in one list.

The runtime (`aida.task_agent`) knows no agent by name, and each agent module
holds only its own spec and work. The surfaces that treat task agents as a
class -- the scheduler, the agent inbox, the roster -- read them from here, so
a fourth agent reaches all of them by one line, and a test fails if an agent's
principal setting exists without an entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from aida.lineage_agent import LINEAGE_AGENT, LINEAGE_WORK
from aida.quality_agent import QUALITY_AGENT, QUALITY_WORK
from aida.steward_agent import STEWARD_AGENT, STEWARD_WORK
from aida.task_agent import CapabilityWork, TaskAgentSpec
from atlas.platform.config import Settings


@dataclass(frozen=True, slots=True)
class RegisteredTaskAgent:
    spec: TaskAgentSpec
    work: Mapping[str, CapabilityWork]


TASK_AGENTS: Final[tuple[RegisteredTaskAgent, ...]] = (
    RegisteredTaskAgent(STEWARD_AGENT, STEWARD_WORK),
    RegisteredTaskAgent(LINEAGE_AGENT, LINEAGE_WORK),
    RegisteredTaskAgent(QUALITY_AGENT, QUALITY_WORK),
)


def task_agent_for_principal(
    settings: Settings, principal_id: str | None
) -> RegisteredTaskAgent | None:
    """The task agent configured with this workload identity, if any.

    An identity comparison against configuration -- the same one the runtime
    resolves authority by -- never a match on a name or a key.
    """
    if principal_id is None:
        return None
    for agent in TASK_AGENTS:
        if agent.spec.principal(settings) == principal_id.strip():
            return agent
    return None
