"""TL-5: the public Tool SDK (`sdk/aida_tool_sdk`).

Split into three groups:

  * Pure local validation/serialization -- no DB, no network. Proves the SDK
    genuinely reuses the server's real code (`aida.sql_guard.SqlGuard`,
    `aida.tool_rendering`, `aida.schemas.GovernedToolVersionCreate`) rather
    than a parallel copy of its rules.
  * The HTTP submission client, with `httpx.post` mocked -- proves it sends
    exactly the payload local serialization produces, never calls the
    network when local validation already failed, and surfaces server
    rejections as `ToolDraftSubmissionError`.
  * A governance-boundary test: nothing named `publish`/`approve`/`certify`/
    `execute` exists anywhere on the SDK's public surface.
  * One real-database integration test (mirrors the harness in
    `tests/test_tool_registry_ranking_and_impact.py`): the SDK's serialized
    payload is fed straight into the actual draft-submission endpoint
    (`aida.tool_api.create_tool_version`) against an in-memory SQLite
    database, proving the wire format really is what that endpoint expects
    -- and that the datasource-table-allowlist check this SDK cannot
    replicate locally is still enforced server-side.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from aida_tool_sdk import (
    ToolCandidate,
    ToolCandidateValidationError,
    ToolDraftClient,
    ToolDraftSubmissionError,
    candidate_to_draft_payload,
    candidate_to_wire_model,
    parameter,
    validate_candidate,
)
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.config import Settings
from aida.db import Base
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.tool_api import create_tool_version
from tests.support.doubles import security_context

SQL_TEMPLATE = (
    "SELECT customer_id, state_code FROM finance.customers "
    "WHERE state_code = :region AND customer_id >= :minimum_id"
)


def _candidate(**overrides: Any) -> ToolCandidate:
    defaults: dict[str, Any] = dict(
        slug="region_customer_lookup",
        name="Region customer lookup",
        description="Looks up customers by state for a governed downstream agent.",
        datasource_id=uuid4(),
        sql_template=SQL_TEMPLATE,
        dialect="postgres",
        allowed_roles=["Analyst"],
    )
    defaults.update(overrides)
    candidate = ToolCandidate(**defaults)
    if "parameters" not in overrides:
        candidate.add_parameter(
            parameter(name="region", parameter_type="STRING", allowed_values=["NY", "TX"])
        )
        candidate.add_parameter(
            parameter(name="minimum_id", parameter_type="INTEGER", minimum=1, default=1)
        )
    return candidate


# ---------------------------------------------------------------------------
# Local validation / serialization -- pure, no DB, no network.
# ---------------------------------------------------------------------------


def test_valid_candidate_passes_local_validation_and_dry_run_renders() -> None:
    candidate = _candidate().with_example_values(region="NY", minimum_id=5)

    result = validate_candidate(candidate)

    assert result.valid
    assert result.errors == []
    assert result.referenced_tables == ("finance.customers",)
    assert result.rendered_example_sql is not None
    assert "'NY'" in result.rendered_example_sql
    assert result.rendered_example_parameters == {"region": "NY", "minimum_id": 5}


def test_invalid_slug_is_caught_by_the_real_wire_schema() -> None:
    # `GovernedToolVersionCreate.slug` requires `^[a-z][a-z0-9_]{1,99}$` --
    # this exercises that real pydantic pattern, not an SDK-side copy of it.
    candidate = _candidate(slug="Not-A-Valid-Slug!")

    result = validate_candidate(candidate)

    assert not result.valid
    assert any("slug" in error for error in result.errors)

    with pytest.raises(ToolCandidateValidationError):
        candidate_to_wire_model(candidate)


def test_duplicate_parameter_names_are_caught_by_the_real_wire_schema() -> None:
    candidate = _candidate(
        sql_template=(
            "SELECT :region, :region_again FROM finance.customers WHERE state_code = :region"
        ),
        parameters=[
            parameter(name="region", parameter_type="STRING"),
            parameter(name="region", parameter_type="STRING"),
        ],
    )

    with pytest.raises(ToolCandidateValidationError) as excinfo:
        candidate_to_wire_model(candidate)
    assert any("unique" in error for error in excinfo.value.errors)


def test_sensitive_parameter_with_a_default_is_rejected_at_construction() -> None:
    # `ToolParameterDefinition.validate_bounds` (the real server model) forbids
    # a persisted default on a sensitive parameter -- `parameter(...)` *is*
    # that class, so this fails the moment it's constructed, before the
    # candidate even exists.
    with pytest.raises(Exception, match="sensitive"):
        parameter(name="ssn", parameter_type="STRING", sensitive=True, default="000-00-0000")


def test_undeclared_placeholder_is_caught_locally() -> None:
    candidate = _candidate(
        sql_template="SELECT customer_id FROM finance.customers WHERE state_code = :region "
        "AND signup_year >= :signup_year",
    )
    # `signup_year` is used in the template but only `region`/`minimum_id` are declared.

    result = validate_candidate(candidate)

    assert not result.valid
    assert any("signup_year" in error and "undeclared" in error for error in result.errors)


def test_unused_parameter_definition_is_caught_locally() -> None:
    candidate = _candidate(sql_template="SELECT customer_id FROM finance.customers")
    # Neither `region` nor `minimum_id` appears in the template at all.

    result = validate_candidate(candidate)

    assert not result.valid
    assert any("unused" in error for error in result.errors)


@pytest.mark.parametrize(
    ("sql", "expected_violation"),
    [
        ("DELETE FROM finance.customers", "READ_ONLY_QUERY_REQUIRED"),
        ("SELECT * FROM finance.customers", "SELECT_WILDCARD_FORBIDDEN"),
        (
            "SELECT customer_id FROM finance.customers a CROSS JOIN finance.accounts b",
            "CROSS_OR_UNBOUNDED_JOIN_FORBIDDEN",
        ),
    ],
)
def test_sql_guard_violations_are_caught_locally_via_the_real_guard(
    sql: str, expected_violation: str
) -> None:
    candidate = _candidate(sql_template=sql, parameters=[])

    result = validate_candidate(candidate)

    assert not result.valid
    assert any(expected_violation in error for error in result.errors)


def test_forbidden_function_is_caught_by_the_real_dialect_denylist() -> None:
    # `pg_sleep` is in `SqlGuard._forbidden_functions_by_dialect["postgres"]` --
    # a real network/session-control bypass the guard blocks regardless of
    # dialect, not something this SDK invents its own denylist for.
    candidate = _candidate(
        sql_template="SELECT pg_sleep(5) FROM finance.customers", parameters=[]
    )

    result = validate_candidate(candidate)

    assert not result.valid
    assert any("FORBIDDEN_FUNCTION:pg_sleep" in error for error in result.errors)


def test_example_value_type_mismatch_is_caught_by_the_real_renderer() -> None:
    candidate = _candidate().with_example_values(region="NY", minimum_id="not-an-integer")

    result = validate_candidate(candidate)

    assert not result.valid
    assert any("example rendering failed" in error for error in result.errors)


def test_validate_candidate_raise_on_error_raises_with_the_same_errors() -> None:
    candidate = _candidate(sql_template="SELECT customer_id FROM finance.customers")

    with pytest.raises(ToolCandidateValidationError) as excinfo:
        validate_candidate(candidate, raise_on_error=True)
    assert excinfo.value.errors  # non-empty, same errors a non-raising call would report


def test_candidate_to_draft_payload_matches_the_real_request_schema_exactly() -> None:
    from aida.schemas import GovernedToolVersionCreate

    candidate = _candidate()

    payload = candidate_to_draft_payload(candidate)

    assert set(payload) == set(GovernedToolVersionCreate.model_fields)
    # Round-trips cleanly through the real request model with no coercion
    # surprises -- this *is* what the endpoint will parse.
    rebuilt = GovernedToolVersionCreate.model_validate(payload)
    assert rebuilt.slug == candidate.slug
    assert rebuilt.sql_template == candidate.sql_template
    assert [p.name for p in rebuilt.parameters] == [p.name for p in candidate.parameters]
    assert rebuilt.allowed_roles == candidate.allowed_roles


# ---------------------------------------------------------------------------
# HTTP submission client -- `httpx.post` mocked, no live server.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict[str, Any], text: str = "") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or str(json_body)

    def json(self) -> dict[str, Any]:
        return self._json_body


def test_submit_draft_posts_the_exact_local_payload_and_returns_the_parsed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate()
    project_id = uuid4()
    expected_payload = candidate_to_draft_payload(candidate)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> _FakeResponse:
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _FakeResponse(201, {"id": str(uuid4()), "status": "DRAFT", "version": 1})

    monkeypatch.setattr("httpx.post", fake_post)

    fake_token = "secret-token"  # noqa: S105
    client = ToolDraftClient("https://aida.example.com", token=fake_token)
    response = client.submit_draft(project_id, candidate)

    assert response["status"] == "DRAFT"
    assert len(calls) == 1
    assert calls[0]["url"] == f"https://aida.example.com/v1/projects/{project_id}/tools"
    assert calls[0]["json"] == expected_payload
    assert calls[0]["headers"]["Authorization"] == "Bearer secret-token"


def test_submit_draft_raises_a_typed_error_on_server_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(
        url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> _FakeResponse:
        return _FakeResponse(
            422, {"detail": "unknown or unauthorized tool tables: finance.ghost"}
        )

    monkeypatch.setattr("httpx.post", fake_post)

    client = ToolDraftClient("https://aida.example.com")

    with pytest.raises(ToolDraftSubmissionError) as excinfo:
        client.submit_draft(uuid4(), _candidate())
    assert excinfo.value.status_code == 422
    assert "unauthorized tool tables" in excinfo.value.detail


def test_submit_draft_never_touches_the_network_when_local_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("httpx.post", lambda *a, **k: calls.append((a, k)))  # pragma: no cover

    client = ToolDraftClient("https://aida.example.com")
    invalid_candidate = _candidate(sql_template="SELECT customer_id FROM finance.customers")

    with pytest.raises(ToolCandidateValidationError):
        client.submit_draft(uuid4(), invalid_candidate)
    assert calls == []


# ---------------------------------------------------------------------------
# Governance boundary: no publish/approve/certify/execute surface, anywhere.
# ---------------------------------------------------------------------------


def test_sdk_has_no_publish_approve_certify_or_execute_surface() -> None:
    import aida_tool_sdk

    forbidden_substrings = ("publish", "approve", "certify", "execute")
    public_names = [name for name in vars(aida_tool_sdk) if not name.startswith("_")]
    for obj in [aida_tool_sdk, ToolCandidate, ToolDraftClient] + [
        getattr(aida_tool_sdk, name) for name in public_names
    ]:
        members = [member for member in dir(obj) if not member.startswith("_")]
        for member in members:
            lowered = member.lower()
            assert not any(word in lowered for word in forbidden_substrings), (
                f"{obj!r}.{member} looks like a publish/approve/certify/execute surface; "
                "this SDK must only ever be able to produce or submit a DRAFT"
            )


# ---------------------------------------------------------------------------
# Real-database integration: the SDK's payload against the real endpoint.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class _Scenario:
    """One organization/project/datasource with a single seeded table,
    `finance.customers` -- the table `SQL_TEMPLATE` above reads from.
    Mirrors the harness in `tests/test_tool_registry_ranking_and_impact.py`.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> "_Scenario":
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()

        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code="RETAIL"
        )
        db.add(self.lob)
        await db.flush()

        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Finance",
            code="FINANCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Banking",
            slug="core-banking",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name="core-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://core-warehouse",
        )
        db.add(self.datasource)
        await db.flush()

        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="bank",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="finance",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        self.customers_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="customers",
            object_type="TABLE",
            fingerprint="fp-customers",
        )
        db.add(self.customers_table)
        await db.flush()
        return self

    def maker(self) -> object:
        return security_context(
            organization_id=self.organization.id, roles=frozenset({"ToolDeveloper"})
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def test_locally_validated_candidate_is_accepted_as_a_draft_by_the_real_endpoint(
    scenario: _Scenario,
) -> None:
    candidate = _candidate(datasource_id=scenario.datasource.id)
    local_result = validate_candidate(candidate)
    assert local_result.valid, local_result.errors

    created = await create_tool_version(
        scenario.project.id,
        candidate_to_wire_model(candidate),
        context=scenario.maker(),
        session=scenario.db,
        settings=Settings(),
    )

    assert created.status == "DRAFT"
    assert created.slug == candidate.slug
    assert created.referenced_tables == ["finance.customers"]
    assert [p.name for p in created.parameters] == ["region", "minimum_id"]
    # Never published/approved by anything this SDK did.
    assert created.approved_by is None
    assert created.approved_at is None


async def test_server_still_rejects_a_table_outside_the_datasource_allowlist(
    scenario: _Scenario,
) -> None:
    # `finance.ghost_accounts` was never seeded onto the datasource's catalog
    # -- local validation has no way to know that (it requires live server
    # state), so it passes locally, and only the real endpoint catches it.
    candidate = _candidate(
        datasource_id=scenario.datasource.id,
        sql_template="SELECT customer_id FROM finance.ghost_accounts WHERE state_code = :region "
        "AND customer_id >= :minimum_id",
    )
    local_result = validate_candidate(candidate)
    assert local_result.valid, local_result.errors

    with pytest.raises(HTTPException) as excinfo:
        await create_tool_version(
            scenario.project.id,
            candidate_to_wire_model(candidate),
            context=scenario.maker(),
            session=scenario.db,
            settings=Settings(),
        )
    assert excinfo.value.status_code == 422
    assert "unauthorized" in str(excinfo.value.detail)
