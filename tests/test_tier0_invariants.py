"""Tier 0 invariant tests (`Docs/40-engineering/04-testing-strategy.md` §2,
`Docs/10-architecture/01-principles-and-invariants.md`) -- the nine binding
properties that must hold in every state of the system, formalized as one
suite (tracker item ST-03).

**Coverage in this file: INV-2, INV-3, INV-4, INV-8**, plus the workspace-level
half of INV-5 (`test_cross_tenant_denial` and
`test_authorization_defaults_to_deny_without_membership`), which drives the
policy engine against a real in-memory SQLite database.

**The other five live in their own modules**, because each needed a harness
rather than a handful of assertions and one 1,500-line file would be read by
nobody:

- `tests/test_inv1_single_authoritative_store.py` -- INV-1, single authoritative
  store. Every Cypher statement in `src/aida` is extracted and classified, so a
  projection write outside the projector package fails immediately; the rebuild
  property is proven as replay determinism against a fixed PostgreSQL fixture.
- `tests/test_inv5_tenant_isolation.py` -- INV-5, tenant isolation, at the API
  surface. Every route on the live FastAPI app is enumerated; all 44
  organization-scoped ones are driven with a foreign tenant and a session that
  raises on first use, and the remaining 155 are required to reach a boundary
  check. Complements, rather than replaces, the workspace-authorization tests in
  this file: those prove one entry point deeply against a real database, that
  module proves the whole surface.
- `tests/test_inv6_value_freedom.py` -- INV-6, value-freedom. The query gateway
  is driven end-to-end against a fake source returning sentinel-laden rows, and
  everything it persists is searched for them.
- `tests/test_inv7_attributability.py` -- INV-7, attributability. The mutating
  route set is derived from HTTP verb *and* call graph, and each must reach
  `record_audit`.
- `tests/test_inv9_capability_honesty.py` -- INV-9, honest capability reporting.
  Driven over the connector registry rather than a list of connectors.

**What is still not proven, stated once so it is not implied away.** Two strict
xfails remain, and both name a gap in the *codebase*, not in this suite:
eleven-plus endpoints commit governed state with no audit record (INV-7), and
capability flags are hand-declared rather than derived from a certification
result (INV-9). Both are recorded in
`Docs/review-2026-08/gap/06-tier0-invariant-suite.md`. Separately, INV-1's live
projection-rebuild drill (gap item E5) and INV-6's full ingestion-pipeline
sentinel sweep still need infrastructure that does not exist here; the tests
above prove the in-process half of each and say so in their own docstrings.
"""

import ast
import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.config import Settings
from aida.connectors.base import Connector
from aida.db import Base
from aida.model_gateway import SqlGenerationOutput
from aida.models import GovernanceReview, Organization
from aida.query_gateway import QueryExecutionGateway
from aida.schemas import GovernanceDecisionRequest
from aida.security import SecurityContext
from aida.semantic_api import decide_governance_review
from aida.semantic_inference import SemanticEnrichmentBatchOutput
from aida.workspace_service import authorize, create_workspace

# --- INV-2: one execution choke point ----------------------------------

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "aida"
# Both members of the SQL-accepting surface (`aida.connectors.sql_execution.SqlExecutor`).
# `estimate_read_query` takes a caller-supplied statement just as `execute_read_query`
# does, so a bypass through it reaches the source exactly the same way.
_CONNECTOR_EXECUTION_METHODS = frozenset({"execute_read_query", "estimate_read_query"})
_GATEWAY_MODULE = "query_gateway.py"


def _files_calling_connector_execution_outside_the_gateway() -> list[str]:
    offenders = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(_SRC_ROOT)
        parts = relative.parts
        if parts == (_GATEWAY_MODULE,):
            continue
        if parts[0] == "connectors":
            # The connectors package defines these methods; that's not a call.
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _CONNECTOR_EXECUTION_METHODS:
                offenders.append(str(relative))
                break
    return offenders


def test_no_connector_execution_outside_gateway() -> None:
    """INV-2: no SQL statement reaches a data source except through the Query
    Execution Gateway. Statically scans every module under `src/aida` for a call
    to either member of the connectors' SQL-accepting surface; the only permitted
    caller is `query_gateway.py` itself.

    This is the third of three layers enforcing INV-2, and the only one that can
    see a dynamic bypass. The other two are structural: `ConnectorRegistry.create`
    returns `Connector`, which has no SQL-accepting member (so `mypy --strict`
    rejects the call), and the import-linter contract "INV-2 connector SQL
    execution is reachable only from the query gateway" forbids any module but the
    gateway from importing `aida.connectors.execution_access`, the sole source of a
    `SqlExecutor`.
    """
    offenders = _files_calling_connector_execution_outside_the_gateway()
    assert offenders == [], (
        f"{sorted(_CONNECTOR_EXECUTION_METHODS)} must only be called from "
        f"{_GATEWAY_MODULE}, found callers in: {offenders}"
    )


# The driver entry points a module would have to call to reach a source without
# going through a `SqlExecutor` at all. `policy_native_sync` did exactly this
# (review 2026-09-11, defect D1): it imported `asyncpg`/`pytds` directly and
# executed DDL on a customer database, so it called neither method above, imported
# nothing the import-linter contract protects, and passed `mypy --strict` -- all
# three INV-2 layers, clean, while holding an open connection to a source.
_DRIVER_CONNECT_CALLS = frozenset(
    {
        "connect",  # asyncpg.connect / pytds.connect / snowflake.connector.connect
        "connect_async",  # oracledb.connect_async
    }
)
_DRIVER_MODULES = frozenset(
    {"asyncpg", "pytds", "oracledb", "snowflake", "databricks", "databricks_sql", "dbsql"}
)


def _files_opening_a_driver_connection_outside_connectors() -> list[str]:
    offenders = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(_SRC_ROOT)
        if relative.parts[0] == "connectors":
            # The connectors package is where a source connection is supposed to
            # be opened; the gateway is what decides whether a statement may use one.
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _DRIVER_CONNECT_CALLS:
                continue
            root = node.func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in _DRIVER_MODULES:
                offenders.append(f"{relative}:{node.lineno}")
    return offenders


def test_no_module_outside_connectors_opens_its_own_source_connection() -> None:
    """INV-2's fourth layer: a module cannot reach a source by importing a driver.

    The three existing layers all watch the `SqlExecutor` surface. None of them
    can see a module that skips that surface entirely and dials the database
    itself, which is how a second execution path lived in `policy_native_sync`
    until the 2026-09-11 review found it. Opening the connection is the step
    worth forbidding: everything after it is unreviewable by construction.

    A module that genuinely needs to talk to a source belongs in
    `aida.connectors`, behind the registry, and its statements belong to the
    gateway. Adding a name here is a change to the platform's central
    invariant; it needs an ADR, not a pull request.
    """
    offenders = _files_opening_a_driver_connection_outside_connectors()
    assert offenders == [], (
        "only aida.connectors may open a connection to a data source, found: " f"{offenders}"
    )


# The raw signing/tokenization secrets. Each is read by exactly one provider
# factory; every other reader is a module holding key material the deployment's
# KMS configuration was meant to keep out of the process.
_RAW_KEY_SETTINGS = {
    "audit_hmac_key": "signing.py",
    "tokenization_key": "tokenization.py",
}


def _files_reading_a_raw_key_outside_its_provider() -> list[str]:
    # Both source roots, unlike the INV-2 scans above: a key read from
    # `src/atlas` leaks exactly as much as one read from `src/aida`, and the
    # settings object these live on is defined over there. `config.py` is the
    # one legitimate reader -- it validates the values it declares.
    offenders = []
    roots = [_SRC_ROOT, _SRC_ROOT.parent / "atlas"]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(root.parent)
            if relative.name in {"config.py"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or node.attr not in _RAW_KEY_SETTINGS:
                    continue
                if relative.name == _RAW_KEY_SETTINGS[node.attr]:
                    continue
                offenders.append(f"{relative}:{node.lineno} reads {node.attr}")
    return offenders


def test_raw_signing_keys_are_read_only_by_their_provider_factory() -> None:
    """QG-5/QG-6: a KMS-backed deployment must not hold key material.

    `signing.py` promises the raw key never enters the process once a KMS signer
    is configured, and production forbids the local providers outright. Until the
    2026-09-11 review that promise was false in four places: `agent_orchestrator`
    (twice), `tool_api` and `intelligence_api` each passed
    `settings.audit_hmac_key` straight to `hmac.new` for an agent run's question
    digest, a tool execution's parameter fingerprint and a feedback comment. All
    four now go through `signing.sign_value`.

    Nothing structural stopped that, which is why it happened at four sites
    rather than one. This is the structural part: the raw secrets may be read
    only by the provider factory that owns each one, so the next such call is a
    failing test rather than a quiet leak of key material into a hot path.

    The production length floors in `Settings` stay as they are. They are cheap
    insurance for exactly the regression this test now prevents, and removing
    them because "production cannot read the key anyway" would rest on the very
    invariant that had just been violated.
    """
    offenders = _files_reading_a_raw_key_outside_its_provider()
    assert offenders == [], (
        "raw signing/tokenization keys may only be read by their provider factory "
        f"({', '.join(sorted(_RAW_KEY_SETTINGS.values()))}); found: {offenders}"
    )


def test_the_connector_handed_to_the_platform_has_no_sql_surface() -> None:
    """INV-2, structurally: the type `ConnectorRegistry.create` is annotated to
    return must not expose a SQL-accepting method, because that annotation is what
    makes a bypass a type error everywhere else in the codebase.

    If someone moves `execute_read_query` back onto `Connector`, the import contract
    and the AST scan above both still pass while the type-level guarantee silently
    disappears. This test is what notices.
    """
    from aida.connectors.registry import ConnectorRegistry
    from aida.connectors.sql_execution import SqlExecutor

    returned = inspect.signature(ConnectorRegistry.create).return_annotation
    assert returned is Connector, (
        f"ConnectorRegistry.create must stay annotated as returning Connector, got {returned!r}"
    )
    for method in _CONNECTOR_EXECUTION_METHODS:
        assert not hasattr(Connector, method), (
            f"Connector must not expose {method}; it belongs on SqlExecutor"
        )
        assert hasattr(SqlExecutor, method), f"SqlExecutor must expose {method}"


# --- INV-3: model output is never authority -----------------------------


def test_model_output_types_are_inert() -> None:
    """INV-3: LLM output is untrusted input and can never directly execute a
    query. Every structured model-output type is a plain, validated Pydantic
    model with no execute/run/call surface and no relationship to the
    connector or gateway classes -- and the one place generated SQL is ever
    executed (`QueryExecutionGateway.execute`) accepts a plain `sql: str`,
    never one of these proposal objects, so there is no conversion function
    from "unvalidated model output" to "executed query."
    """
    proposal_types: list[type[BaseModel]] = [SqlGenerationOutput, SemanticEnrichmentBatchOutput]
    executable_surface = {"execute", "execute_read_query", "run", "__call__"}

    for proposal_type in proposal_types:
        assert issubclass(proposal_type, BaseModel)
        assert not issubclass(proposal_type, Connector)
        assert not issubclass(proposal_type, QueryExecutionGateway)
        assert executable_surface.isdisjoint(vars(proposal_type))

    execute_params = inspect.signature(QueryExecutionGateway.execute).parameters
    assert execute_params["sql"].annotation is str
    for proposal_type in proposal_types:
        assert execute_params["sql"].annotation is not proposal_type


# --- INV-4: fail closed --------------------------------------------------

_SECURE_PRODUCTION_BASELINE: dict[str, Any] = {
    "environment": "production",
    "identity_provider": "oidc",
    "oidc_issuer": "https://identity.bank.example",
    "oidc_audience": "atlas",
    "oidc_jwks_url": "https://identity.bank.example/.well-known/jwks.json",
    "credential_provider": "vault",
    "allow_development_sql_override": False,
    "audit_hmac_key": "a" * 32,
    "hmac_signing_provider": "vault_transit",
    # QG-6: same shape as `hmac_signing_provider` above -- a secure production
    # baseline configures the KMS-backed tokenization provider, not the local
    # application-managed one.
    "tokenization_key": "b" * 32,
    "tokenization_provider": "vault_transform",
    "openai_base_url": "https://openai.internal",
    "gemini_base_url": "https://gemini.internal",
    "default_query_row_limit": 100,
    "hard_query_row_limit": 1000,
    "_env_file": None,
}

_INCOMPLETE_POSTURE_CASES: list[tuple[str, dict[str, Any], str]] = [
    (
        "development identity provider in production",
        {"identity_provider": "development"},
        "development identity provider is forbidden",
    ),
    (
        "OIDC identity provider missing issuer and audience",
        {"oidc_issuer": None, "oidc_audience": None},
        "OIDC issuer and audience are required",
    ),
    (
        "OIDC identity provider missing both JWKS URL and pinned JWKS JSON",
        {"oidc_jwks_url": None, "oidc_jwks_json": None},
        "OIDC JWKS URL or pinned JWKS JSON is required",
    ),
    (
        "production OIDC JWKS URL not served over HTTPS",
        {"oidc_jwks_url": "http://identity.bank.example/jwks.json"},
        "production OIDC JWKS URL must use HTTPS",
    ),
    (
        "environment-variable secret provider in production",
        {"credential_provider": "env"},
        "environment secret provider is forbidden",
    ),
    (
        "default query row limit exceeds the hard limit",
        {"default_query_row_limit": 5000, "hard_query_row_limit": 1000},
        "default query row limit cannot exceed the hard limit",
    ),
    (
        "development SQL override enabled in production",
        {"allow_development_sql_override": True},
        "development SQL override is forbidden",
    ),
    (
        "model generation enabled without an approved route",
        {"model_generation_enabled": True, "model_route": None},
        "model generation requires an explicit approved route",
    ),
    (
        "production model provider URL not served over HTTPS",
        {"openai_base_url": "http://model-proxy.internal"},
        "production model provider URLs must use HTTPS",
    ),
    (
        "production private model endpoint URL not served over HTTPS (MG-3)",
        {"model_endpoint_urls": {"vnet-openai": "http://model-proxy.internal"}},
        "production private model endpoint URLs must use HTTPS",
    ),
    (
        "production audit HMAC key shorter than 32 characters",
        {"audit_hmac_key": "too-short"},
        "production audit HMAC key must contain at least 32 characters",
    ),
    (
        "application-managed local HMAC signer in production (QG-5)",
        {"hmac_signing_provider": "local"},
        "application-managed local HMAC signer is forbidden",
    ),
    (
        "application-managed local tokenization provider in production (QG-6)",
        {"tokenization_provider": "local"},
        "application-managed local tokenization provider is forbidden",
    ),
    (
        "production tokenization key shorter than 32 characters",
        {"tokenization_key": "too-short"},
        "production tokenization key must contain at least 32 characters",
    ),
]


@pytest.mark.parametrize(
    ("case_name", "overrides", "expected_message"),
    _INCOMPLETE_POSTURE_CASES,
    ids=[case[0] for case in _INCOMPLETE_POSTURE_CASES],
)
def test_production_config_fail_closed(
    case_name: str, overrides: dict[str, Any], expected_message: str
) -> None:
    """INV-4: an incomplete or insecure security posture produces a denial at
    startup, never a degraded success. Parameterized over every rejection
    branch in `Settings.reject_insecure_production_configuration` -- each
    case starts from a fully secure production baseline and flips exactly the
    one field that makes it unsafe.
    """
    config = {**_SECURE_PRODUCTION_BASELINE, **overrides}

    with pytest.raises(ValidationError, match=expected_message):
        Settings(**config)


def test_the_secure_production_baseline_itself_is_accepted() -> None:
    """Sanity check for the fixture above: the unmodified baseline must be
    valid, or every parameterized rejection case would be meaningless (it
    would be impossible to tell a real rejection from a baseline that was
    already broken).
    """
    Settings(**_SECURE_PRODUCTION_BASELINE)


# --- INV-5: tenant isolation is total -------------------------------------


@pytest_asyncio.fixture
async def isolated_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def test_cross_tenant_denial(isolated_session: AsyncSession) -> None:
    """INV-5: authorization defaults to deny and never crosses an organization.

    Exercised against the single workspace authorization entry point with a real
    database. A principal carrying a valid identity in organization B is refused a
    workspace in organization A even though that workspace exists, and the refusal
    is shaped so that "exists elsewhere" and "does not exist" are indistinguishable
    from outside (`30-contracts/02-api-conventions.md`).
    """
    org_a = Organization(name="A", slug=f"a-{uuid4().hex[:8]}")
    org_b = Organization(name="B", slug=f"b-{uuid4().hex[:8]}")
    isolated_session.add_all([org_a, org_b])
    await isolated_session.flush()

    workspace = await create_workspace(
        isolated_session,
        organization_id=org_a.id,
        name="Risk",
        slug="risk",
        purpose="p",
        owner_principal="alice",
    )
    # alice is a real owner -- in organization A.
    intruder = SecurityContext(
        principal_id="alice",
        principal_type="USER",
        organization_id=org_b.id,
        roles=frozenset({"PlatformAdmin"}),
    )
    result = await authorize(
        isolated_session,
        intruder,
        workspace_id=workspace.id,
        action="READ_METADATA",
        resource_type="TABLE",
    )
    assert result.allowed is False
    assert result.reason_code == "CROSS_ORGANIZATION_DENIED"


async def test_authorization_defaults_to_deny_without_membership(
    isolated_session: AsyncSession,
) -> None:
    """INV-5: access is granted, never inherited. Being in the right organization
    is not by itself permission to enter a workspace inside it."""
    org = Organization(name="A", slug=f"a-{uuid4().hex[:8]}")
    isolated_session.add(org)
    await isolated_session.flush()
    workspace = await create_workspace(
        isolated_session,
        organization_id=org.id,
        name="Risk",
        slug="risk",
        purpose="p",
        owner_principal="alice",
    )
    outsider = SecurityContext(
        principal_id="mallory",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"PlatformAdmin"}),
    )
    result = await authorize(
        isolated_session,
        outsider,
        workspace_id=workspace.id,
        action="READ_METADATA",
        resource_type="TABLE",
    )
    assert result.allowed is False
    assert result.reason_code == "NO_WORKSPACE_MEMBERSHIP"


# --- INV-8: maker != checker ----------------------------------------------


class _SelfApprovalSession:
    """Only `.get()` is ever reached before the self-approval check fires --
    the maker-checker guard runs before `decide_governance_review` looks at
    `review.object_type` at all, so this fake never needs a second object.
    """

    def __init__(self, review: GovernanceReview) -> None:
        self._review = review

    async def get(self, _model: type[object], _identity: object) -> GovernanceReview:
        return self._review

    async def scalar(self, _statement: object) -> GovernanceReview:
        return self._review


_GOVERNED_OBJECT_TYPES = [
    "SEMANTIC_MODEL_VERSION",
    "GOVERNED_TOOL_VERSION",
    "MODEL_ROUTE_CONFIGURATION",
    "CONTEXT_PRODUCT_VERSION",
    "METADATA_ENRICHMENT_PROPOSAL",
    "GLOSSARY_TERM_VERSION",
    "ASSET_DOCUMENTATION_VERSION",
    "BULK_STEWARDSHIP_OPERATION",
    "GLOSSARY_CONFLICT",
    "GLOSSARY_LINK_PROPOSAL",
]


@pytest.mark.parametrize("object_type", _GOVERNED_OBJECT_TYPES)
async def test_self_approval_denied(object_type: str) -> None:
    """INV-8: the identity that proposes a governed change can never be the
    identity that approves it, for any object type. Exercised against every
    governed object type `decide_governance_review` handles.
    """
    organization_id = uuid4()
    review = GovernanceReview(
        id=uuid4(),
        organization_id=organization_id,
        object_type=object_type,
        object_id=str(uuid4()),
        requested_action="APPROVE",
        status="PENDING",
        requested_by="maker",
    )
    session = _SelfApprovalSession(review)
    same_principal_context = SecurityContext(
        principal_id="maker",  # identical to review.requested_by
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )

    with pytest.raises(HTTPException) as denied:
        await decide_governance_review(
            review.id,
            GovernanceDecisionRequest(decision="APPROVE"),
            same_principal_context,
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 409
    assert "maker-checker" in denied.value.detail
