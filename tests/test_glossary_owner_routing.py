"""GL-6: unowned-asset backlog owner routing and escalation.

Follows the fake-session pattern established in test_glossary_stewardship.py /
test_glossary_contracts.py -- no live Postgres/Docker, handlers called
directly against in-memory fakes.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from aida import notification_routing
from aida.glossary_owner_routing import (
    DEFAULT_ESCALATE_AFTER,
    DEFAULT_ROUTE_AFTER,
    TableFacts,
    select_candidate_owner,
    sync_unowned_asset_backlog,
)
from aida.main import app
from aida.models import (
    MetadataSchema,
    MetadataTable,
    NotificationRuleRecord,
    OwnershipRule,
    UnownedAssetEscalation,
)
from aida.schemas import UnownedAssetBacklogRouteRequest
from aida.security import SecurityContext
from aida.stewardship_api import list_unowned_asset_backlog, route_unowned_asset_backlog

# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def test_glossary_owner_routing_api_contracts_are_exposed() -> None:
    paths = app.openapi()["paths"]

    # GL-6: bounded unowned-asset backlog with automated owner routing/escalation
    assert "/v1/organizations/{organization_id}/stewardship/unowned-backlog" in paths
    assert "/v1/organizations/{organization_id}/stewardship/unowned-backlog/route" in paths


def test_glossary_owner_routing_reuses_dq1_engine_functions_directly() -> None:
    """GL-6 must not fork DQ-1's routing/escalation engine -- it imports the
    same functions, not reimplementations of them."""
    from aida import glossary_owner_routing

    assert glossary_owner_routing.route_notification is notification_routing.route_notification
    assert glossary_owner_routing.should_escalate is notification_routing.should_escalate
    assert glossary_owner_routing.escalate is notification_routing.escalate
    assert glossary_owner_routing.format_itsm_payload is notification_routing.format_itsm_payload
    assert glossary_owner_routing.Incident is notification_routing.Incident
    assert glossary_owner_routing.NotificationRule is notification_routing.NotificationRule


# ---------------------------------------------------------------------------
# select_candidate_owner
# ---------------------------------------------------------------------------


def _facts(**overrides: object) -> TableFacts:
    defaults: dict[str, object] = {
        "table_id": uuid4(),
        "datasource_id": uuid4(),
        "table_name": "stg_payments",
        "schema_name": "raw",
        "domain_key": "payments",
        "tags": ("pii",),
    }
    defaults.update(overrides)
    return TableFacts(**defaults)  # type: ignore[arg-type]


def _ownership_rule(**overrides: object) -> OwnershipRule:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "rule_key": "stg-owner",
        "display_name": "Staging tables -> Jane",
        "match_field": "TABLE_NAME",
        "match_pattern": "stg_*",
        "owner_type": "INDIVIDUAL",
        "owner_principal": "jane@bank.example",
        "status": "ACTIVE",
        "created_by": "admin",
    }
    defaults.update(overrides)
    return OwnershipRule(**defaults)  # type: ignore[arg-type]


def test_select_candidate_owner_matches_table_name_pattern() -> None:
    facts = _facts(table_name="stg_payments")
    rule = _ownership_rule(match_field="TABLE_NAME", match_pattern="stg_*")

    assert select_candidate_owner(facts, [rule]) == "jane@bank.example"


def test_select_candidate_owner_matches_tag_glob() -> None:
    facts = _facts(table_name="fct_orders", tags=("finance-owned",))
    rule = _ownership_rule(
        match_field="TAG", match_pattern="finance-*", owner_principal="finance-team@bank.example"
    )

    assert select_candidate_owner(facts, [rule]) == "finance-team@bank.example"


def test_select_candidate_owner_returns_none_when_nothing_matches() -> None:
    facts = _facts(table_name="fct_orders", schema_name="curated", domain_key=None, tags=())
    rule = _ownership_rule(match_field="TABLE_NAME", match_pattern="stg_*")

    assert select_candidate_owner(facts, [rule]) is None


def test_select_candidate_owner_skips_inactive_rules() -> None:
    facts = _facts(table_name="stg_payments")
    rule = _ownership_rule(match_field="TABLE_NAME", match_pattern="stg_*", status="RETIRED")

    assert select_candidate_owner(facts, [rule]) is None


# ---------------------------------------------------------------------------
# sync_unowned_asset_backlog
# ---------------------------------------------------------------------------


def _notification_rule(**overrides: object) -> NotificationRuleRecord:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "name": "Unowned asset backlog",
        "conditions": {},
        "channel": "EMAIL",
        "recipients": ["steward-lead@bank.example"],
        "escalation_after_minutes": 60,
        "enabled": True,
        "created_by": "admin",
    }
    defaults.update(overrides)
    return NotificationRuleRecord(**defaults)  # type: ignore[arg-type]


def test_sync_creates_pending_entry_without_routing_before_the_bound() -> None:
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={table_id},
        existing_entries={},
        table_facts={table_id: _facts(table_id=table_id)},
        ownership_rules=[],
        notification_rules=[_notification_rule(organization_id=organization_id)],
        now=now,
    )

    assert len(result.created) == 1
    entry = result.created[0]
    assert entry.table_id == table_id
    assert entry.status == "PENDING"
    assert entry.first_detected_unowned_at == now
    assert result.routed == []
    assert result.escalated == []


def test_sync_routes_aged_pending_entry_to_matched_rule_and_candidate_owner() -> None:
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)
    facts = _facts(table_id=table_id, table_name="stg_payments")
    existing = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table_id,
        first_detected_unowned_at=now - DEFAULT_ROUTE_AFTER - timedelta(hours=1),
        status="PENDING",
        recipients=[],
    )
    ownership_rule = _ownership_rule(
        organization_id=organization_id, match_field="TABLE_NAME", match_pattern="stg_*"
    )
    notify_rule = _notification_rule(organization_id=organization_id, channel="EMAIL")

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={table_id},
        existing_entries={table_id: existing},
        table_facts={table_id: facts},
        ownership_rules=[ownership_rule],
        notification_rules=[notify_rule],
        now=now,
    )

    assert result.created == []
    assert len(result.routed) == 1
    routed = result.routed[0]
    assert routed is existing
    assert routed.status == "ROUTED"
    assert routed.candidate_owner == "jane@bank.example"
    assert routed.notification_rule_id == notify_rule.id
    assert routed.channel == "EMAIL"
    assert routed.recipients == ["steward-lead@bank.example"]
    assert routed.dedup_key
    assert routed.routed_at == now
    assert result.escalated == []


def test_sync_routes_straight_to_escalated_once_past_escalate_after() -> None:
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)
    existing = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table_id,
        first_detected_unowned_at=now - DEFAULT_ESCALATE_AFTER - timedelta(hours=1),
        status="PENDING",
        recipients=[],
    )

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={table_id},
        existing_entries={table_id: existing},
        table_facts={table_id: _facts(table_id=table_id)},
        ownership_rules=[],
        notification_rules=[_notification_rule(organization_id=organization_id)],
        now=now,
    )

    assert result.routed == []
    assert result.escalated == [existing]
    assert existing.status == "ESCALATED"
    assert existing.escalated_at == now


def test_sync_leaves_entry_pending_when_no_notification_rule_matches() -> None:
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)
    existing = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table_id,
        first_detected_unowned_at=now - DEFAULT_ROUTE_AFTER - timedelta(hours=1),
        status="PENDING",
        recipients=[],
    )
    non_matching_rule = _notification_rule(
        organization_id=organization_id, conditions={"severity": "INFO"}
    )

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={table_id},
        existing_entries={table_id: existing},
        table_facts={table_id: _facts(table_id=table_id)},
        ownership_rules=[],
        notification_rules=[non_matching_rule],
        now=now,
    )

    assert result.routed == []
    assert result.escalated == []
    assert existing.status == "PENDING"
    assert existing.candidate_owner is None


def test_sync_escalates_routed_entry_past_the_rules_escalation_deadline() -> None:
    """Reuses ``should_escalate``/``escalate`` verbatim -- the deadline check
    reads real wall-clock time internally (matching DQ-1's own engine), so a
    ``routed_at`` set far in the past guarantees the deadline has passed
    regardless of when the test runs."""
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)
    rule = _notification_rule(organization_id=organization_id, escalation_after_minutes=5)
    existing = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table_id,
        first_detected_unowned_at=datetime(2000, 1, 1, tzinfo=UTC),
        status="ROUTED",
        candidate_owner="jane@bank.example",
        notification_rule_id=rule.id,
        channel="EMAIL",
        recipients=["steward-lead@bank.example"],
        dedup_key="dk-1",
        routed_at=datetime(2000, 1, 1, tzinfo=UTC),
    )

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={table_id},
        existing_entries={table_id: existing},
        table_facts={table_id: _facts(table_id=table_id)},
        ownership_rules=[],
        notification_rules=[rule],
        now=now,
    )

    assert result.escalated == [existing]
    assert existing.status == "ESCALATED"
    assert existing.escalated_at is not None


def test_sync_does_not_escalate_routed_entry_before_the_deadline() -> None:
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)
    rule = _notification_rule(organization_id=organization_id, escalation_after_minutes=999_999)
    existing = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table_id,
        first_detected_unowned_at=now - DEFAULT_ROUTE_AFTER,
        status="ROUTED",
        candidate_owner="jane@bank.example",
        notification_rule_id=rule.id,
        channel="EMAIL",
        recipients=["steward-lead@bank.example"],
        dedup_key="dk-1",
        routed_at=now,
    )

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={table_id},
        existing_entries={table_id: existing},
        table_facts={table_id: _facts(table_id=table_id)},
        ownership_rules=[],
        notification_rules=[rule],
        now=now,
    )

    assert result.escalated == []
    assert existing.status == "ROUTED"


def test_sync_resolves_entry_whose_table_is_no_longer_unowned() -> None:
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)
    existing = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table_id,
        first_detected_unowned_at=now - timedelta(days=1),
        status="ROUTED",
        recipients=[],
    )

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids=set(),  # table now has an owner
        existing_entries={table_id: existing},
        table_facts={},
        ownership_rules=[],
        notification_rules=[],
        now=now,
    )

    assert result.resolved == [existing]
    assert existing.status == "RESOLVED"
    assert existing.resolved_at == now


def test_sync_route_limit_does_not_falsely_resolve_entries_outside_the_capped_slice() -> None:
    """A table beyond the ``route_limit`` slice this pass didn't touch is
    still unowned -- it must not be silently marked resolved just because it
    wasn't processed. Correctness of resolution depends on the *complete*
    unowned set, not the capped routing slice."""
    organization_id = uuid4()
    now = datetime.now(UTC)
    tracked_id = UUID("00000000-0000-0000-0000-000000000002")
    other_id = UUID("00000000-0000-0000-0000-000000000001")  # sorts first
    tracked_entry = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=tracked_id,
        first_detected_unowned_at=now - timedelta(days=1),
        status="PENDING",
        recipients=[],
    )

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={tracked_id, other_id},  # both still unowned
        existing_entries={tracked_id: tracked_entry},
        table_facts={},  # neither has facts loaded (outside the fetched slice)
        ownership_rules=[],
        notification_rules=[],
        now=now,
        route_limit=1,  # only `other_id` (sorts first) is processed this pass
    )

    assert result.resolved == []
    assert tracked_entry.status == "PENDING"


def test_sync_itsm_channel_produces_the_shared_itsm_payload() -> None:
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)
    existing = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table_id,
        first_detected_unowned_at=now - DEFAULT_ROUTE_AFTER - timedelta(hours=1),
        status="PENDING",
        recipients=[],
    )
    itsm_rule = _notification_rule(organization_id=organization_id, channel="ITSM")

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={table_id},
        existing_entries={table_id: existing},
        table_facts={table_id: _facts(table_id=table_id)},
        ownership_rules=[],
        notification_rules=[itsm_rule],
        now=now,
    )

    assert len(result.itsm_payloads) == 1
    payload = result.itsm_payloads[0]
    assert payload["category"] == "data_quality"
    assert payload["correlation_id"] == f"unowned-asset:{table_id}"


def test_sync_escalates_to_tier2_past_the_second_deadline() -> None:
    """GL-6: an entry still unaddressed long enough after its *tier-1*
    escalation escalates again, unconditionally through ITSM -- regardless of
    what channel tier 1 used (EMAIL here), since tier 2 is a fixed
    operational backstop, not a second configurable notification rule."""
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)
    existing = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table_id,
        first_detected_unowned_at=datetime(2000, 1, 1, tzinfo=UTC),
        status="ESCALATED",
        candidate_owner="jane@bank.example",
        channel="EMAIL",
        recipients=["steward-lead@bank.example"],
        dedup_key="dk-1",
        routed_at=datetime(2000, 1, 1, tzinfo=UTC),
        escalated_at=datetime(2000, 1, 2, tzinfo=UTC),
    )

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={table_id},
        existing_entries={table_id: existing},
        table_facts={table_id: _facts(table_id=table_id)},
        ownership_rules=[],
        notification_rules=[],
        now=now,
    )

    assert result.escalated_tier2 == [existing]
    assert existing.status == "ESCALATED_TIER_2"
    assert existing.escalated_tier2_at == now
    assert len(result.itsm_payloads) == 1
    assert result.itsm_payloads[0]["correlation_id"] == f"unowned-asset:{table_id}"


def test_sync_does_not_escalate_to_tier2_before_the_second_deadline() -> None:
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)
    existing = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table_id,
        first_detected_unowned_at=now - DEFAULT_ROUTE_AFTER - DEFAULT_ESCALATE_AFTER,
        status="ESCALATED",
        candidate_owner="jane@bank.example",
        channel="EMAIL",
        recipients=["steward-lead@bank.example"],
        dedup_key="dk-1",
        routed_at=now - DEFAULT_ESCALATE_AFTER,
        escalated_at=now,
    )

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={table_id},
        existing_entries={table_id: existing},
        table_facts={table_id: _facts(table_id=table_id)},
        ownership_rules=[],
        notification_rules=[],
        now=now,
    )

    assert result.escalated_tier2 == []
    assert existing.status == "ESCALATED"
    assert existing.escalated_tier2_at is None
    assert result.itsm_payloads == []


def test_sync_tier2_escalation_produces_itsm_payload_even_without_a_matching_rule() -> None:
    """Distinguishes tier 2 from tier 1: tier 1 only adds an ITSM payload
    when the matched rule's own channel is ITSM (see
    ``test_sync_itsm_channel_produces_the_shared_itsm_payload``); tier 2
    always does, even with zero notification rules at all."""
    organization_id = uuid4()
    table_id = uuid4()
    now = datetime.now(UTC)
    existing = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table_id,
        first_detected_unowned_at=datetime(2000, 1, 1, tzinfo=UTC),
        status="ESCALATED",
        candidate_owner=None,
        recipients=[],
        escalated_at=datetime(2000, 1, 15, tzinfo=UTC),
    )

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids={table_id},
        existing_entries={table_id: existing},
        table_facts={table_id: _facts(table_id=table_id)},
        ownership_rules=[],
        notification_rules=[],
        now=now,
    )

    assert existing.status == "ESCALATED_TIER_2"
    assert len(result.itsm_payloads) == 1
    assert result.itsm_payloads[0]["correlation_id"] == f"unowned-asset:{table_id}"


# ---------------------------------------------------------------------------
# API wiring (fake session, in the style of _OwnershipRuleSession /
# _ConflictDetectionSession above in test_glossary_stewardship.py)
# ---------------------------------------------------------------------------


def _apply_flush_defaults(instance: object) -> None:
    table = getattr(type(instance), "__table__", None)
    if table is None:
        return
    for column in table.columns:
        if getattr(instance, column.name, None) is not None:
            continue
        default = column.default
        if default is None:
            continue
        value = default.arg(None) if default.is_callable else default.arg
        setattr(instance, column.name, value)


class _BacklogRouteSession:
    """Serves queued `.scalars()` results in call order, plus one `.execute()`
    result for the table-facts join -- the same shape `route_unowned_asset_backlog`
    issues its queries in."""

    def __init__(self, *, scalars_queue: list[list[object]], execute_rows: list[tuple]) -> None:
        self.scalars_queue = list(scalars_queue)
        self.execute_rows = execute_rows
        self.added: list[object] = []
        self.timeline: list[str] = []

    async def scalars(self, _statement: object) -> object:
        values = self.scalars_queue.pop(0)

        class _Result:
            def all(self_inner) -> list[object]:
                return values

            def __iter__(self_inner):
                return iter(values)

        return _Result()

    async def scalar(self, _statement: object) -> object:
        return None

    async def execute(self, _statement: object) -> object:
        rows = self.execute_rows

        class _Result:
            def all(self_inner) -> list[tuple]:
                return rows

        return _Result()

    def add(self, value: object) -> None:
        _apply_flush_defaults(value)
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.timeline.append("commit")


async def test_route_unowned_asset_backlog_routes_a_table_past_the_bound() -> None:
    organization_id = uuid4()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=organization_id,
        catalog_id=uuid4(),
        name="raw",
        fingerprint="fp",
    )
    table = MetadataTable(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=uuid4(),
        schema_id=schema.id,
        name="stg_payments",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    ownership_rule = _ownership_rule(
        organization_id=organization_id, match_field="TABLE_NAME", match_pattern="stg_*"
    )
    notify_rule = _notification_rule(organization_id=organization_id)
    now = datetime.now(UTC)
    existing_entry = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table.id,
        first_detected_unowned_at=now - DEFAULT_ROUTE_AFTER - timedelta(hours=1),
        status="PENDING",
        recipients=[],
        created_at=now,
        updated_at=now,
    )

    session = _BacklogRouteSession(
        scalars_queue=[
            [table],  # _scope_table_ids
            [],  # _owned_table_ids: OwnershipAssignment
            [],  # _owned_table_ids: AssetDocumentation
            [existing_entry],  # existing UnownedAssetEscalation rows
            [ownership_rule],  # active OwnershipRule rows
            [notify_rule],  # enabled NotificationRuleRecord rows
        ],
        execute_rows=[(table, schema, None, None)],
    )
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )

    result = await route_unowned_asset_backlog(
        organization_id,
        UnownedAssetBacklogRouteRequest(),
        context,
        session,  # type: ignore[arg-type]
    )

    assert len(result.routed) == 1
    assert result.routed[0].candidate_owner == "jane@bank.example"
    assert result.routed[0].status == "ROUTED"
    assert result.escalated == []
    assert result.resolved_count == 0
    assert session.timeline == ["commit"]


async def test_route_unowned_asset_backlog_escalates_to_tier2() -> None:
    organization_id = uuid4()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=organization_id,
        catalog_id=uuid4(),
        name="raw",
        fingerprint="fp",
    )
    table = MetadataTable(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=uuid4(),
        schema_id=schema.id,
        name="stg_payments",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    notify_rule = _notification_rule(organization_id=organization_id)
    now = datetime.now(UTC)
    existing_entry = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=table.id,
        first_detected_unowned_at=datetime(2000, 1, 1, tzinfo=UTC),
        status="ESCALATED",
        candidate_owner="jane@bank.example",
        notification_rule_id=notify_rule.id,
        channel="EMAIL",
        recipients=["steward-lead@bank.example"],
        dedup_key="dk-1",
        escalated_at=datetime(2000, 1, 15, tzinfo=UTC),
        created_at=now,
        updated_at=now,
    )

    session = _BacklogRouteSession(
        scalars_queue=[
            [table],  # _scope_table_ids
            [],  # _owned_table_ids: OwnershipAssignment
            [],  # _owned_table_ids: AssetDocumentation
            [existing_entry],  # existing UnownedAssetEscalation rows
            [],  # active OwnershipRule rows
            [notify_rule],  # enabled NotificationRuleRecord rows
        ],
        execute_rows=[(table, schema, None, None)],
    )
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )

    result = await route_unowned_asset_backlog(
        organization_id,
        UnownedAssetBacklogRouteRequest(),
        context,
        session,  # type: ignore[arg-type]
    )

    assert len(result.escalated_tier2) == 1
    assert result.escalated_tier2[0].status == "ESCALATED_TIER_2"
    assert result.escalated == []
    assert session.timeline == ["commit"]


async def test_list_unowned_asset_backlog_excludes_resolved_by_default() -> None:
    organization_id = uuid4()
    now = datetime.now(UTC)
    open_entry = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=uuid4(),
        first_detected_unowned_at=now,
        status="PENDING",
        recipients=[],
        created_at=now,
        updated_at=now,
    )
    session = _BacklogRouteSession(scalars_queue=[[open_entry]], execute_rows=[])
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Viewer"}),
    )

    page = await list_unowned_asset_backlog(
        organization_id,
        None,
        100,
        0,
        context,
        session,  # type: ignore[arg-type]
    )

    assert len(page.items) == 1
    assert page.items[0].status == "PENDING"
