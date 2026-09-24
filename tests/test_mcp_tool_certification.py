"""R11-MP18: MCP offers and runs only certified tool versions where that is required.

Tool certification was recorded and reported, and gated nothing: an
uncertified PUBLISHED tool was listed by `tools/list` and callable through
`tools/call`. Where `Settings.mcp_requires_tool_certification` holds -- by
default in staging and production -- a version with no active certification is
neither offered nor run, and a call to one answers exactly like a call to a tool
that does not exist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.mcp_server import _handle_tools_call, _handle_tools_list
from aida.models import AuditEvent, GovernedToolVersion, ToolCertificationRun
from tests.test_tool_registry_ranking_and_impact import (  # noqa: F401
    _Scenario,
    db,
)

pytestmark = pytest.mark.asyncio

REQUIRED = Settings(mcp_tool_certification_required=True, _env_file=None)
NOT_REQUIRED = Settings(mcp_tool_certification_required=False, _env_file=None)


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:  # noqa: F811
    return await _Scenario(db).build()


async def _certify(
    scenario: _Scenario, version: GovernedToolVersion, *, expires_in: timedelta
) -> None:
    now = datetime.now(UTC)
    scenario.db.add(
        ToolCertificationRun(
            organization_id=scenario.organization.id,
            tool_id=version.tool_id,
            tool_version_id=version.id,
            suite_version="v1",
            corpus_fingerprint="f" * 64,
            status="CERTIFIED",
            total_cases=3,
            passed_cases=3,
            score=100,
            results=[],
            rationale="all cases passed",
            executed_by="certifier",
            certified_by="checker",
            issued_at=now,
            expires_at=now + expires_in,
        )
    )
    await scenario.db.flush()


async def _listed(scenario: _Scenario, settings: Settings) -> set[str]:
    result = await _handle_tools_list(scenario.db, scenario.analyst(), None, settings)
    return {t["name"] for t in result["tools"] if t["_atlas_meta"].get("tool_id")}


async def test_an_uncertified_tool_is_not_offered_where_certification_is_required(
    scenario: _Scenario,
) -> None:
    await scenario.tool_version(slug="uncertified", table="finance.customers")
    certified = await scenario.tool_version(slug="certified", table="finance.customers")
    await _certify(scenario, certified, expires_in=timedelta(days=30))

    assert await _listed(scenario, REQUIRED) == {"atlas__certified"}
    assert await _listed(scenario, NOT_REQUIRED) == {"atlas__certified", "atlas__uncertified"}


async def test_an_expired_certification_stops_counting(scenario: _Scenario) -> None:
    version = await scenario.tool_version(slug="expired", table="finance.customers")
    await _certify(scenario, version, expires_in=timedelta(days=-1))
    assert await _listed(scenario, REQUIRED) == set()


async def test_calling_an_uncertified_tool_answers_like_an_unknown_one_and_is_audited(
    scenario: _Scenario,
) -> None:
    await scenario.tool_version(slug="uncertified", table="finance.customers")
    arguments: dict[str, Any] = {"name": "atlas__uncertified", "arguments": {}}

    refused = await _handle_tools_call(arguments, scenario.db, scenario.analyst(), REQUIRED, "c1")
    unknown = await _handle_tools_call(
        {"name": "atlas__no-such-tool", "arguments": {}},
        scenario.db,
        scenario.analyst(),
        REQUIRED,
        "c2",
    )
    assert refused["isError"] is True
    assert refused["content"][0]["text"] == "Tool 'uncertified' not found or not published."
    assert unknown["content"][0]["text"] == "Tool 'no-such-tool' not found or not published."
    audits = (
        await scenario.db.scalars(
            select(AuditEvent).where(AuditEvent.action == "mcp.tool_call.certification_missing")
        )
    ).all()
    assert len(audits) == 1


def test_certification_is_required_by_default_only_in_staging_and_production() -> None:
    # `model_construct` because a real production Settings needs its whole
    # production configuration; only the property's rule is under test here.
    def required(**values: object) -> bool:
        return Settings.model_construct(**values).mcp_requires_tool_certification

    assert required(environment="production", mcp_tool_certification_required=None)
    assert required(environment="staging", mcp_tool_certification_required=None)
    assert not required(environment="development", mcp_tool_certification_required=None)
    assert not required(environment="production", mcp_tool_certification_required=False)
    assert required(environment="development", mcp_tool_certification_required=True)
