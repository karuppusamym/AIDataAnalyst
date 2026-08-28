from uuid import uuid4

import pytest
from pydantic import ValidationError

from aida.schemas import GovernanceDecisionRequest, SemanticMetricCreate


def test_non_count_metric_requires_measure_column() -> None:
    with pytest.raises(ValidationError, match="measure_column_id"):
        SemanticMetricCreate(
            slug="total_balance",
            name="Total Balance",
            description="Total current account balance",
            aggregation="SUM",
            grain="account",
            source_table_id=uuid4(),
        )


def test_count_metric_can_omit_measure_column() -> None:
    metric = SemanticMetricCreate(
        slug="customer_count",
        name="Customer Count",
        description="Count of customers",
        aggregation="COUNT",
        grain="customer",
        source_table_id=uuid4(),
    )

    assert metric.measure_column_id is None


def test_duplicate_dimensions_are_rejected() -> None:
    dimension_id = uuid4()
    with pytest.raises(ValidationError, match="must be unique"):
        SemanticMetricCreate(
            slug="customer_count",
            name="Customer Count",
            description="Count of customers",
            aggregation="COUNT",
            grain="customer",
            source_table_id=uuid4(),
            allowed_dimension_column_ids=[dimension_id, dimension_id],
        )


def test_rejection_requires_reason() -> None:
    with pytest.raises(ValidationError, match="reason is required"):
        GovernanceDecisionRequest(decision="REJECT")
