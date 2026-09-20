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
over the live registry.

The enforcement clause is now implemented (tracker R11-C14): every advertised flag is
derived from a committed certification result
(`src/aida/connectors/capability_certification.json`, one row per connector and flag,
LIVE for PostgreSQL and SQL Server and FIXTURE for the other four), and a flag is
advertised only if the connector claims it *and* its row is CERTIFIED. The derivation
and its gates are proven in `tests/test_c14_capability_certification.py`.

What is *not* yet true is that every claimed flag is certified. A flag whose probe
genuinely failed is not lowered by a certification run -- lowering `explain` would make
the gateway refuse an engine, which is an operator's decision -- so it is listed in the
result's `uncertified_claims` and stays advertised.
`test_capability_flags_are_derived_from_certification` is therefore still a strict xfail,
narrowed to exactly those flags (`KNOWN_UNCERTIFIED_CLAIMS`), and it turns into a
normal passing test the day that list is empty.
"""

import json
from dataclasses import asdict
from dataclasses import fields as dataclass_fields
from uuid import uuid4

import pytest

from aida.config import Settings
from aida.connectors.base import ConnectorCapabilities
from aida.connectors.capability_certification import load_certification_result
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
    "databricks": json.dumps(
        {
            "server_hostname": "dbc-test.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/test123",
            "access_token": "dapi_test_token",
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
    assert len(_IMPLEMENTED) >= 6
    # CN-2b moved Databricks from `declare_planned` to a real pull adapter, so only
    # teradata and db2 remain planned; the tripwire tracks that, not a fixed count.
    assert len(_PLANNED) >= 2
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
        # INV-9: `definition.capabilities` is derived, so every flag it advertises is one the
        # connector claims AND either certified or explicitly held in `uncertified_claims`.
        result = load_certification_result()
        certification = result.connectors[definition.connector_type]
        held = result.held_flags(definition.connector_type)
        unbacked = []
        for flag, on in advertised.items():
            if not on:
                continue
            if not definition.claimed_capabilities[flag]:
                unbacked.append(flag)  # advertised without being claimed
            elif certification.flags[flag].status != "CERTIFIED" and flag not in held:
                unbacked.append(flag)  # claimed, but neither certified nor explicitly held
        assert unbacked == [], (
            f"{definition.connector_type} advertises {unbacked} with no certification behind it"
        )
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


# --- the enforcement clause: derived from certification, except the held claims --------

#: The flags that are advertised WITHOUT a CERTIFIED row, exactly, as `connector.flag`.
#:
#: Each one is a flag the connector claims whose probe genuinely did not certify it, and each
#: is held rather than lowered because lowering (`explain` above all) would refuse execution
#: for that engine -- a decision that belongs to the operator, not to a certification run.
#: This tuple is the narrowing of the strict xfail below: it names the only flags the
#: enforcement clause does not yet hold for, and `test_the_flags_advertised_without_a_
#: certification_are_exactly_the_known_ones` fails if the committed result disagrees with it in
#: either direction, so a new uncertified claim cannot hide behind the xfail.
#:
#: * `snowflake.partitions` -- claimed True since the adapter's first commit, but nothing in
#:   `snowflake.py` reads a partition (Snowflake's micro-partitions are not listed by any
#:   catalog view); only EXPLAIN's pruning counters mention them.
KNOWN_UNCERTIFIED_CLAIMS: tuple[str, ...] = ("snowflake.partitions",)


def _uncertified_claims() -> list[str]:
    claims = load_certification_result().uncertified_claims
    return sorted(f"{claim.connector_type}.{claim.flag}" for claim in claims)


def test_certification_evidence_still_only_covers_the_hierarchy_flags() -> None:
    """Pins what the *per-datasource* certification run reads, so a change to it is noticed.

    `connector_certification_evidence` (behind `POST /datasources/{id}/connector-certifications`)
    is a readiness suite for one registered datasource: credential reference, connection
    evidence, inventory. Its `hierarchy_contract` check reads only `catalogs` and `schemas`,
    and it still does. That is no longer the INV-9 gap: per-flag certification is the
    connector certification result (`test_c14_capability_certification.py`), and the
    `datasource.capabilities` this suite reads are copied from the derived flags.
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
    assert not {"explain", "constraints", "partitions"} & check_names


def test_every_advertised_flag_is_derived_from_a_certification_result() -> None:
    """The enforcement clause, structurally: no flag is advertised on the claim alone.

    For every implemented connector and every flag, the advertised value is exactly
    `claimed and (CERTIFIED or explicitly held)`. Recomputed here from the committed result
    rather than trusting the registry, so a registry that went back to reading the literal
    fails this test.
    """
    result = load_certification_result()
    for definition in _IMPLEMENTED:
        certification = result.connectors[definition.connector_type]
        held = result.held_flags(definition.connector_type)
        for flag in _CAPABILITY_FLAGS:
            claimed = definition.claimed_capabilities[flag]
            certified = certification.flags[flag].status == "CERTIFIED"
            expected = claimed and (certified or flag in held)
            assert definition.capabilities[flag] is expected, (
                f"{definition.connector_type}.{flag}: advertised "
                f"{definition.capabilities[flag]}, but claimed={claimed} certified={certified} "
                f"held={flag in held}"
            )


def test_the_flags_advertised_without_a_certification_are_exactly_the_known_ones() -> None:
    """Keeps the xfail below honest: it is narrowed to these flags and to no others.

    A strict xfail passes when *anything* in its body fails, so on its own it would also
    swallow a brand-new uncertified claim. This test does not: it compares the committed
    result with `KNOWN_UNCERTIFIED_CLAIMS` in both directions.
    """
    assert _uncertified_claims() == sorted(KNOWN_UNCERTIFIED_CLAIMS), (
        "the set of flags advertised without a certification changed; if that is intended, "
        "amend KNOWN_UNCERTIFIED_CLAIMS and tell the connector owner"
    )


@pytest.mark.xfail(
    condition=bool(KNOWN_UNCERTIFIED_CLAIMS),
    strict=True,
    reason=(
        "INV-9's enforcement clause -- 'capability flags are derived from the certification "
        "result, not hand-declared' -- is implemented: every advertised flag comes from "
        "capability_certification.json (aida.connectors.capability_certification), LIVE for "
        "PostgreSQL and SQL Server and FIXTURE for the other four, and no flag is advertised "
        "beyond its connector's claim. What is not yet true is that every claimed flag is "
        "CERTIFIED: " + ", ".join(KNOWN_UNCERTIFIED_CLAIMS) + " is held rather than lowered, "
        "because lowering a flag can refuse execution for an engine and that is the operator's "
        "decision. Strict xfail, narrowed to exactly those flags: it becomes a hard failure -- "
        "and KNOWN_UNCERTIFIED_CLAIMS must then be emptied -- the day the list is empty."
    ),
)
def test_capability_flags_are_derived_from_certification() -> None:
    """INV-9's enforcement clause in full: every claimed flag is certified.

    Expected to fail only for `KNOWN_UNCERTIFIED_CLAIMS`; see the xfail reason.
    """
    assert _uncertified_claims() == [], (
        "these advertised capability flags have no CERTIFIED row behind them, only an explicit "
        f"uncertified-claim entry: {_uncertified_claims()}"
    )
