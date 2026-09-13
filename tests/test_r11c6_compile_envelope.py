"""R11-C6 finding 12: context-product compile honours the capability envelope.

`GET /context-product-versions/{id}/compile` and its `/download` read a
product's governed content by version id -- tables, negative knowledge,
exemplars -- and checked roles only. The version reads beside them gate on the
contracted agent's `capability_envelope.context_product_ids`, so an agent whose
envelope omitted a product could not read it but could still compile it. Same
product, a different door.

Found while correcting the enforcement matrix, which listed compile as the
remaining envelope-free context-product surface.

These drive the real route handlers through the harness the version-read
tests use, and prove the allow path *by position*: a patched quality
evaluation raises a sentinel, so reaching it means the envelope let the caller
through, and not reaching it means the refusal came first. "No exception"
could not tell allowed-through from never-reached.
"""

from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException

from aida import context_compiler_api
from aida.context_compiler_api import (
    compile_context_product_version,
    download_context_compilation,
)
from aida.security import SecurityContext
from tests.test_r11c6_rest_context_product_envelope import (
    NOT_FOUND,
    _agent,
    _audit_actions,
    _contract,
    _readable,
    _RestEnvelopeSession,
)


class _ReachedQualityGate(Exception):
    """Raised by the patched quality evaluation: the caller got past the
    envelope and every check above it."""


@pytest.fixture
def quality_tripwire(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    reached: list[bool] = []

    async def _tripwire(*_args: Any, **_kwargs: Any) -> Any:
        reached.append(True)
        raise _ReachedQualityGate

    monkeypatch.setattr(
        context_compiler_api, "evaluate_context_product_quality_from_db", _tripwire
    )
    return reached


def _session(product_key_allowed: str | None, org: UUID, version: object, product: object) -> Any:
    contracts: tuple[object, ...] = ()
    if product_key_allowed is not None:
        contracts = (
            _contract(
                organization_id=org,
                principal_id="agent:revenue-bot",
                products=[product_key_allowed],
            ),
        )
    return _RestEnvelopeSession(get_results=[version, product], contracts=contracts)


async def test_compile_refuses_a_product_the_envelope_omits(
    quality_tripwire: list[bool],
) -> None:
    """The defect. Before the gate this compiled the product for an agent
    that was refused a plain read of the same version."""
    version, product = _readable(allowed_roles=["DataSteward"])
    session = _session("some_other_product", version.organization_id, version, product)

    with pytest.raises(HTTPException) as excinfo:
        await compile_context_product_version(
            version.id, target="MCP", context=_agent(version.organization_id), session=session
        )

    assert (excinfo.value.status_code, excinfo.value.detail) == (404, NOT_FOUND)
    assert "context_product.read.envelope_denied" in _audit_actions(session)
    assert quality_tripwire == [], "the refusal came after the quality gate"


async def test_download_refuses_a_product_the_envelope_omits(
    quality_tripwire: list[bool],
) -> None:
    """Both compile routes load through `_load_source`, so one gate covers
    both -- asserted, not assumed."""
    version, product = _readable(allowed_roles=["DataSteward"])
    session = _session("some_other_product", version.organization_id, version, product)

    with pytest.raises(HTTPException) as excinfo:
        await download_context_compilation(
            version.id, target="YAML", context=_agent(version.organization_id), session=session
        )

    assert (excinfo.value.status_code, excinfo.value.detail) == (404, NOT_FOUND)
    assert quality_tripwire == []


async def test_compile_lets_an_agent_through_for_a_product_its_envelope_names(
    quality_tripwire: list[bool],
) -> None:
    version, product = _readable(allowed_roles=["DataSteward"])
    session = _session(product.product_key, version.organization_id, version, product)

    with pytest.raises(_ReachedQualityGate):
        await compile_context_product_version(
            version.id, target="MCP", context=_agent(version.organization_id), session=session
        )

    assert quality_tripwire == [True]


async def test_compile_is_unchanged_for_a_human(quality_tripwire: list[bool]) -> None:
    """A person holds no contract; the gate must be invisible to them."""
    version, product = _readable(allowed_roles=["DataSteward"])
    session = _session(None, version.organization_id, version, product)
    human = SecurityContext(
        principal_id="steward@bank.example",
        principal_type="USER",
        organization_id=version.organization_id,
        roles=frozenset({"DataSteward"}),
    )

    with pytest.raises(_ReachedQualityGate):
        await compile_context_product_version(
            version.id, target="MCP", context=human, session=session
        )

    assert quality_tripwire == [True]


async def test_an_agent_identity_with_no_contract_cannot_compile(
    quality_tripwire: list[bool],
) -> None:
    """An `AGENT` identity that resolves no contract is refused rather than
    served as an uncontracted human."""
    version, product = _readable(allowed_roles=["DataSteward"])
    session = _session(None, version.organization_id, version, product)

    with pytest.raises(HTTPException) as excinfo:
        await compile_context_product_version(
            version.id, target="MCP", context=_agent(version.organization_id), session=session
        )

    assert excinfo.value.status_code == 404
    assert quality_tripwire == []
