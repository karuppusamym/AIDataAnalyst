from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from aida.glossary_api import create_glossary_term_version, submit_glossary_term_version
from aida.main import app
from aida.models import (
    AssetCertification,
    GlossaryConflict,
    GlossaryLinkProposal,
    GlossaryTerm,
    GlossaryTermVersion,
    MetadataBusinessAnnotation,
    MetadataBusinessAnnotationVersion,
    MetadataSchema,
    MetadataTable,
    OwnershipRule,
)
from aida.schemas import (
    BulkStewardshipOperationCreate,
    GlossaryCategoryCreate,
    GlossaryConflictResolution,
    GlossaryLinkProposalGenerate,
    GlossaryTermDeprecationRequest,
    GlossaryTermVersionCreate,
    OwnershipRuleCreate,
)
from aida.security import SecurityContext
from aida.stewardship_api import (
    apply_ownership_rule,
    detect_glossary_conflicts,
    generate_glossary_link_proposals,
    submit_conflict_resolution,
)
from aida.stewardship_service import active_certified_table_ids


def test_glossary_stewardship_api_contracts_are_exposed() -> None:
    paths = app.openapi()["paths"]

    # GL-1: term deprecation, maker-checker via the shared governance review queue
    assert "/v1/glossary-terms/{term_id}/deprecate" in paths
    assert "/v1/organizations/{organization_id}/stewardship/bulk-operations" in paths

    # GL-2: bulk, rule-based ownership assignment
    assert "/v1/organizations/{organization_id}/ownership-rules" in paths
    assert "/v1/ownership-rules/{rule_id}/apply" in paths
    assert "/v1/organizations/{organization_id}/ownership-assignments" in paths

    # GL-3: conflict detection and resolution, losing position retained
    assert "/v1/organizations/{organization_id}/glossary-conflicts" in paths
    assert "/v1/organizations/{organization_id}/glossary-conflicts/detect" in paths
    assert "/v1/glossary-conflicts/{conflict_id}/resolution" in paths

    # GL-4: coverage scoring, per domain/LOB, trended over time
    assert "/v1/organizations/{organization_id}/stewardship/coverage" in paths
    assert "/v1/organizations/{organization_id}/stewardship/coverage/snapshots" in paths

    # GL-8 (breadth already built alongside the required scope)
    assert "/v1/organizations/{organization_id}/glossary-link-proposals/generate" in paths
    assert "/v1/glossary-link-proposals/{proposal_id}/submit" in paths
    assert "/v1/organizations/{organization_id}/glossary-categories" in paths


def test_glossary_term_deprecation_requires_a_real_reason() -> None:
    with pytest.raises(ValidationError):
        GlossaryTermDeprecationRequest(reason="too short")

    request = GlossaryTermDeprecationRequest(reason="Superseded by the FY26 revenue definition.")
    assert request.reason.startswith("Superseded")


def test_ownership_rule_supports_domain_schema_tag_and_pattern_matching() -> None:
    for match_field in ("TABLE_NAME", "SCHEMA_NAME", "QUALIFIED_NAME", "DOMAIN_KEY", "TAG"):
        rule = OwnershipRuleCreate(
            rule_key="finance-staging-owner",
            display_name="Finance staging owner",
            match_field=match_field,
            match_pattern="stg_finance_*",
            owner_type="INDIVIDUAL",
            owner_principal="finance-data-steward",
        )
        assert rule.match_field == match_field

    with pytest.raises(ValidationError):
        OwnershipRuleCreate(
            rule_key="bad",
            display_name="Bad rule",
            match_field="LOB",
            match_pattern="*",
            owner_type="INDIVIDUAL",
            owner_principal="someone",
        )


def test_bulk_ownership_assignment_requires_owner_fields() -> None:
    with pytest.raises(ValidationError, match="owner_type and owner_principal"):
        BulkStewardshipOperationCreate(
            operation_type="ASSIGN_OWNERSHIP",
            subject_type="TABLE",
            subject_ids=[uuid4()],
        )

    operation = BulkStewardshipOperationCreate(
        operation_type="ASSIGN_OWNERSHIP",
        subject_type="TABLE",
        subject_ids=[uuid4(), uuid4()],
        owner_type="GROUP",
        owner_principal="risk-data-team",
    )
    assert operation.owner_principal == "risk-data-team"


def test_bulk_operation_rejects_duplicate_subjects() -> None:
    subject_id = uuid4()
    with pytest.raises(ValidationError, match="unique"):
        BulkStewardshipOperationCreate(
            operation_type="LINK_TERM",
            subject_type="TABLE",
            subject_ids=[subject_id, subject_id],
            term_id=uuid4(),
        )


def test_link_term_operation_requires_term_id() -> None:
    with pytest.raises(ValidationError, match="term_id"):
        BulkStewardshipOperationCreate(
            operation_type="LINK_TERM",
            subject_type="TABLE",
            subject_ids=[uuid4()],
        )


def test_deprecate_and_certify_operations_require_rationale() -> None:
    with pytest.raises(ValidationError, match="rationale"):
        BulkStewardshipOperationCreate(
            operation_type="DEPRECATE_TERM",
            subject_type="TERM",
            subject_ids=[uuid4()],
        )
    with pytest.raises(ValidationError, match="rationale"):
        BulkStewardshipOperationCreate(
            operation_type="CERTIFY_ASSET",
            subject_type="TABLE",
            subject_ids=[uuid4()],
        )


def test_certify_operation_requires_an_expiry() -> None:
    with pytest.raises(ValidationError, match="expires_at"):
        BulkStewardshipOperationCreate(
            operation_type="CERTIFY_ASSET",
            subject_type="TABLE",
            subject_ids=[uuid4()],
            rationale="Owner confirmed accuracy against the source ledger.",
        )


def test_glossary_conflict_resolution_requires_rationale() -> None:
    with pytest.raises(ValidationError):
        GlossaryConflictResolution(resolution="ACCEPT_POSITION_A", rationale="short")

    # The losing position is never discarded by the resolution payload itself --
    # RETAIN_BOTH is a first-class resolution outcome, not a fallback.
    resolution = GlossaryConflictResolution(
        resolution="RETAIN_BOTH",
        rationale="Both definitions are valid in their respective reporting contexts.",
    )
    assert resolution.resolution == "RETAIN_BOTH"


def test_glossary_link_proposal_generation_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        GlossaryLinkProposalGenerate(minimum_confidence=0.1)
    with pytest.raises(ValidationError):
        GlossaryLinkProposalGenerate(limit=0)

    defaults = GlossaryLinkProposalGenerate()
    assert 0.5 <= defaults.minimum_confidence <= 1.0
    assert 1 <= defaults.limit <= 500


def test_glossary_category_key_must_be_stable_lowercase() -> None:
    with pytest.raises(ValidationError):
        GlossaryCategoryCreate(
            category_key="Finance Domain",
            display_name="Finance",
            description="Finance-related governed terms.",
        )

    category = GlossaryCategoryCreate(
        category_key="finance",
        display_name="Finance",
        description="Finance-related governed terms.",
    )
    assert category.category_key == "finance"


# ---------------------------------------------------------------------------
# Behavioral coverage below: the tests above prove routes are registered and
# request payloads validate. These call the real handler functions directly
# (bypassing FastAPI dependency injection, the pattern already established
# in test_high_stakes_behaviors.py / test_operational_behaviors.py) against
# fake sessions, to prove the described behavior actually happens.
# ---------------------------------------------------------------------------


def _apply_flush_defaults(instance: object) -> None:
    """Approximate what a real ``session.flush()`` populates via column defaults.

    The fake sessions below never touch a real engine, so SQLAlchemy's own
    default machinery (``id``, ``status``, ``created_at``, ...) never runs. The
    handlers under test read those fields back off the object they just created
    (e.g. to build the response model), so this fills them in the same way a
    live flush would -- not to make an assertion pass, but so the code under
    test can run its normal path at all.
    """
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


class _QueueSession:
    """A fake session whose `.get()` returns a fixed value and whose `.scalar()`
    calls pop preset results off a queue in call order -- the pattern already
    used for `DeprecationSession`/`ReservationSession` in the other gap-closing
    test files.
    """

    def __init__(
        self, *, get_result: object = None, scalar_results: list[object] | None = None
    ) -> None:
        self.get_result = get_result
        self.scalar_results = list(scalar_results or [])
        self.added: list[object] = []
        self.timeline: list[str] = []

    async def get(self, _model: type[object], _identity: object) -> object:
        return self.get_result

    async def scalar(self, _statement: object) -> object:
        return self.scalar_results.pop(0)

    def add(self, value: object) -> None:
        _apply_flush_defaults(value)
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.timeline.append("commit")


# --- GL-1: term lifecycle (versioning + maker-checker submission) ----------


async def test_create_glossary_term_version_increments_the_version_number() -> None:
    organization_id = uuid4()
    term = GlossaryTerm(
        id=uuid4(),
        organization_id=organization_id,
        term_key="net_revenue",
        lifecycle_status="ACTIVE",
    )
    # no open version, latest existing version == 2
    session = _QueueSession(get_result=term, scalar_results=[None, 2])
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )
    body = GlossaryTermVersionCreate(
        display_name="Net Revenue", definition="Revenue after returns and discounts.", synonyms=[]
    )

    result = await create_glossary_term_version(
        term.id,
        body,
        context,
        session,  # type: ignore[arg-type]
    )

    assert result.version == 3
    assert result.status == "DRAFT"
    assert session.timeline == ["commit"]


async def test_create_glossary_term_version_rejects_a_second_open_version() -> None:
    organization_id = uuid4()
    term = GlossaryTerm(
        id=uuid4(),
        organization_id=organization_id,
        term_key="net_revenue",
        lifecycle_status="ACTIVE",
    )
    open_version = GlossaryTermVersion(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term.id,
        version=1,
        status="DRAFT",
        display_name="Net Revenue",
        definition="Revenue after returns and discounts.",
        created_by="admin",
    )
    session = _QueueSession(get_result=term, scalar_results=[open_version])
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )
    body = GlossaryTermVersionCreate(
        display_name="Net Revenue v2", definition="An updated revenue definition.", synonyms=[]
    )

    with pytest.raises(HTTPException) as denied:
        await create_glossary_term_version(
            term.id,
            body,
            context,
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 409


async def test_create_glossary_term_version_rejects_deprecated_terms() -> None:
    organization_id = uuid4()
    term = GlossaryTerm(
        id=uuid4(),
        organization_id=organization_id,
        term_key="legacy_metric",
        lifecycle_status="DEPRECATED",
    )
    session = _QueueSession(get_result=term, scalar_results=[])
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )
    body = GlossaryTermVersionCreate(
        display_name="Legacy Metric", definition="No longer used by any team.", synonyms=[]
    )

    with pytest.raises(HTTPException) as denied:
        await create_glossary_term_version(
            term.id,
            body,
            context,
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 409


async def test_submit_glossary_term_version_opens_a_governance_review_for_a_draft() -> None:
    organization_id = uuid4()
    version = GlossaryTermVersion(
        id=uuid4(),
        organization_id=organization_id,
        term_id=uuid4(),
        version=1,
        status="DRAFT",
        display_name="Net Revenue",
        definition="Revenue after returns and discounts.",
        created_by="admin",
    )
    session = _QueueSession(get_result=version, scalar_results=[None])  # no existing pending review
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )

    review = await submit_glossary_term_version(
        version.id,
        context,
        session,  # type: ignore[arg-type]
    )

    assert version.status == "REVIEW_REQUIRED"
    assert review.object_type == "GLOSSARY_TERM_VERSION"
    assert review.object_id == str(version.id)
    assert review.status == "PENDING"
    assert session.timeline == ["commit"]


async def test_submit_glossary_term_version_rejects_a_non_draft_non_pending_version() -> None:
    organization_id = uuid4()
    version = GlossaryTermVersion(
        id=uuid4(),
        organization_id=organization_id,
        term_id=uuid4(),
        version=1,
        status="APPROVED",
        display_name="Net Revenue",
        definition="Revenue after returns and discounts.",
        created_by="admin",
    )
    session = _QueueSession(get_result=version, scalar_results=[])
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )

    with pytest.raises(HTTPException) as denied:
        await submit_glossary_term_version(
            version.id,
            context,
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 409


# --- GL-2: rule-based ownership assignment (bulk) ---------------------------


class _OwnershipRuleSession:
    def __init__(
        self, *, rule: OwnershipRule, joined_rows: list[tuple[object, ...]], subject_count: int
    ) -> None:
        self.rule = rule
        self.joined_rows = joined_rows
        self.subject_count = subject_count
        self.added: list[object] = []
        self.timeline: list[str] = []

    async def get(self, _model: type[object], _identity: object) -> object:
        return self.rule

    async def execute(self, _statement: object) -> object:
        rows = self.joined_rows

        class _Result:
            def all(self_inner) -> list[tuple[object, ...]]:
                return rows

        return _Result()

    async def scalar(self, _statement: object) -> object:
        return self.subject_count

    def add(self, value: object) -> None:
        _apply_flush_defaults(value)
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.timeline.append("commit")


def _sample_ownership_rule(
    *, organization_id: UUID, match_field: str, match_pattern: str
) -> OwnershipRule:
    return OwnershipRule(
        id=uuid4(),
        organization_id=organization_id,
        rule_key="stg-owner",
        display_name="Staging tables -> Jane",
        match_field=match_field,
        match_pattern=match_pattern,
        owner_type="INDIVIDUAL",
        owner_principal="jane@bank.example",
        status="ACTIVE",
        created_by="admin",
    )


async def test_apply_ownership_rule_matches_tables_by_wildcard_pattern_and_bulk_assigns() -> None:
    organization_id = uuid4()
    rule = _sample_ownership_rule(
        organization_id=organization_id, match_field="TABLE_NAME", match_pattern="stg_*"
    )
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=organization_id,
        catalog_id=uuid4(),
        name="raw",
        fingerprint="fp",
    )
    matching_table = MetadataTable(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=uuid4(),
        schema_id=schema.id,
        name="stg_payments",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    other_table = MetadataTable(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=uuid4(),
        schema_id=schema.id,
        name="fct_orders",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session = _OwnershipRuleSession(
        rule=rule,
        joined_rows=[(matching_table, schema, None, None), (other_table, schema, None, None)],
        subject_count=1,
    )
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )

    operation = await apply_ownership_rule(
        rule.id,
        context,
        session,  # type: ignore[arg-type]
    )

    assert operation.subject_ids == [str(matching_table.id)]
    assert operation.operation_type == "ASSIGN_OWNERSHIP"
    assert operation.subject_type == "TABLE"
    assert operation.parameters["owner_type"] == "INDIVIDUAL"
    assert operation.parameters["owner_principal"] == "jane@bank.example"
    assert operation.parameters["source_rule_id"] == str(rule.id)
    assert session.timeline == ["commit"]


async def test_apply_ownership_rule_rejects_when_no_tables_match() -> None:
    organization_id = uuid4()
    rule = _sample_ownership_rule(
        organization_id=organization_id, match_field="TABLE_NAME", match_pattern="nonexistent_*"
    )
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
        name="fct_orders",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session = _OwnershipRuleSession(
        rule=rule, joined_rows=[(table, schema, None, None)], subject_count=0
    )
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )

    with pytest.raises(HTTPException) as denied:
        await apply_ownership_rule(
            rule.id,
            context,
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 409


# --- GL-3: conflict detection and resolution --------------------------------


class _ConflictDetectionSession:
    def __init__(
        self, *, term_version_rows: list[tuple[object, object]], existing_conflicts: list[object]
    ) -> None:
        self.term_version_rows = term_version_rows
        self.existing_conflicts = existing_conflicts
        self.added: list[object] = []
        self.timeline: list[str] = []

    async def execute(self, _statement: object) -> object:
        rows = self.term_version_rows

        class _Result:
            def all(self_inner) -> list[tuple[object, object]]:
                return rows

        return _Result()

    async def scalars(self, _statement: object) -> object:
        values = self.existing_conflicts

        class _ScalarResult:
            def all(self_inner) -> list[object]:
                return values

        return _ScalarResult()

    def add(self, value: object) -> None:
        _apply_flush_defaults(value)
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.timeline.append("commit")


def _sample_glossary_term(*, organization_id: UUID, term_key: str) -> GlossaryTerm:
    return GlossaryTerm(
        id=uuid4(), organization_id=organization_id, term_key=term_key, lifecycle_status="ACTIVE"
    )


def _sample_term_version(
    *, organization_id: UUID, term_id: UUID, display_name: str, definition: str
) -> GlossaryTermVersion:
    return GlossaryTermVersion(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term_id,
        version=1,
        status="APPROVED",
        display_name=display_name,
        definition=definition,
        synonyms=[],
        created_by="admin",
    )


async def test_detect_glossary_conflicts_flags_synonym_collisions_with_diff_definitions() -> None:
    organization_id = uuid4()
    term_net = _sample_glossary_term(organization_id=organization_id, term_key="net_revenue")
    term_gross = _sample_glossary_term(organization_id=organization_id, term_key="gross_revenue")
    version_net = _sample_term_version(
        organization_id=organization_id,
        term_id=term_net.id,
        display_name="Revenue",
        definition="Net revenue after returns and discounts.",
    )
    version_gross = _sample_term_version(
        organization_id=organization_id,
        term_id=term_gross.id,
        display_name="Revenue",
        definition="Gross revenue before any deductions.",
    )
    session = _ConflictDetectionSession(
        term_version_rows=[(term_net, version_net), (term_gross, version_gross)],
        existing_conflicts=[],
    )
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )

    page = await detect_glossary_conflicts(
        organization_id,
        context,
        session,  # type: ignore[arg-type]
    )

    assert page.total == 1
    conflict = page.items[0]
    assert conflict.conflict_type == "SYNONYM_COLLISION"
    assert {conflict.position_a["term_id"], conflict.position_b["term_id"]} == {
        str(term_net.id),
        str(term_gross.id),
    }
    assert session.timeline == ["commit"]


async def test_detect_glossary_conflicts_ignores_same_label_when_definitions_agree() -> None:
    # Two terms sharing a display label but agreeing on definition are a legitimate
    # synonym relationship, not a competing-meaning conflict -- must not be flagged.
    organization_id = uuid4()
    term_a = _sample_glossary_term(organization_id=organization_id, term_key="churn")
    term_b = _sample_glossary_term(organization_id=organization_id, term_key="attrition")
    shared_definition = "Customers who cancel service within the measurement window."
    version_a = _sample_term_version(
        organization_id=organization_id,
        term_id=term_a.id,
        display_name="Churn Rate",
        definition=shared_definition,
    )
    version_b = _sample_term_version(
        organization_id=organization_id,
        term_id=term_b.id,
        display_name="Churn Rate",
        definition=shared_definition,
    )
    session = _ConflictDetectionSession(
        term_version_rows=[(term_a, version_a), (term_b, version_b)],
        existing_conflicts=[],
    )
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )

    page = await detect_glossary_conflicts(
        organization_id,
        context,
        session,  # type: ignore[arg-type]
    )

    assert page.total == 0
    # The scan itself is still audited (approved_terms_scanned / conflicts_created),
    # but no GlossaryConflict row is created for a same-definition label collision.
    assert not any(isinstance(value, GlossaryConflict) for value in session.added)


async def test_submit_conflict_resolution_retains_both_positions_pending_review() -> None:
    organization_id = uuid4()
    conflict = GlossaryConflict(
        id=uuid4(),
        organization_id=organization_id,
        term_id=uuid4(),
        conflict_type="SYNONYM_COLLISION",
        status="OPEN",
        position_a={
            "term_id": str(uuid4()),
            "display_name": "Revenue",
            "definition": "Net revenue after returns and discounts.",
        },
        position_b={
            "term_id": str(uuid4()),
            "display_name": "Revenue",
            "definition": "Gross revenue before any deductions.",
        },
        raised_by="steward",
    )
    original_position_a = dict(conflict.position_a)
    original_position_b = dict(conflict.position_b)
    session = _QueueSession(get_result=conflict, scalar_results=[])
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )
    body = GlossaryConflictResolution(
        resolution="ACCEPT_POSITION_A",
        resolved_definition="Net revenue after returns and discounts.",
        rationale="Finance owns and maintains this definition company-wide.",
    )

    review = await submit_conflict_resolution(
        conflict.id,
        body,
        context,
        session,  # type: ignore[arg-type]
    )

    assert conflict.status == "REVIEW_REQUIRED"
    assert conflict.proposed_resolution == "ACCEPT_POSITION_A"
    # Proposing a winner never deletes or overwrites the losing side's recorded
    # position -- both stay intact and visible through the review.
    assert conflict.position_a == original_position_a
    assert conflict.position_b == original_position_b
    assert review.object_type == "GLOSSARY_CONFLICT"
    assert session.timeline == ["commit"]


# --- GL-5: certification expiry actually stops a certification from counting


def test_active_certified_table_ids_excludes_expired_and_revoked_certifications() -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    current_table, expired_table, revoked_table = uuid4(), uuid4(), uuid4()
    certifications = [
        AssetCertification(
            id=uuid4(),
            organization_id=uuid4(),
            table_id=current_table,
            status="ACTIVE",
            rationale="Verified against the source ledger this quarter.",
            certified_by="steward",
            expires_at=now + timedelta(days=30),
        ),
        AssetCertification(
            id=uuid4(),
            organization_id=uuid4(),
            table_id=expired_table,
            status="ACTIVE",
            rationale="Verified last year; certification window has lapsed.",
            certified_by="steward",
            expires_at=now - timedelta(days=1),
        ),
        AssetCertification(
            id=uuid4(),
            organization_id=uuid4(),
            table_id=revoked_table,
            status="REVOKED",
            rationale="Superseded by a corrected definition.",
            certified_by="steward",
            expires_at=now + timedelta(days=30),
        ),
    ]

    result = active_certified_table_ids(certifications, now=now)

    assert result == {current_table}


# --- GL-8: term linkage inference (business-annotation label matching) ------


class _LinkProposalSession:
    """Answers the five sequential fetches `generate_glossary_link_proposals` makes:
    four `execute()` calls (term_rows, annotation_rows, link_rows, proposal_rows) --
    AT-6 moved the annotations fetch from `scalars()` to `execute()` since it now
    joins the current `MetadataBusinessAnnotationVersion` -- and one `scalars()`
    call (tables), each dispatched from its own queue in call order.
    """

    def __init__(
        self,
        *,
        term_rows: list[tuple[object, object]],
        annotations: list[tuple[object, object]],
        link_rows: list[tuple[object, ...]],
        proposal_rows: list[tuple[object, ...]],
        tables: list[object],
    ) -> None:
        self._execute_queue: list[list[tuple[object, ...]]] = [
            term_rows,
            annotations,
            link_rows,
            proposal_rows,
        ]
        self._scalars_queue: list[list[object]] = [tables]
        self.added: list[object] = []
        self.timeline: list[str] = []

    async def execute(self, _statement: object) -> object:
        rows = self._execute_queue.pop(0)

        class _Result:
            def all(self_inner) -> list[tuple[object, ...]]:
                return rows

        return _Result()

    async def scalars(self, _statement: object) -> object:
        values = self._scalars_queue.pop(0)

        class _ScalarResult:
            def all(self_inner) -> list[object]:
                return values

        return _ScalarResult()

    def add(self, value: object) -> None:
        _apply_flush_defaults(value)
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.timeline.append("commit")


def _sample_business_annotation(
    *, organization_id: UUID, table_id: UUID, business_name: str, synonyms: list[str]
) -> tuple[MetadataBusinessAnnotation, MetadataBusinessAnnotationVersion]:
    """AT-6: content lives on the (returned) current `MetadataBusinessAnnotationVersion`,
    never on `MetadataBusinessAnnotation` itself -- see `business_annotation_versions.py`.
    """
    annotation = MetadataBusinessAnnotation(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=uuid4(),
        table_id=table_id,
        domain_id=uuid4(),
        entity_id=uuid4(),
        source_proposal_id=uuid4(),
    )
    version = MetadataBusinessAnnotationVersion(
        id=uuid4(),
        organization_id=organization_id,
        annotation_id=annotation.id,
        version=1,
        status="APPROVED",
        business_name=business_name,
        business_description="Approved business context for this table.",
        table_role="FACT",
        grain_statement="One row per transaction.",
        synonyms=synonyms,
        suggested_questions=[],
        tags=[],
        confidence=0.9,
        approved_by="steward",
        approved_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    return annotation, version


def _sample_metadata_table(*, organization_id: UUID, name: str) -> MetadataTable:
    return MetadataTable(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=uuid4(),
        schema_id=uuid4(),
        name=name,
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fingerprint-" + name,
    )


async def test_generate_glossary_link_proposals_matches_labels_and_skips_existing_links() -> None:
    organization_id = uuid4()
    term = _sample_glossary_term(organization_id=organization_id, term_key="net_revenue")
    version = GlossaryTermVersion(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term.id,
        version=1,
        status="APPROVED",
        display_name="Net Revenue",
        definition="Revenue after returns and discounts.",
        synonyms=["Net Sales"],
        created_by="admin",
    )
    table_matched = _sample_metadata_table(organization_id=organization_id, name="fact_net_revenue")
    table_already_linked = _sample_metadata_table(
        organization_id=organization_id, name="fact_revenue_legacy"
    )
    # Primary match: annotation business_name exactly equals the approved display name.
    annotation_primary = _sample_business_annotation(
        organization_id=organization_id,
        table_id=table_matched.id,
        business_name="Net Revenue",
        synonyms=[],
    )
    # Synonym match, but this table already has a link to the same term -- must be
    # excluded even though the label matches.
    annotation_already_linked = _sample_business_annotation(
        organization_id=organization_id,
        table_id=table_already_linked.id,
        business_name="Legacy Revenue Table",
        synonyms=["Net Sales"],
    )
    session = _LinkProposalSession(
        term_rows=[(term, version)],
        annotations=[annotation_primary, annotation_already_linked],
        link_rows=[(table_already_linked.id, term.id)],
        proposal_rows=[],
        tables=[table_matched, table_already_linked],
    )
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )

    page = await generate_glossary_link_proposals(
        organization_id,
        GlossaryLinkProposalGenerate(),
        context,
        session,  # type: ignore[arg-type]
    )

    assert page.total == 1
    proposal = page.items[0]
    assert proposal.table_id == table_matched.id
    assert proposal.term_id == term.id
    assert proposal.confidence == 1.0
    assert proposal.evidence["matched_label"] == "Net Revenue"
    assert proposal.evidence["term_label_kind"] == "DISPLAY_NAME"
    created_proposals = [
        value for value in session.added if isinstance(value, GlossaryLinkProposal)
    ]
    assert len(created_proposals) == 1
    assert session.timeline == ["commit"]


async def test_generate_glossary_link_proposals_filters_matches_below_minimum_confidence() -> None:
    organization_id = uuid4()
    term = _sample_glossary_term(organization_id=organization_id, term_key="net_revenue")
    version = GlossaryTermVersion(
        id=uuid4(),
        organization_id=organization_id,
        term_id=term.id,
        version=1,
        status="APPROVED",
        display_name="Net Revenue",
        definition="Revenue after returns and discounts.",
        synonyms=["Net Sales"],
        created_by="admin",
    )
    table = _sample_metadata_table(organization_id=organization_id, name="fact_revenue_legacy")
    # Only a synonym match (confidence 0.92, not the 1.0 primary-label match) -- below
    # the caller-supplied minimum, so no proposal should be created for it.
    annotation = _sample_business_annotation(
        organization_id=organization_id,
        table_id=table.id,
        business_name="Legacy Revenue Table",
        synonyms=["Net Sales"],
    )
    session = _LinkProposalSession(
        term_rows=[(term, version)],
        annotations=[annotation],
        link_rows=[],
        proposal_rows=[],
        tables=[table],
    )
    context = SecurityContext(
        principal_id="steward",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )

    page = await generate_glossary_link_proposals(
        organization_id,
        GlossaryLinkProposalGenerate(minimum_confidence=0.95),
        context,
        session,  # type: ignore[arg-type]
    )

    assert page.total == 0
    assert not any(isinstance(value, GlossaryLinkProposal) for value in session.added)
    assert session.timeline == ["commit"]
