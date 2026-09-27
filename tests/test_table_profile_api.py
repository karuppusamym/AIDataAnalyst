"""R11-FP04: `GET /v1/tables/{table_id}/profile`, end to end.

`Docs/review-2026-09-16/REVIEW.md` F06.2 asks for the value-free aggregate half
of FP-04 to ship with its read surface, and FP-04's own acceptance (module 05
§16.3) is that "Catalog/Quality show statistical evidence **and sampling
limitations**".

Until now this route's only coverage was the static gate scan in
`tests/test_inv4_authorization_wiring.py` ("`get_latest_table_profile` reaches
`gate`") -- which says the handler *calls* an authorization function and nothing
about what it then serves. So the three properties that matter here had no test
at all: that the statistics arrive with the scope that qualifies them, that a
classification policy can withhold the new facets without withholding the
table, and that a withheld facet is marked rather than dropped.

Driven against in-memory SQLite by calling the handler directly, the same
approach `test_profiling_exception_policy.py` uses for this module's endpoints.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.api as api_module
import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.config import Settings
from aida.connectors.base import (
    FACET_REASON_NOT_IMPLEMENTED,
    FACET_UNSUPPORTED,
    LENGTH_BUCKET_SCHEME,
    OBSERVATION_SCOPE_SAMPLE,
    PROFILE_FACET_ENTROPY,
    PROFILE_FACET_PATTERN_CLASS,
    PROFILE_FACET_UNITS,
)
from aida.db import Base
from aida.models import (
    AccessPolicy,
    AnalysisRun,
    ColumnProfile,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    TableProfile,
    Workspace,
)
from aida.security_types import SecurityContext
from aida.workspace_service import approve_binding, create_workspace, request_binding
from atlas.modules.profiling.facets import (
    CARDINALITY_HIGH_SELECTIVITY,
    WITHHELD_BY_POLICY,
)

_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    # StaticPool because the gate's workspace resolution and the handler share
    # one in-memory database; without it each connection would get a database of
    # its own and the seeded workspace would be invisible to the gate.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        active.info["maker"] = maker
        yield active
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    # ENFORCE workspaces are what this module tests, so the unresolved posture
    # never applies; naming it anyway keeps a change to the default from
    # silently altering what these tests exercise.
    return Settings(_env_file=None, unresolved_workspace_posture="SHADOW")


def _context(organization_id: UUID, principal: str = "alice") -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=organization_id,
        # Deliberately not PlatformAdmin: `enforce_organization` short-circuits
        # for that role, so a tenant-isolation assertion made with it would pass
        # for the wrong reason.
        roles=frozenset({"Analyst"}),
    )


async def _seed(
    session: AsyncSession,
    *,
    classifications: tuple[str, ...] = ("UNCLASSIFIED", "PII"),
    observation_scope: str | None = OBSERVATION_SCOPE_SAMPLE,
    with_facets: bool = True,
) -> tuple[Organization, MetadataTable, list[MetadataColumn]]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="primary",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://x",
        status="ACTIVE",
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="accounts",
        object_type="BASE_TABLE",
        fingerprint="fp",
    )
    run = AnalysisRun(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id, status="COMPLETED"
    )
    session.add_all([table, run])
    await session.flush()

    columns: list[MetadataColumn] = []
    for position, classification in enumerate(classifications, start=1):
        column = MetadataColumn(
            id=uuid4(),
            organization_id=org.id,
            table_id=table.id,
            name=f"col_{position}",
            ordinal_position=position,
            physical_type="text",
            nullable=True,
            classification=classification,
            fingerprint="fp",
        )
        columns.append(column)
    session.add_all(columns)

    profile = TableProfile(
        id=uuid4(),
        organization_id=org.id,
        analysis_run_id=run.id,
        datasource_id=datasource.id,
        table_id=table.id,
        row_count_estimate=5_000_000,
        sampled_row_count=1000,
        observation_scope=observation_scope,
        status="COMPLETED",
    )
    session.add(profile)
    await session.flush()

    facets: dict[str, Any] = (
        {
            "distinct_ratio": 0.5,
            "effectively_unique": False,
            "cardinality_class": CARDINALITY_HIGH_SELECTIVITY,
            "blank_count": 7,
            "whitespace_only_count": 2,
            "length_bucket_scheme": LENGTH_BUCKET_SCHEME,
            "length_bucket_counts": [100, 400, 400, 80, 10],
            "frequency_entropy_bits": 6.5,
            "unavailable_facets": [
                {
                    "facet": PROFILE_FACET_ENTROPY,
                    "status": FACET_UNSUPPORTED,
                    "reason_code": FACET_REASON_NOT_IMPLEMENTED,
                }
            ],
        }
        if with_facets
        else {}
    )
    for column in columns:
        session.add(
            ColumnProfile(
                id=uuid4(),
                organization_id=org.id,
                table_profile_id=profile.id,
                column_id=column.id,
                null_count=10,
                non_null_count=990,
                approximate_distinct_count=500,
                min_length=3,
                max_length=40,
                **facets,
            )
        )
    await session.commit()
    return org, table, columns


async def _enforcing_workspace_denying(
    session: AsyncSession,
    org: Organization,
    datasource_id: UUID,
    *,
    denied_classification: str | None,
) -> Workspace:
    """A resolvable ENFORCE workspace, optionally with a classification DENY.

    ENFORCE rather than SHADOW because a SHADOW workspace turns every denial
    into an allow by design (`workspace_access.apply_enforcement_mode`) -- a
    withholding test run in shadow mode would assert nothing.
    """
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="rbac-parity",
            name="RBAC parity",
            effect="ALLOW",
            subject_match={"roles": ["analyst", "workspace_owner"]},
            action_match=[],
            created_by="test",
        )
    )
    if denied_classification is not None:
        session.add(
            AccessPolicy(
                organization_id=org.id,
                code="no-pii-metadata",
                name="No PII profile facets",
                effect="DENY",
                priority=1000,
                resource_match={"classifications": [denied_classification]},
                action_match=["READ_METADATA"],
                created_by="test",
            )
        )
    await session.flush()
    workspace = await create_workspace(
        session,
        organization_id=org.id,
        name="W",
        slug=f"w-{uuid4().hex[:6]}",
        purpose="p",
        owner_principal="alice",
    )
    workspace.authorization_mode = "ENFORCE"
    binding = await request_binding(
        session,
        organization_id=org.id,
        workspace_id=workspace.id,
        datasource_id=datasource_id,
        purpose="p",
        requested_by="alice",
    )
    await approve_binding(session, binding, approver_principal="bob", now=_NOW)
    await session.commit()
    return workspace


async def _read(
    session: AsyncSession,
    settings: Settings,
    table: MetadataTable,
    org: Organization,
    **kwargs: Any,
) -> Any:
    return await api_module.get_latest_table_profile(
        table.id, _context(org.id, **kwargs), session, settings
    )


# --- the statistics, and the limitation that qualifies them -----------------


async def test_the_read_serves_every_value_free_facet(
    session: AsyncSession, settings: Settings
) -> None:
    org, table, _columns = await _seed(session)

    profile = await _read(session, settings, table, org)
    column = profile.columns[0]

    assert column.distinct_ratio == pytest.approx(0.5)
    assert column.effectively_unique is False
    assert column.cardinality_class == CARDINALITY_HIGH_SELECTIVITY
    assert column.blank_count == 7
    assert column.whitespace_only_count == 2
    assert column.length_bucket_counts == [100, 400, 400, 80, 10]
    assert column.length_bucket_scheme == LENGTH_BUCKET_SCHEME
    assert column.frequency_entropy_bits == pytest.approx(6.5)
    assert [status.facet for status in column.unavailable_facets] == [PROFILE_FACET_ENTROPY]


async def test_the_sampling_limitation_travels_with_the_statistics(
    session: AsyncSession, settings: Settings
) -> None:
    """FP-04's acceptance is one requirement, not two.

    A distinct ratio measured over a thousand of ten million rows means
    something much weaker than the same number measured over the table, so the
    scope has to be in the payload the numbers arrive in. A client that had to
    make a second call for it would render the numbers unqualified in the
    meantime, which is the failure mode being avoided.
    """
    org, table, _columns = await _seed(session, observation_scope=OBSERVATION_SCOPE_SAMPLE)

    profile = await _read(session, settings, table, org)

    assert profile.observation_scope == OBSERVATION_SCOPE_SAMPLE
    assert profile.sampled_row_count == 1000
    assert profile.row_count_estimate == 5_000_000


async def test_a_profile_written_before_the_facet_says_nothing_rather_than_claiming_full(
    session: AsyncSession, settings: Settings
) -> None:
    """NULL scope is a third state, not a synonym for UNKNOWN.

    Every profile row in an existing deployment has `observation_scope IS NULL`,
    and the temptation is to serve the old derivation
    (`sampled >= row_count_estimate`) in its place. That derivation is exactly
    what R11-FP04 replaced because it read a bounded BigQuery profile as a full
    scan -- so serving it here would have re-introduced the defect on the
    surface a human reads.
    """
    org, table, _columns = await _seed(session, observation_scope=None)

    profile = await _read(session, settings, table, org)

    assert profile.observation_scope is None


async def test_an_unprofiled_table_is_a_404_not_an_empty_profile(
    session: AsyncSession, settings: Settings
) -> None:
    """Most tables in a real estate have never been profiled. An empty profile
    would assert that the statistics were measured and came back empty.
    """
    org, table, _columns = await _seed(session)
    for profile in (await session.scalars(_select_profiles(table.id))).all():
        await session.delete(profile)
    await session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await _read(session, settings, table, org)
    assert excinfo.value.status_code == 404


def _select_profiles(table_id: UUID) -> Any:
    from sqlalchemy import select

    return select(TableProfile).where(TableProfile.table_id == table_id)


async def test_the_facets_atlas_does_not_compute_are_named_rather_than_omitted(
    session: AsyncSession, settings: Settings
) -> None:
    """FP-04 names units and pattern evidence; the value-free half ships
    neither. Saying so is the difference between a gap a reader can see and a
    gap they conclude does not exist -- and the two have different reasons:
    pattern evidence is unbuilt, whereas a data-inferred unit string could only
    be stated by carrying a value (ADR-0014), so no amount of building would
    put it here.
    """
    org, table, _columns = await _seed(session)

    profile = await _read(session, settings, table, org)
    by_facet = {status.facet: status for status in profile.uncomputed_facets}

    assert PROFILE_FACET_PATTERN_CLASS in by_facet
    assert PROFILE_FACET_UNITS in by_facet
    assert by_facet[PROFILE_FACET_PATTERN_CLASS].reason_code != (
        by_facet[PROFILE_FACET_UNITS].reason_code
    ), "an unbuilt facet and a structurally impossible one must not read the same"


# --- classification-aware withholding ---------------------------------------


async def test_a_classification_deny_withholds_that_columns_facets_with_a_marker(
    session: AsyncSession, settings: Settings
) -> None:
    """The acceptance: `classifications=` reaches the gate, and a withheld facet
    is a marker plus a count rather than a silent omission.

    The table-level `gate_read` this route has always called cannot carry a
    classification, so a MASK or DENY rule written against PII columns had
    nothing to act on -- the new facets would have gone out for every column or
    none. The per-classification decision is what gives the policy something to
    decide.
    """
    org, table, columns = await _seed(session, classifications=("UNCLASSIFIED", "PII"))
    await _enforcing_workspace_denying(
        session, org, table.datasource_id, denied_classification="PII"
    )

    profile = await _read(session, settings, table, org)
    by_name = {column.column_name: column for column in profile.columns}

    assert len(profile.columns) == 2, "a withheld column must still be listed"
    assert profile.withheld_column_count == 1

    withheld = by_name["col_2"]
    assert withheld.facets_withheld is True
    assert withheld.withheld_marker == WITHHELD_BY_POLICY
    assert withheld.withheld_reason_code == "DENIED_BY_POLICY"
    assert withheld.distinct_ratio is None
    assert withheld.length_bucket_counts is None
    assert withheld.frequency_entropy_bits is None
    assert withheld.unavailable_facets == [], (
        "the engine's own facet register describes measurement, not entitlement; "
        "mixing them would make a policy refusal read as an engine limitation"
    )

    served = by_name["col_1"]
    assert served.facets_withheld is False
    assert served.distinct_ratio == pytest.approx(0.5), (
        "withholding one classification must not withhold the table; FP-04 has to "
        "stay useful on exactly the tables that carry restricted columns"
    )
    assert columns  # the seeded columns are what the two names above refer to


async def test_the_withholding_test_would_notice_a_gate_that_never_denied(
    session: AsyncSession, settings: Settings
) -> None:
    """Negative control for the test above.

    Without it, a per-classification gate call that silently stopped happening
    -- or a workspace left in SHADOW, which turns every denial into an allow --
    would leave the assertions above passing against a payload that was never
    withheld in the first place, because `facets_withheld` defaults to False.
    """
    org, table, _columns = await _seed(session, classifications=("UNCLASSIFIED", "PII"))
    await _enforcing_workspace_denying(
        session, org, table.datasource_id, denied_classification=None
    )

    profile = await _read(session, settings, table, org)

    assert profile.withheld_column_count == 0
    assert all(column.distinct_ratio is not None for column in profile.columns), (
        "with no DENY policy the same read must serve every facet, or the "
        "withholding assertions prove nothing about the policy"
    )


async def test_the_withheld_column_still_carries_the_counts_it_always_did(
    session: AsyncSession, settings: Settings
) -> None:
    """A deliberate, stated boundary.

    The four counts predate this task and have always been served under this
    route's table-level gate. Narrowing them to nullable here would be a
    breaking response change (`scripts/openapi_diff.py`) that has nothing to do
    with gating the new egress -- so the classification decision governs the
    facets this task adds, and widening it to the counts is a separate,
    version-bumped change.
    """
    org, table, _columns = await _seed(session, classifications=("PII",))
    await _enforcing_workspace_denying(
        session, org, table.datasource_id, denied_classification="PII"
    )

    profile = await _read(session, settings, table, org)
    column = profile.columns[0]

    assert column.facets_withheld is True
    assert column.null_count == 10
    assert column.non_null_count == 990


# --- tenancy ----------------------------------------------------------------


async def test_a_table_in_another_organization_is_refused(
    session: AsyncSession, settings: Settings
) -> None:
    """INV-5, in the style of `tests/test_inv5_tenant_isolation.py`: the tenant
    is implied by the resource rather than named in the path, so the property is
    that the handler reaches `enforce_organization` before it reads anything.

    The refusal is a bare status with no profile in it, which is the part that
    matters for this route -- a partial payload would leak the shape of another
    tenant's table.
    """
    _org, table, _columns = await _seed(session)
    other = Organization(id=uuid4(), name="Other", slug=f"other-{uuid4().hex[:8]}")
    session.add(other)
    await session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await api_module.get_latest_table_profile(
            table.id, _context(other.id, principal="mallory"), session, settings
        )
    assert excinfo.value.status_code in {403, 404}
    assert "col_" not in str(excinfo.value.detail)


async def test_a_table_id_that_does_not_exist_is_a_404(
    session: AsyncSession, settings: Settings
) -> None:
    org, _table, _columns = await _seed(session)

    with pytest.raises(HTTPException) as excinfo:
        await api_module.get_latest_table_profile(
            uuid4(), _context(org.id), session, settings
        )
    assert excinfo.value.status_code == 404


async def test_the_profile_read_never_returns_a_source_value(
    session: AsyncSession, settings: Settings
) -> None:
    """ADR-0014 at the egress. The response model has no field that could hold a
    value -- which is the point -- so this asserts the rendered payload: every
    leaf is a number, a boolean, an identifier, a timestamp, or a word from a
    closed vocabulary this codebase defines.
    """
    org, table, _columns = await _seed(session)

    profile = await _read(session, settings, table, org)
    rendered = profile.model_dump(mode="json")

    permitted_strings = {
        LENGTH_BUCKET_SCHEME,
        CARDINALITY_HIGH_SELECTIVITY,
        OBSERVATION_SCOPE_SAMPLE,
        FACET_UNSUPPORTED,
        FACET_REASON_NOT_IMPLEMENTED,
        PROFILE_FACET_ENTROPY,
        PROFILE_FACET_PATTERN_CLASS,
        PROFILE_FACET_UNITS,
        "COMPLETED",
        "safe-v1",
        "UNCLASSIFIED",
        "PII",
        "NOT_APPLICABLE",
        "WOULD_CARRY_A_VALUE",
        "col_1",
        "col_2",
    }

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                _walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            # An identifier or a timestamp is not a business value; anything
            # else that is a string has to be on the vocabulary list above.
            if node in permitted_strings:
                return
            try:
                UUID(node)
                return
            except ValueError:
                pass
            try:
                datetime.fromisoformat(node)
                return
            except ValueError:
                pass
            raise AssertionError(f"unexplained string at {path}: {node!r}")

    _walk(rendered, "profile")
