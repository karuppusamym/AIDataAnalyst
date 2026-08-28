from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol


class RuntimeStage(StrEnum):
    RECEIVED = "RECEIVED"
    AUTHORIZED = "AUTHORIZED"
    SCREENED = "SCREENED"
    RESOLVED = "RESOLVED"
    PLANNED = "PLANNED"
    GENERATED = "GENERATED"
    VALIDATED = "VALIDATED"
    COSTED = "COSTED"
    EXECUTED = "EXECUTED"
    EXPLAINED = "EXPLAINED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS: dict[RuntimeStage, frozenset[RuntimeStage]] = {
    RuntimeStage.RECEIVED: frozenset({RuntimeStage.AUTHORIZED, RuntimeStage.REJECTED}),
    RuntimeStage.AUTHORIZED: frozenset({RuntimeStage.SCREENED, RuntimeStage.REJECTED}),
    RuntimeStage.SCREENED: frozenset({RuntimeStage.RESOLVED, RuntimeStage.REJECTED}),
    RuntimeStage.RESOLVED: frozenset({RuntimeStage.PLANNED, RuntimeStage.REJECTED}),
    RuntimeStage.PLANNED: frozenset({RuntimeStage.GENERATED, RuntimeStage.REJECTED}),
    RuntimeStage.GENERATED: frozenset({RuntimeStage.VALIDATED, RuntimeStage.REJECTED}),
    RuntimeStage.VALIDATED: frozenset({RuntimeStage.COSTED, RuntimeStage.REJECTED}),
    RuntimeStage.COSTED: frozenset({RuntimeStage.EXECUTED, RuntimeStage.REJECTED}),
    RuntimeStage.EXECUTED: frozenset({RuntimeStage.EXPLAINED, RuntimeStage.FAILED}),
    RuntimeStage.EXPLAINED: frozenset({RuntimeStage.COMPLETED, RuntimeStage.FAILED}),
    RuntimeStage.COMPLETED: frozenset(),
    RuntimeStage.REJECTED: frozenset(),
    RuntimeStage.FAILED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class RuntimeState:
    request_id: str
    stage: RuntimeStage = RuntimeStage.RECEIVED
    semantic_version: str | None = None
    policy_version: str | None = None
    logical_plan: dict[str, Any] | None = None
    generated_sql: str | None = None
    failure_reason: str | None = None
    step_count: int = 0

    def transition(self, target: RuntimeStage, **updates: Any) -> "RuntimeState":
        if target not in ALLOWED_TRANSITIONS[self.stage]:
            raise ValueError(f"invalid runtime transition: {self.stage} -> {target}")
        return replace(self, stage=target, step_count=self.step_count + 1, **updates)


class ModelGateway(Protocol):
    async def structured_completion(
        self,
        *,
        route: str,
        system_instruction: str,
        payload: dict[str, Any],
        output_schema: type[Any],
    ) -> Any: ...


class DisabledModelGateway:
    async def structured_completion(
        self,
        *,
        route: str,
        system_instruction: str,
        payload: dict[str, Any],
        output_schema: type[Any],
    ) -> Any:
        del route, system_instruction, payload, output_schema
        raise RuntimeError("no policy-approved model route is configured")
