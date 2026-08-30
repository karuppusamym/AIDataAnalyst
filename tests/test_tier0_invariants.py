"""Tier 0 invariant tests (`Docs/40-engineering/04-testing-strategy.md` §2,
`Docs/10-architecture/01-principles-and-invariants.md`) -- the nine binding
properties that must hold in every state of the system, formalized as one
suite (tracker item ST-03).

**Coverage in this file: INV-2, INV-3, INV-4, INV-8.** These four are provable
today with static analysis and direct function calls against fakes -- the same
no-live-infrastructure convention the rest of this suite already follows.

**Deliberately not attempted here, and why** (rather than faking a shallow
pass): INV-1 (`test_projection_rebuild`) and INV-6 (`test_no_source_values_in_control_plane`)
require deleting and replaying against a live Neo4j/search stack and a full
ingestion pipeline -- Tier 3/4 integration infrastructure that does not exist
in this environment. INV-5 (`test_cross_tenant_denial`) and INV-7
(`test_every_mutation_audits`) as specced require exercising *every* API
endpoint and background worker, which needs a running app plus a real or
much heavier per-route fake-session harness than a single test file -- a
distinct, larger undertaking from formalizing what's already provable. INV-9
(`test_capability_matrix_matches_certification`) needs a certification-result
store to assert against; nothing in the current codebase derives capability
flags from a certification run, so there is nothing yet to test. Closing
these five is real remaining work, not a gap this file quietly papers over.
"""

import ast
import inspect
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from aida.config import Settings
from aida.connectors.base import Connector
from aida.model_gateway import SqlGenerationOutput
from aida.models import GovernanceReview
from aida.query_gateway import QueryExecutionGateway
from aida.schemas import GovernanceDecisionRequest
from aida.security import SecurityContext
from aida.semantic_api import decide_governance_review
from aida.semantic_inference import SemanticEnrichmentBatchOutput

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
        "production audit HMAC key shorter than 32 characters",
        {"audit_hmac_key": "too-short"},
        "production audit HMAC key must contain at least 32 characters",
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
