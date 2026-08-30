"""INV-9 -- honest capability reporting.

**Statement.** A connector, adapter, or feature advertises only behaviour that is
implemented and passing its certification suite. Planned capability is displayed as
planned.

**Enforcement (as written).** Capability flags are derived from the certification
result, not hand-declared.

**Why it is Tier 0.** Every downstream safety decision in the platform reads a
capability flag and believes it. The query gateway refuses to run a statement it
cannot cost, and it decides that by asking `connector.capabilities.explain`. A flag
that says `True` because someone typed `True` -- rather than because a certification
run proved it -- converts the platform's central cost control into a suggestion. The
same flag is what a bank's third-party risk assessment reads off the capability
matrix endpoint.

**What is proven here, and the one thing that is not.** The advertised/implemented
agreement, the planned-is-planned rule, and the load-bearing consequence (a
connector that cannot explain is refused execution) are all proven by enumeration
over the live registry. The enforcement clause itself is *not* satisfied by the
codebase: `ingestion.default_capabilities` returns `definition.capabilities`
verbatim -- the hand-declared dict -- and the certification suite
(`connector_certification_evidence`) checks only two of the nine capability flags.
`test_capability_flags_are_derived_from_certification` records that as a strict
xfail naming exactly what is missing, rather than letting the suite imply INV-9 is
enforced when only its observable half is.
"""

import json
from dataclasses import asdict
from dataclasses import fields as dataclass_fields
from uuid import uuid4

import pytest

from aida.config import Settings
from aida.connectors.base import ConnectorCapabilities
from aida.connectors.registry import ConnectorDefinition, connector_registry
from aida.connectors.sql_execution import SqlExecutor
from aida.ingestion import connector_certification_evidence, default_capabilities
from aida.models import DataSource
from aida.query_gateway import QueryExecutionGateway, QueryRejected
from tests.support.doubles import CatalogSession, FakeSqlExecutor, security_context

# A syntactically valid credential payload per connector, sufficient to construct
# the object. None of them opens a connection: `__init__` only parses. Written as
# data so that adding a connector to the registry fails here with "no test DSN"
# rather than silently dropping out of every test in this module.
_CONSTRUCTION_DSNS: dict[str, str] = {
    "postgres": "postgresql://user:pass@host:5432/db",
    "oracle": "oracle://user:pass@host:1521/service",
    "sqlserver": "mssql://user:pass@host:1433/db",
    "snowflake": "snowflake://user:pass@account/db/schema?warehouse=wh",
    "bigquery": json.dumps(
        {
            "auth_method": "workload_identity",
            "project_id": "atlas-test-project",
            "location": "europe-west2",
        }
    ),
}

_IMPLEMENTED = [
    definition
    for definition in connector_registry.definitions
    if definition.implementation_status == "IMPLEMENTED"
]
_PLANNED = [
    definition
    for definition in connector_registry.definitions
    if definition.implementation_status != "IMPLEMENTED"
]
_CAPABILITY_FLAGS = tuple(field.name for field in dataclass_fields(ConnectorCapabilities))


def _identifier(definition: ConnectorDefinition) -> str:
    return definition.connector_type


def test_the_registry_is_populated() -> None:
    """Tripwire: every test in this module is parameterized over the registry, so
    an empty registry would turn the whole file into a no-op that reports green.
    """
    assert len(_IMPLEMENTED) >= 5
    assert len(_PLANNED) >= 3
    assert len(_CAPABILITY_FLAGS) >= 8


def test_every_registered_connector_has_a_construction_dsn() -> None:
    """Guards the fixture above. A connector added to the registry without an
    entry here would be skipped by `test_advertised_capabilities_match_the_implementation`
    -- exactly the connector most likely to have a wrong flag.
    """
    missing = sorted(
        definition.connector_type
        for definition in _IMPLEMENTED
        if definition.connector_type not in _CONSTRUCTION_DSNS
    )
    assert missing == [], (
        f"these connectors have no test credential payload, so their advertised "
        f"capabilities are never checked against their implementation: {missing}"
    )


@pytest.mark.parametrize("definition", _IMPLEMENTED, ids=_identifier)
def test_advertised_capabilities_match_the_implementation(
    definition: ConnectorDefinition,
) -> None:
    """INV-9: what the registry advertises must equal what the connector reports.

    Constructs each registered connector and compares the registry's advertised
    capability dict against the object's own `capabilities`. Prevents the drift
    where a connector's `DEFAULT_CAPABILITIES` is tightened after a certification
    failure while the registry keeps advertising the old, more generous set --
    which is precisely how a capability claim outlives the behaviour behind it.
    """
    connector = connector_registry.create(
        definition.connector_type, _CONSTRUCTION_DSNS[definition.connector_type]
    )
    reported = asdict(connector.capabilities)

    assert definition.capabilities == reported, (
        f"{definition.connector_type} advertises {definition.capabilities} but the "
        f"implementation reports {reported}"
    )
    assert set(reported) == set(_CAPABILITY_FLAGS), (
        "the advertised capability dict does not cover every flag on "
        "ConnectorCapabilities; a missing key reads as 'absent', not 'false'"
    )


@pytest.mark.parametrize("definition", _IMPLEMENTED, ids=_identifier)
def test_an_implemented_connector_can_actually_execute(
    definition: ConnectorDefinition,
) -> None:
    """INV-9's "implemented" claim, taken literally: a connector the registry
    calls IMPLEMENTED must really provide the SQL-execution surface.

    Also the INV-2 half of the same fact -- `open_execution_session` fails closed
    on a connector that is not a `SqlExecutor`, so a registry entry that lied here
    would turn every query against that source into a 500 rather than a denial.
    """
    connector = connector_registry.create(
        definition.connector_type, _CONSTRUCTION_DSNS[definition.connector_type]
    )
    assert isinstance(connector, SqlExecutor), (
        f"{definition.connector_type} is advertised as IMPLEMENTED but does not "
        "implement the SQL execution surface"
    )


@pytest.mark.parametrize("definition", _PLANNED, ids=_identifier)
def test_planned_capability_is_displayed_as_planned(
    definition: ConnectorDefinition,
) -> None:
    """INV-9's second sentence: "Planned capability is displayed as planned."

    A planned connector must advertise no capabilities at all, must not claim a
    certification maturity, must not carry a release version, and must not be
    constructible. Prevents the roadmap-as-feature-list failure that this
    invariant exists to name -- the one a procurement questionnaire cannot
    detect and a customer discovers in production.
    """
    assert definition.capabilities == {}, (
        f"{definition.connector_type} is PLANNED but advertises capabilities: "
        f"{definition.capabilities}"
    )
    assert definition.maturity == "NOT_CERTIFIED"
    assert definition.version == "0.0.0"
    assert definition.connector_type not in connector_registry.supported_types, (
        f"{definition.connector_type} is PLANNED but can be instantiated"
    )
    with pytest.raises(ValueError, match="unsupported connector type"):
        connector_registry.create(definition.connector_type, "x://y")


@pytest.mark.parametrize(
    "definition", connector_registry.definitions, ids=_identifier
)
def test_the_capability_matrix_never_advertises_an_uncertified_capability(
    definition: ConnectorDefinition,
) -> None:
    """INV-9 at the surface a customer actually reads.

    `GET /v1/connectors/capability-matrix` renders `default_capabilities`, which
    must return `{}` for anything not IMPLEMENTED. Driven over every definition in
    the registry, planned and implemented alike, so the endpoint's honesty is a
    property of the registry rather than of the three connectors someone tested.
    """
    advertised = default_capabilities(definition)
    if definition.implementation_status == "IMPLEMENTED":
        assert advertised == definition.capabilities
    else:
        assert advertised == {}, (
            f"the capability matrix advertises {advertised} for the not-yet-"
            f"implemented connector {definition.connector_type}"
        )


# --- the load-bearing consequence of a capability flag ----------------------


def _costing_datasource(connector_type: str) -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="capability-probe",
        connector_type=connector_type,
        dialect="postgres",
        environment="TEST",
        credential_reference="vault://probe",
        status="ACTIVE",
    )


async def _run_gateway(
    monkeypatch: pytest.MonkeyPatch, capabilities: ConnectorCapabilities
) -> str:
    datasource = _costing_datasource("postgres")
    executor = FakeSqlExecutor(({"customer_id": 1},), capabilities=capabilities)
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session", lambda connector_type, dsn: executor
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type("_Resolver", (), {"resolve": staticmethod(lambda ref: "dsn://x")})(),
    )
    session = CatalogSession(
        tables=[("analytics_db", "analytics", "customers")],
        columns=[("analytics_db", "analytics", "customers", "customer_id")],
        sensitive_columns=[],
    )
    try:
        result = await QueryExecutionGateway(Settings(_env_file=None)).execute(
            session,
            datasource=datasource,
            context=security_context(organization_id=datasource.organization_id),
            correlation_id="corr-inv9",
            sql="SELECT customer_id FROM analytics.customers",
            requested_limit=10,
            semantic_version=None,
        )
        return result.execution.status
    except QueryRejected as rejected:
        return f"REJECTED: {rejected}"


async def test_a_connector_that_cannot_explain_is_refused_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-9's consequence, not just its declaration.

    `explain` is the one capability flag with teeth: the gateway will not run a
    statement it cannot cost first. Driving the real gateway with
    `explain=False` must produce a denial, which is what makes an honest `False`
    -- Oracle advertises exactly that today -- a safety property rather than a
    documentation detail.

    Prevents the change that treats a missing estimate as "cost unknown, proceed",
    which would also breach INV-4 (fail closed).
    """
    outcome = await _run_gateway(monkeypatch, ConnectorCapabilities(explain=False))
    assert outcome.startswith("REJECTED"), (
        f"a connector advertising explain=False was allowed to execute: {outcome}"
    )


async def test_a_connector_that_can_explain_is_allowed_to_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to the test above. Without it, a gateway that rejected every
    query for any reason would make the denial test pass while proving nothing
    about the capability flag.
    """
    outcome = await _run_gateway(monkeypatch, ConnectorCapabilities(explain=True))
    assert outcome == "COMPLETED", f"a costable query did not complete: {outcome}"


def test_at_least_one_registered_connector_honestly_declines_a_capability() -> None:
    """INV-9 is only meaningful if a `False` is ever actually written.

    A registry where every flag is `True` would satisfy every agreement test in
    this module while telling the customer nothing. Oracle's `explain=False` and
    the uniformly `False` `delegated_identity` flag are the evidence that the
    capability matrix is a report rather than a marketing surface -- and this test
    fails if the flags ever become uniformly optimistic.
    """
    declined = {
        (definition.connector_type, flag)
        for definition in _IMPLEMENTED
        for flag in _CAPABILITY_FLAGS
        if definition.capabilities.get(flag) is False
    }
    assert declined, "every implemented connector advertises every capability as True"


# --- the enforcement clause, which the codebase does not yet satisfy --------

# The capability flags `connector_certification_evidence` actually evaluates.
# `hierarchy_contract` checks `catalogs` and `schemas`; no other check reads a
# capability flag at all.
_CERTIFIED_CAPABILITY_FLAGS = frozenset({"catalogs", "schemas"})


def test_certification_evidence_still_only_covers_the_hierarchy_flags() -> None:
    """Pins the *size* of the INV-9 gap so the xfail below stays accurate.

    Runs the real certification suite against a fully-capable datasource and
    records which capability flags its checks read. If the suite grows a check for
    `explain` or `constraints`, this test fails and the xfail's stated reason must
    be rewritten -- which is the point: an honest gap statement has to be
    maintained, not written once.
    """
    datasource = _costing_datasource("postgres")
    datasource.capabilities = dict.fromkeys(_CAPABILITY_FLAGS, True)
    definition = connector_registry.definition("postgres")

    status, score, checks = connector_certification_evidence(
        datasource,
        definition,
        active_catalogs=1,
        active_tables=1,
    )
    check_names = {check["name"] for check in checks}

    assert status in {"CERTIFIED", "CONDITIONAL", "FAILED"}
    assert 0 <= score <= 100
    assert "hierarchy_contract" in check_names
    uncovered = sorted(set(_CAPABILITY_FLAGS) - _CERTIFIED_CAPABILITY_FLAGS)
    assert uncovered, "every capability flag is now certified; update the xfail below"
    for flag in ("explain", "constraints", "partitions", "delegated_identity"):
        assert flag in uncovered


@pytest.mark.xfail(
    strict=True,
    reason=(
        "INV-9's enforcement clause -- 'capability flags are derived from the "
        "certification result, not hand-declared' -- is not implemented. "
        "`ingestion.default_capabilities` returns the hand-written "
        "`ConnectorDefinition.capabilities` dict verbatim, and "
        "`connector_certification_evidence` evaluates only `catalogs` and "
        "`schemas` (via hierarchy_contract); `explain`, `constraints`, `indexes`, "
        "`partitions`, `query_history`, `delegated_identity` and "
        "`approximate_statistics` are never certified. Closing this needs the "
        "certification corpus in gap item E12 plus a derivation step under "
        "src/aida, neither of which this workstream owns. Strict xfail so it "
        "becomes a hard failure the day the derivation lands."
    ),
)
def test_capability_flags_are_derived_from_certification() -> None:
    """INV-9's enforcement clause in full. Currently fails; see the xfail reason."""
    uncertified = sorted(set(_CAPABILITY_FLAGS) - _CERTIFIED_CAPABILITY_FLAGS)
    assert uncertified == [], (
        f"these advertised capability flags are hand-declared, not derived from a "
        f"certification result: {uncertified}"
    )
