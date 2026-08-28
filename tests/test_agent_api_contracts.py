import asyncio
from uuid import uuid4

from aida.api import ai_runtime_status
from aida.config import Settings
from aida.main import app
from aida.schemas import AgentRunRead
from aida.security import SecurityContext


def test_ai_runtime_status_is_hybrid_and_fail_closed() -> None:
    context = SecurityContext(
        principal_id="contract-test",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"Viewer"}),
    )

    status = asyncio.run(
        ai_runtime_status(
            context=context,
            settings=Settings(environment="test", temporal_enabled=False),
        )
    )

    assert status.orchestration_mode == "HYBRID"
    assert status.model_route_status == "NOT_CONFIGURED"
    assert status.model_generation_enabled is False
    assert status.identity_provider == "DEVELOPMENT"
    assert status.credential_provider == "ENV"
    assert status.credential_provider_available is True
    assert status.enterprise_security_ready is False
    assert "sql_ast_validation" in status.deterministic_controls


def test_agent_history_contract_does_not_expose_question_digest() -> None:
    schema = AgentRunRead.model_json_schema()

    assert "question_hash" not in schema["properties"]
    assert "/v1/datasources/{datasource_id}/agent-runs" in app.openapi()["paths"]
    assert "/v1/ai/runtime-status" in app.openapi()["paths"]
