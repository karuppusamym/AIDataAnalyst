"""R11-C14 / INV-9: capability flags are derived from the certification result.

`tests/test_inv9_capability_honesty.py` proved the *observable* half of INV-9 (advertised
matches implemented; a connector that cannot explain is refused execution) and recorded the
enforcement clause -- "capability flags are derived from the certification result, not
hand-declared" -- as a strict xfail. This file proves the derivation itself.

Nothing here needs a database. LIVE evidence is read from the committed result
(`src/aida/connectors/capability_certification.json`); only
`scripts/certify_connector_capabilities.py` produces it, against the sample containers.
What this file guards is everything that can go wrong *between* that run and the platform:

* the derivation can never advertise more than a connector claims, however the result reads;
* a failed probe is held and reported, never silently lowered, and a claim nobody listed fails
  closed;
* a connector's code cannot drift from its certification without a gate going red;
* a fixture result is never labelled live;
* the capability endpoint and the real query gateway see the derived flags, not the literal.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import asdict, replace
from itertools import product
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest

from aida.config import Settings
from aida.connectors import capability_certification as certification
from aida.connectors.base import ConnectorCapabilities, QueryEstimate, QueryResult
from aida.connectors.capability_certification import (
    CAPABILITY_FLAGS,
    STATUS_CERTIFIED,
    STATUS_NOT_APPLICABLE,
    STATUS_NOT_CERTIFIED,
    TIER_FIXTURE,
    TIER_LIVE,
    CertificationResult,
    ConnectorCertification,
    FlagCertification,
    UncertifiedClaim,
    compute_fingerprint,
    derive_capabilities,
    derive_flags,
    fingerprint_inputs,
    load_certification_result,
    render_markdown,
    stale_connectors,
    verify_result,
)
from aida.connectors.postgres import PostgresConnector
from aida.connectors.registry import ConnectorRegistry, connector_registry
from aida.models import DataSource
from aida.query_gateway import QueryExecutionGateway, QueryRejected
from atlas.modules.ingestion import router as ingestion_router
from tests.support.doubles import CatalogSession, security_context
from tests.test_inv9_capability_honesty import KNOWN_UNCERTIFIED_CLAIMS

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
_LIVE_MODULE = "tests/test_c14_live_capability_probes.py"
_IMPLEMENTED = sorted(
    d.connector_type
    for d in connector_registry.definitions
    if d.implementation_status == "IMPLEMENTED"
)

#: The claimed-but-uncertified flags, pinned once, next to the strict xfail they narrow, in
#: `tests/test_inv9_capability_honesty.py`. Changing that tuple is a reviewed edit, not a side
#: effect of a certification run: each entry stays advertised without a certification.
_EXPECTED_UNCERTIFIED_CLAIMS = {
    (item.split(".")[0], item.split(".")[1]) for item in KNOWN_UNCERTIFIED_CLAIMS
}
#: The exact set of (connector, flag) cells where the derived flag is *lower* than the claim.
#: Empty at landing on purpose: lowering `explain` refuses execution for that engine, which is an
#: operator's decision, so nothing may be lowered by a certification run alone.
_EXPECTED_LOWERED: set[tuple[str, str]] = set()


def _script() -> ModuleType:
    path = _REPO / "scripts" / "certify_connector_capabilities.py"
    spec = importlib.util.spec_from_file_location("c14_certify_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["c14_certify_script"] = module
    spec.loader.exec_module(module)
    return module


def _capabilities(**flags: bool) -> ConnectorCapabilities:
    return ConnectorCapabilities(**flags)


def _row(
    flag: str,
    *,
    claimed: bool,
    status: str = STATUS_CERTIFIED,
    tier: str | None = TIER_LIVE,
    tests: tuple[str, ...] | None = None,
) -> FlagCertification:
    if tests is None:
        tests = (
            (f"{_LIVE_MODULE}::test_live_probe[postgres-{flag}]",)
            if tier == TIER_LIVE
            else (f"tests/some_fixture.py::test_{flag}",)
        )
    return FlagCertification(
        flag=flag,
        claimed=claimed,
        status=status,
        tier=tier if status != STATUS_NOT_APPLICABLE else None,
        probe="p",
        evidence="e",
        tests=tests if status == STATUS_CERTIFIED else (),
        reason_code="PROBE_FAILED" if status == STATUS_NOT_CERTIFIED else None,
    )


def _result(
    connector: str,
    rows: dict[str, FlagCertification],
    *,
    held: tuple[str, ...] = (),
    fingerprint: certification.CodeFingerprint | None = None,
) -> CertificationResult:
    """A consistent result for one connector, with `derived` computed by the real rule."""
    claimed = {flag: rows[flag].claimed for flag in CAPABILITY_FLAGS}
    claims = tuple(
        UncertifiedClaim(connector, flag, "PROBE_FAILED", "what", "evidence") for flag in held
    )
    interim = CertificationResult(
        1,
        "suite",
        "2026-01-01",
        {
            connector: ConnectorCertification(
                connector,
                fingerprint or certification.CodeFingerprint("d", {}),
                {},
                claimed,
                rows,
                {},
            )
        },
        claims,
    )
    derived = {
        item.flag: item.derived
        for item in derive_flags(connector, _capabilities(**claimed), interim)
    }
    return replace(
        interim,
        connectors={
            connector: replace(interim.connectors[connector], derived=derived),
        },
    )


def _all_rows(claimed: bool, **overrides: FlagCertification) -> dict[str, FlagCertification]:
    rows = {flag: _row(flag, claimed=claimed) for flag in CAPABILITY_FLAGS}
    rows.update(overrides)
    return rows


# --- the derivation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claimed", "status", "held"),
    list(
        product(
            [True, False],
            [STATUS_CERTIFIED, STATUS_NOT_CERTIFIED, STATUS_NOT_APPLICABLE, None],
            [True, False],
        )
    ),
)
def test_derivation_never_exceeds_the_claim_whatever_the_result_says(
    claimed: bool, status: str | None, held: bool
) -> None:
    """The one structural guarantee. Every combination of claim, row status and held entry.

    A flag is advertised only if the connector claims it; beyond that it needs a CERTIFIED row
    or an explicit uncertified-claim entry. Enumerated rather than sampled because the whole
    property is a four-line truth table, and a mutant that returns the literal, ignores the
    claim, or ignores the row breaks a specific row of it.
    """
    rows = {} if status is None else {"explain": _row("explain", claimed=claimed, status=status)}
    connector = certification.ConnectorCertification(
        "postgres", certification.CodeFingerprint("d", {}), {}, {}, rows, {}
    )
    result = CertificationResult(
        1,
        "s",
        "d",
        {"postgres": connector},
        (UncertifiedClaim("postgres", "explain", "PROBE_FAILED", "w", "e"),) if held else (),
    )
    by_flag = {
        item.flag: item for item in derive_flags("postgres", _capabilities(explain=claimed), result)
    }
    explain = by_flag["explain"]

    expected = claimed and (status == STATUS_CERTIFIED or held)
    assert explain.derived is expected, (claimed, status, held)
    if not claimed:
        assert explain.derived is False, "a flag the connector does not claim was advertised"
    # `held` is reported exactly when the flag is advertised *without* a certification.
    assert explain.held is (claimed and status != STATUS_CERTIFIED and held)
    # Every flag the claim leaves False stays False, no matter what else the result holds.
    assert all(not item.derived for flag, item in by_flag.items() if flag != "explain")


def test_a_certified_flag_the_connector_does_not_claim_is_not_advertised() -> None:
    """Oracle's `explain` is the live example: the fixture probe passes, the claim is False.

    Nothing raises a claim from a probe result alone. The gateway refuses an engine that
    cannot cost a statement, so a claim must never be widened by a certification run.
    """
    result = _result("postgres", _all_rows(False, explain=_row("explain", claimed=False)))
    assert derive_capabilities("postgres", _capabilities(), result).explain is False


def test_a_claim_that_is_neither_certified_nor_listed_fails_closed() -> None:
    """A newly claimed flag is not advertised until a certification says so."""
    rows = _all_rows(False, explain=_row("explain", claimed=True, status=STATUS_NOT_CERTIFIED))
    result = _result("postgres", rows)  # explain claimed, not certified, and not held
    assert derive_capabilities("postgres", _capabilities(explain=True), result).explain is False


def test_a_connector_absent_from_the_result_advertises_nothing() -> None:
    result = _result("postgres", _all_rows(False))
    derived = derive_capabilities(
        "some_new_connector", _capabilities(explain=True, constraints=True, views=True), result
    )
    assert not any(asdict(derived).values()), "an uncertified connector advertised a capability"


def test_a_failed_probe_leaves_the_flag_as_claimed_and_is_reported() -> None:
    """The safety rule at landing, driven through the runner's own row and assembly logic.

    A probe that genuinely fails must NOT lower `explain` (that would make the gateway refuse
    the engine); it is recorded as an uncertified claim with what failed, and the flag stays as
    it was. Run through `scripts/certify_connector_capabilities.py`'s functions so a runner that
    silently lowered the flag, or dropped the record, fails here.
    """
    script = _script()
    node = f"{_LIVE_MODULE}::test_live_probe[postgres-explain]"
    failed = {
        node: {
            "outcome": "failed",
            "properties": {"probe": "estimate cost"},
            "detail": "AssertionError: no cost",
        }
    }
    row = script._row(
        "postgres", "explain", True, script.Plan("LIVE", TIER_LIVE, "", (node,)), failed
    )
    assert (row.status, row.reason_code, row.tier) == (
        STATUS_NOT_CERTIFIED,
        "PROBE_FAILED",
        TIER_LIVE,
    )
    assert "no cost" in row.evidence

    rows = _all_rows(False, explain=row)
    certified = {
        "postgres": ConnectorCertification(
            "postgres",
            certification.CodeFingerprint("d", {}),
            {},
            {flag: rows[flag].claimed for flag in CAPABILITY_FLAGS},
            rows,
            {},
        )
    }
    assembled = script._assemble(certified, "2026-01-01")

    assert [(c.connector_type, c.flag, c.reason_code) for c in assembled.uncertified_claims] == [
        ("postgres", "explain", "PROBE_FAILED")
    ], "the failed probe was not reported"
    assert assembled.connectors["postgres"].derived["explain"] is True, (
        "a failed probe silently lowered a flag the connector claims"
    )
    assert derive_capabilities("postgres", _capabilities(explain=True), assembled).explain is True


def test_the_runner_refuses_to_certify_a_live_engine_from_skipped_probes() -> None:
    """An unreachable sample container must not turn LIVE evidence into `NOT_EXERCISED`."""
    script = _script()
    node = f"{_LIVE_MODULE}::test_live_probe[postgres-explain]"
    skipped = {node: {"outcome": "skipped", "properties": {}, "detail": "no reachable server"}}
    with pytest.raises(script.RunnerError, match="probed LIVE"):
        script._row(
            "postgres", "explain", True, script.Plan("LIVE", TIER_LIVE, "", (node,)), skipped
        )


def test_the_runner_never_certifies_a_claimed_flag_as_not_applicable() -> None:
    script = _script()
    with pytest.raises(script.RunnerError, match="claimed"):
        script._row(
            "snowflake", "triggers", True, script.Plan("NOT_APPLICABLE", None, "", basis="b"), {}
        )


def test_a_passing_probe_that_observes_absence_is_not_a_certification() -> None:
    """`ABSENT` passes the test and must land as NOT_CERTIFIED, not CERTIFIED."""
    script = _script()
    node = "tests/x.py::test_fixture_probe[oracle-indexes]"
    ran = {
        node: {
            "outcome": "passed",
            "properties": {"verdict": "ABSENT", "evidence": "none"},
            "detail": "",
        }
    }
    row = script._row(
        "oracle", "indexes", False, script.Plan("PROBE", TIER_FIXTURE, "w", (node,)), ran
    )
    assert (row.status, row.reason_code) == (STATUS_NOT_CERTIFIED, "NOT_IMPLEMENTED")


# --- the committed result ------------------------------------------------------------------------


def test_the_committed_result_is_internally_consistent_and_matches_the_claims() -> None:
    claims = {
        d.connector_type: ConnectorCapabilities(**d.claimed_capabilities)
        for d in connector_registry.definitions
        if d.implementation_status == "IMPLEMENTED"
    }
    problems = verify_result(
        load_certification_result(), live_probe_modules=[_LIVE_MODULE], claims=claims
    )
    assert problems == [], problems


def test_every_implemented_connector_is_certified_for_every_flag() -> None:
    result = load_certification_result()
    for connector_type in _IMPLEMENTED:
        assert connector_type in result.connectors, f"{connector_type} has no certification"
        assert set(result.connectors[connector_type].flags) == set(CAPABILITY_FLAGS), connector_type


def test_the_staleness_gate_no_connector_has_drifted_from_its_certification() -> None:
    """The gate. Fails when a connector module (or anything it imports) changed after the
    certification was produced -- so a connector cannot silently drift from what was proven.
    Needs no database: LIVE evidence is read from the committed result.
    """
    stale = stale_connectors(load_certification_result(), connector_types=_IMPLEMENTED)
    assert stale == (), (
        "re-run scripts/certify_connector_capabilities.py (LIVE rows need the sample "
        f"containers): {[s.describe() for s in stale]}"
    )


def test_the_committed_page_is_rendered_from_the_committed_result() -> None:
    page = _REPO / "Docs" / "90-reference" / "connector-capability-certification.md"
    assert page.read_text(encoding="utf-8") == render_markdown(load_certification_result())


def test_the_uncertified_claims_are_exactly_the_reviewed_set() -> None:
    """A flag advertised without a certification is a reviewed decision, not a side effect."""
    listed = {
        (claim.connector_type, claim.flag)
        for claim in load_certification_result().uncertified_claims
    }
    assert listed == _EXPECTED_UNCERTIFIED_CLAIMS, (
        f"the set of claimed-but-uncertified flags changed: {sorted(listed)}; amend the "
        "expected set here (and tell the connector owner) if that is intended"
    )


def test_at_landing_no_derived_flag_is_lower_than_its_claim() -> None:
    """The safety rule, pinned. A certification run must never lower a flag by itself.

    Lowering `explain` makes the gateway refuse execution for that engine. If a flag is ever
    genuinely lowered, this set changes -- deliberately, in review.
    """
    lowered = {
        (d.connector_type, flag)
        for d in connector_registry.definitions
        if d.implementation_status == "IMPLEMENTED"
        for flag in CAPABILITY_FLAGS
        if d.claimed_capabilities[flag] and not d.capabilities[flag]
    }
    assert lowered == _EXPECTED_LOWERED


def test_no_derived_flag_ever_exceeds_its_claim_on_the_real_registry() -> None:
    for definition in connector_registry.definitions:
        if definition.implementation_status != "IMPLEMENTED":
            assert definition.capabilities == {}
            continue
        wider = [
            flag
            for flag in CAPABILITY_FLAGS
            if definition.capabilities[flag] and not definition.claimed_capabilities[flag]
        ]
        assert wider == [], f"{definition.connector_type} advertises unclaimed flags {wider}"


def test_a_real_connector_reports_the_derived_flags_not_its_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connector -> derivation, with a result that withholds `explain` from PostgreSQL."""
    rows = _all_rows(
        True,
        explain=_row("explain", claimed=True, status=STATUS_NOT_CERTIFIED),
    )
    result = _result("postgres", rows)
    literal = PostgresConnector.DEFAULT_CAPABILITIES
    assert literal.explain is True

    monkeypatch.setattr(certification, "load_certification_result", lambda path=None: result)
    advertised = PostgresConnector("postgresql://u:p@h:5432/d").capabilities
    assert advertised.explain is False, "the connector still reports its hand-written literal"
    assert advertised.constraints is True


# --- evidence tiers are never blurred ------------------------------------------------------------


def test_only_the_engines_with_a_live_probe_are_certified_live() -> None:
    result = load_certification_result()
    live = {
        connector_type
        for connector_type, cert in result.connectors.items()
        if any(row.tier == TIER_LIVE for row in cert.flags.values())
    }
    assert live == {"postgres", "sqlserver"}, live
    for connector_type in set(_IMPLEMENTED) - live:
        assert all(
            row.tier in {TIER_FIXTURE, None}
            for row in result.connectors[connector_type].flags.values()
        ), f"{connector_type} has no live engine here yet a row is not fixture-tier"


def test_no_fixture_result_cites_a_live_probe_and_no_live_result_cites_anything_else() -> None:
    for connector_type, cert in load_certification_result().connectors.items():
        for flag, row in cert.flags.items():
            in_live_module = [t for t in row.tests if t.startswith(_LIVE_MODULE)]
            if row.tier == TIER_LIVE:
                assert row.tests and len(in_live_module) == len(row.tests), (connector_type, flag)
            if row.tier == TIER_FIXTURE:
                assert in_live_module == [], (connector_type, flag)


def test_verify_result_refuses_a_fixture_row_that_cites_a_live_probe() -> None:
    rows = _all_rows(
        True,
        explain=_row(
            "explain",
            claimed=True,
            tier=TIER_FIXTURE,
            tests=(f"{_LIVE_MODULE}::test_live_probe[x]",),
        ),
    )
    problems = verify_result(_result("postgres", rows), live_probe_modules=[_LIVE_MODULE])
    assert any("labelled FIXTURE but cites live probes" in p for p in problems), problems


def test_verify_result_refuses_a_live_row_that_cites_a_fixture_test() -> None:
    rows = _all_rows(
        True,
        explain=_row(
            "explain", claimed=True, tier=TIER_LIVE, tests=("tests/test_connectors.py::t",)
        ),
    )
    problems = verify_result(_result("postgres", rows), live_probe_modules=[_LIVE_MODULE])
    assert any("labelled LIVE but cites non-live tests" in p for p in problems), problems


def test_verify_result_reports_a_claim_that_is_neither_certified_nor_listed() -> None:
    rows = _all_rows(True, explain=_row("explain", claimed=True, status=STATUS_NOT_CERTIFIED))
    problems = verify_result(_result("postgres", rows), live_probe_modules=[_LIVE_MODULE])
    assert any("not listed in uncertified_claims" in p for p in problems), problems


def test_verify_result_reports_a_result_written_against_a_different_claim() -> None:
    """The claim changed after certification, even if nothing else moved."""
    result = _result("postgres", _all_rows(True))
    problems = verify_result(
        result,
        live_probe_modules=[_LIVE_MODULE],
        claims={"postgres": _capabilities(explain=False)},
    )
    assert any("DEFAULT_CAPABILITIES changed since certification" in p for p in problems), problems


# --- the code fingerprint and the staleness gate -----------------------------------------


def _mirror(tmp_path: Path, connector: str) -> Path:
    """Copy exactly the files `connector`'s fingerprint covers into a private `src/` tree."""
    root = tmp_path / "src"
    for path in fingerprint_inputs(connector):
        target = root / path.relative_to(_SRC)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return root


def test_the_fingerprint_is_the_same_wherever_the_tree_lives(tmp_path: Path) -> None:
    root = _mirror(tmp_path, "postgres")
    assert compute_fingerprint("postgres", root) == compute_fingerprint("postgres")


def test_the_fingerprint_covers_the_connector_and_what_it_imports() -> None:
    files = set(compute_fingerprint("bigquery").files)
    assert "aida/connectors/bigquery.py" in files
    assert {"aida/connectors/base.py", "aida/connectors/discovery.py"} <= files
    assert "aida/capability_states.py" in files, "a transitive import was missed"
    assert not any("capability_certification" in f for f in files), "the judge is not the judged"


def test_a_change_to_a_connector_module_makes_its_certification_stale(tmp_path: Path) -> None:
    """The staleness gate has teeth: edit the connector, and the gate names the file."""
    root = _mirror(tmp_path, "postgres")
    result = certification.CertificationResult(
        1,
        "s",
        "d",
        {
            "postgres": ConnectorCertification(
                "postgres", compute_fingerprint("postgres", root), {}, {}, {}, {}
            )
        },
    )
    assert stale_connectors(result, source_root=root, connector_types=["postgres"]) == ()

    module = root / "aida" / "connectors" / "postgres.py"
    module.write_text(module.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

    stale = stale_connectors(result, source_root=root, connector_types=["postgres"])
    assert [s.connector_type for s in stale] == ["postgres"]
    assert stale[0].changed == ("aida/connectors/postgres.py",)


def test_a_change_to_a_module_every_connector_shares_stales_every_connector(tmp_path: Path) -> None:
    root = _mirror(tmp_path, "postgres")
    result = certification.CertificationResult(
        1,
        "s",
        "d",
        {
            "postgres": ConnectorCertification(
                "postgres", compute_fingerprint("postgres", root), {}, {}, {}, {}
            )
        },
    )
    base = root / "aida" / "connectors" / "base.py"
    base.write_text(base.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    stale = stale_connectors(result, source_root=root, connector_types=["postgres"])
    assert stale and stale[0].changed == ("aida/connectors/base.py",)


def test_line_endings_do_not_change_the_fingerprint(tmp_path: Path) -> None:
    root = _mirror(tmp_path, "postgres")
    before = compute_fingerprint("postgres", root)
    module = root / "aida" / "connectors" / "postgres.py"
    module.write_bytes(module.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert compute_fingerprint("postgres", root) == before


def test_a_connector_with_no_certification_is_reported_stale() -> None:
    result = certification.CertificationResult(1, "s", "d", {}, ())
    stale = stale_connectors(result, connector_types=["postgres"])
    assert [s.connector_type for s in stale] == ["postgres"]
    assert "aida/connectors/postgres.py" in stale[0].added


def test_the_runner_check_names_a_stale_connector() -> None:
    script = _script()
    result = load_certification_result()
    wrong = replace(
        result.connectors["postgres"],
        fingerprint=certification.CodeFingerprint(
            "0" * 64, result.connectors["postgres"].fingerprint.files
        ),
    )
    problems = script.check(replace(result, connectors={**result.connectors, "postgres": wrong}))
    assert any(p.startswith("stale: postgres") for p in problems), problems


# --- the capability endpoint ---------------------------------------------------------------------


async def _matrix(
    monkeypatch: pytest.MonkeyPatch, registry: ConnectorRegistry
) -> dict[str, dict[str, Any]]:
    monkeypatch.setattr(ingestion_router, "connector_registry", registry)
    context = security_context(organization_id=uuid4())
    payload = await ingestion_router.connector_capability_matrix(context=context)
    return {item.connector_type: item.model_dump() for item in payload}


async def test_the_capability_endpoint_serves_the_derived_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result that withholds `explain` must reach the endpoint as `explain: false`."""
    rows = _all_rows(
        True, explain=_row("explain", claimed=True, status=STATUS_NOT_CERTIFIED, tier=TIER_LIVE)
    )
    registry = ConnectorRegistry(certification=_result("postgres", rows))
    registry.register(
        "postgres", PostgresConnector, capabilities=PostgresConnector.DEFAULT_CAPABILITIES
    )
    served = (await _matrix(monkeypatch, registry))["postgres"]

    assert PostgresConnector.DEFAULT_CAPABILITIES.explain is True
    assert served["capabilities"]["explain"] is False, "the endpoint served the hand-written flag"
    evidence = served["capability_evidence"]["explain"]
    assert (evidence["claimed"], evidence["status"], evidence["held"]) == (
        True,
        STATUS_NOT_CERTIFIED,
        False,
    )
    assert served["capabilities"]["constraints"] is True


async def test_the_endpoint_says_which_tier_certified_each_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = await _matrix(monkeypatch, connector_registry)
    assert served["postgres"]["capability_evidence"]["explain"]["tier"] == TIER_LIVE
    assert served["sqlserver"]["capability_evidence"]["explain"]["tier"] == TIER_LIVE
    assert served["bigquery"]["capability_evidence"]["explain"]["tier"] == TIER_FIXTURE
    assert served["snowflake"]["capability_evidence"]["delegated_identity"]["tier"] == TIER_FIXTURE
    # Held, not certified: advertised, and the endpoint says so instead of implying a proof.
    partitions = served["snowflake"]["capability_evidence"]["partitions"]
    assert served["snowflake"]["capabilities"]["partitions"] is True
    assert (partitions["status"], partitions["tier"], partitions["held"]) == (
        STATUS_NOT_CERTIFIED,
        None,
        True,
    ), "a held flag has no certified tier, and the endpoint must not invent one"
    # A flag that is False keeps its evidence too.
    assert served["oracle"]["capabilities"]["explain"] is False
    assert served["oracle"]["capability_evidence"]["explain"]["claimed"] is False
    # Planned connectors advertise nothing and carry no evidence.
    assert served["teradata"]["capabilities"] == {}
    assert served["teradata"]["capability_evidence"] == {}


async def test_the_endpoint_never_lists_a_flag_as_certified_without_a_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = await _matrix(monkeypatch, connector_registry)
    for connector_type, body in served.items():
        for flag, evidence in body["capability_evidence"].items():
            if evidence["status"] == STATUS_CERTIFIED:
                assert evidence["tier"] in {TIER_LIVE, TIER_FIXTURE}, (connector_type, flag)
            else:
                assert evidence["tier"] is None, (connector_type, flag)


# --- the load-bearing consequence, through the real gateway ------------------------------


class _CannedPostgres(PostgresConnector):
    """The real connector class -- so `capabilities` is the real derivation -- with no network."""

    async def estimate_read_query(self, sql: str, *, timeout_seconds: int) -> QueryEstimate:
        return QueryEstimate(score=1.0, kind="EXPLAIN_COST", estimated_rows=1.0)

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        return QueryResult(rows=({"customer_id": 1},), warehouse_query_id="canned")


async def _gateway_outcome(monkeypatch: pytest.MonkeyPatch, result: CertificationResult) -> str:
    monkeypatch.setattr(certification, "load_certification_result", lambda path=None: result)
    executor = _CannedPostgres("postgresql://u:p@h:5432/d")
    datasource = DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="c14",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="vault://c14",
        status="ACTIVE",
    )
    monkeypatch.setattr("aida.query_gateway.open_execution_session", lambda t, d: executor)
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type("_R", (), {"resolve": staticmethod(lambda ref: "dsn://x")})(),
    )
    session = CatalogSession(
        tables=[("analytics_db", "analytics", "customers")],
        columns=[("analytics_db", "analytics", "customers", "customer_id")],
        sensitive_columns=[],
    )
    try:
        outcome = await QueryExecutionGateway(Settings(_env_file=None)).execute(
            session,
            datasource=datasource,
            context=security_context(organization_id=datasource.organization_id),
            correlation_id="corr-c14",
            sql="SELECT customer_id FROM analytics.customers",
            requested_limit=10,
            semantic_version=None,
        )
        return outcome.execution.status
    except QueryRejected as rejected:
        return f"REJECTED: {rejected}"


async def test_a_certification_that_withholds_explain_makes_the_real_gateway_refuse_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """certification result -> `connector.capabilities` -> the gateway's cost gate.

    `explain` is the flag with teeth. The gateway asks the connector it is about to run, and
    that connector's flag now comes from the certification result, so a result that does not
    certify `explain` (and does not hold it) turns into a refusal at the gateway. A connector
    still reading its literal would sail through this test's first assertion.
    """
    withheld = _result(
        "postgres",
        _all_rows(True, explain=_row("explain", claimed=True, status=STATUS_NOT_CERTIFIED)),
    )
    outcome = await _gateway_outcome(monkeypatch, withheld)
    assert outcome.startswith("REJECTED"), (
        f"an uncertified explain was allowed to execute: {outcome}"
    )


async def test_a_certification_that_certifies_explain_lets_the_real_gateway_run_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion: without it, a gateway that refused everything would pass the test above."""
    outcome = await _gateway_outcome(monkeypatch, _result("postgres", _all_rows(True)))
    assert outcome == "COMPLETED", outcome
