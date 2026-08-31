from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings
from aida.db import Base
from aida.main import app
from aida.model_gateway import (
    ApprovedModelRoute,
    DeterministicTestProvider,
    ProviderNeutralModelGateway,
)
from aida.models import MetadataColumn, MetadataConstraint, MetadataTable
from aida.semantic_inference import (
    SemanticEnrichmentBatchOutput,
    infer_table_semantics,
    model_enrich_batch,
    model_input,
    validate_model_suggestion,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """In-memory sqlite session for `model_enrich_batch`'s DB-backed kill-switch
    check (MG-2) -- same real-engine pattern as `test_bulk_governance_decisions.py`."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _table(name: str) -> MetadataTable:
    return MetadataTable(
        id=uuid4(),
        organization_id=uuid4(),
        datasource_id=uuid4(),
        schema_id=uuid4(),
        name=name,
        object_type="TABLE",
        fingerprint="a" * 64,
    )


def _column(table: MetadataTable, name: str, ordinal: int, classification: str) -> MetadataColumn:
    return MetadataColumn(
        id=uuid4(),
        organization_id=table.organization_id,
        table_id=table.id,
        name=name,
        ordinal_position=ordinal,
        physical_type="text",
        nullable=False,
        classification=classification,
        fingerprint="b" * 64,
    )


def test_rules_infer_customer_domain_and_primary_key_grain() -> None:
    table = _table("customers")
    columns = [
        _column(table, "customer_id", 1, "UNCLASSIFIED"),
        _column(table, "email_address", 2, "PII"),
    ]
    constraints = [
        MetadataConstraint(
            id=uuid4(),
            organization_id=table.organization_id,
            datasource_id=table.datasource_id,
            table_id=table.id,
            name="customers_pkey",
            constraint_type="PRIMARY_KEY",
            columns=["customer_id"],
            referenced_columns=[],
            fingerprint="c" * 64,
        )
    ]

    output = infer_table_semantics(
        table=table,
        schema_name="public",
        columns=columns,
        constraints=constraints,
    )

    assert output.domain_key == "CUSTOMER"
    assert output.entity_key == "CUSTOMER"
    assert "Customer Id" in output.grain_statement
    assert output.tool_blueprint.output_columns == ["customer_id"]
    assert "email_address" not in output.tool_blueprint.output_columns


def test_rules_infer_transaction_role_without_source_values() -> None:
    table = _table("card_transactions")
    output = infer_table_semantics(
        table=table,
        schema_name="banking",
        columns=[_column(table, "transaction_id", 1, "UNCLASSIFIED")],
        constraints=[],
    )

    assert output.domain_key == "PAYMENTS"
    assert output.table_role == "TRANSACTION"
    assert output.business_description.endswith("independent approval.")
    assert all("row value" not in evidence.lower() for evidence in output.evidence_ids)


def test_model_contract_rejects_extra_fields_and_sensitive_tool_columns() -> None:
    table = _table("customers")
    output = infer_table_semantics(
        table=table,
        schema_name="public",
        columns=[_column(table, "customer_id", 1, "UNCLASSIFIED")],
        constraints=[],
    )
    raw = {"tables": [output.model_dump(mode="json")], "unexpected": True}
    with pytest.raises(ValidationError):
        SemanticEnrichmentBatchOutput.model_validate(raw)

    changed = output.model_copy(
        update={
            "tool_blueprint": output.tool_blueprint.model_copy(
                update={"output_columns": ["email_address"]}
            )
        }
    )
    with pytest.raises(ValueError, match="sensitive"):
        validate_model_suggestion(
            changed,
            expected_table_id=table.id,
            safe_columns={"customer_id"},
        )


def test_business_semantics_api_contracts_are_exposed() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/datasources/{datasource_id}/semantic-inference-runs" in paths
    assert "/v1/datasources/{datasource_id}/metadata-enrichment-proposals" in paths
    assert "/v1/datasources/{datasource_id}/business-annotations" in paths
    assert "/v1/organizations/{organization_id}/business-map" in paths
    assert "/v1/metadata-enrichment-proposals/{proposal_id}/promote-tool" in paths


async def test_approved_model_batch_uses_strict_metadata_contract(
    session: AsyncSession,
) -> None:
    table = _table("customer_accounts")
    columns = [_column(table, "account_id", 1, "UNCLASSIFIED")]
    baseline = infer_table_semantics(
        table=table,
        schema_name="retail",
        columns=columns,
        constraints=[],
    )
    settings = Settings(
        model_generation_enabled=True,
        model_route="business-route",
        openai_api_key="test-key",
    )
    gateway = ProviderNeutralModelGateway(
        settings,
        providers={
            "OPENAI": DeterministicTestProvider({"tables": [baseline.model_dump(mode="json")]})
        },
    )
    route = ApprovedModelRoute(
        route_key="business-route",
        provider_type="OPENAI",
        model_id="approved-model",
        endpoint_alias="private-endpoint",
        credential_reference="env://OPENAI_API_KEY",
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
    )

    suggestions, evidence = await model_enrich_batch(
        session=session,
        organization_id=uuid4(),
        gateway=gateway,
        route=route,
        inputs=[
            model_input(
                baseline=baseline,
                table=table,
                schema_name="retail",
                columns=columns,
                constraints=[],
            )
        ],
    )

    assert suggestions[table.id].business_name == baseline.business_name
    assert evidence["route"] == "business-route"
    assert evidence["schema_name"] == "SemanticEnrichmentBatchOutput"
