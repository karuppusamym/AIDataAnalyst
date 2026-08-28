import pytest

from aida.schemas import ToolParameterDefinition
from aida.tool_rendering import ToolParameterError, render_tool_sql


def _definitions() -> list[ToolParameterDefinition]:
    return [
        ToolParameterDefinition(
            name="region",
            parameter_type="STRING",
            allowed_values=["NY", "TX"],
            max_length=2,
        ),
        ToolParameterDefinition(
            name="minimum_id",
            parameter_type="INTEGER",
            minimum=1,
            default=1,
        ),
    ]


def test_tool_renderer_escapes_string_values_as_ast_literals() -> None:
    rendered = render_tool_sql(
        "SELECT customer_id FROM retail.customer "
        "WHERE state_code = :region AND customer_id >= :minimum_id",
        dialect="postgres",
        definitions=_definitions(),
        values={"region": "NY"},
    )

    assert "'NY'" in rendered.sql
    assert "customer_id >= 1" in rendered.sql


def test_tool_renderer_rejects_injection_as_disallowed_value() -> None:
    with pytest.raises(ToolParameterError, match="not allowed"):
        render_tool_sql(
            "SELECT customer_id FROM retail.customer WHERE state_code = :region",
            dialect="postgres",
            definitions=[
                ToolParameterDefinition(
                    name="region",
                    parameter_type="STRING",
                    allowed_values=["NY", "TX"],
                )
            ],
            values={"region": "NY' OR TRUE --"},
        )


def test_tool_renderer_rejects_unknown_parameters() -> None:
    with pytest.raises(ToolParameterError, match="unknown parameters"):
        render_tool_sql(
            "SELECT customer_id FROM retail.customer WHERE state_code = :region",
            dialect="postgres",
            definitions=[ToolParameterDefinition(name="region", parameter_type="STRING")],
            values={"region": "NY", "extra": "value"},
        )


def test_tool_renderer_rejects_identifier_placeholders_via_template_contract() -> None:
    with pytest.raises(ToolParameterError, match="undeclared placeholders"):
        render_tool_sql(
            "SELECT customer_id FROM retail.customer WHERE state_code = :region",
            dialect="postgres",
            definitions=[],
            values={},
        )


def test_sensitive_parameter_cannot_persist_default() -> None:
    with pytest.raises(ValueError, match="sensitive parameters cannot define"):
        ToolParameterDefinition(
            name="customer_reference",
            parameter_type="STRING",
            sensitive=True,
            default="persisted-secret",
        )
