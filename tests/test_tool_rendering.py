import math
from typing import Any

import pytest

from aida.schemas import ToolParameterDefinition
from aida.tool_rendering import (
    ToolParameterCode,
    ToolParameterError,
    ToolParameterIssue,
    render_tool_sql,
)


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


# ---------------------------------------------------------------------------
# R11-SQL01 hygiene: the renderer names its refusals
# ---------------------------------------------------------------------------

_ONE = "SELECT o.order_id FROM retail.orders AS o WHERE o.order_id = :order_id"
_TWO = "SELECT o.order_id FROM retail.orders AS o WHERE o.a = :alpha AND o.b = :beta"


def _refusal(
    template: str, definitions: list[ToolParameterDefinition], values: dict[str, Any]
) -> ToolParameterError:
    with pytest.raises(ToolParameterError) as raised:
        render_tool_sql(template, dialect="postgres", definitions=definitions, values=values)
    return raised.value


def _definition(parameter_type: str = "STRING", **extra: Any) -> ToolParameterDefinition:
    return ToolParameterDefinition(name="order_id", parameter_type=parameter_type, **extra)


_UNSUPPORTED = ToolParameterDefinition.model_construct(
    name="order_id", parameter_type="DECIMAL", required=True
)

#: (template, definitions, values, the issues it must carry, the message it has always had).
#: Every message here is what Studio, the tool API and tool certification show and what their
#: tests match; the codes are new, the wording is not.
REFUSALS: list[tuple[str, list[ToolParameterDefinition], dict[str, Any], list[Any], str]] = [
    (
        _ONE,
        [],
        {},
        [(ToolParameterCode.UNDECLARED_PLACEHOLDER, ("order_id",))],
        "undeclared placeholders: order_id",
    ),
    (
        _TWO,
        [],
        {},
        [(ToolParameterCode.UNDECLARED_PLACEHOLDER, ("alpha", "beta"))],
        "undeclared placeholders: alpha, beta",
    ),
    (
        _ONE,
        [_definition(), ToolParameterDefinition(name="region", parameter_type="STRING")],
        {},
        [(ToolParameterCode.UNUSED_DEFINITION, ("region",))],
        "unused parameter definitions: region",
    ),
    (
        _ONE,
        [ToolParameterDefinition(name="region", parameter_type="STRING")],
        {},
        [
            (ToolParameterCode.UNDECLARED_PLACEHOLDER, ("order_id",)),
            (ToolParameterCode.UNUSED_DEFINITION, ("region",)),
        ],
        "undeclared placeholders: order_id; unused parameter definitions: region",
    ),
    (
        _ONE,
        [_definition()],
        {"order_id": "O-1", "extra": "x", "more": "y"},
        [(ToolParameterCode.UNKNOWN_PARAMETER, ("extra", "more"))],
        "unknown parameters: extra, more",
    ),
    (
        _ONE,
        [_definition()],
        {},
        [(ToolParameterCode.REQUIRED_MISSING, ("order_id",))],
        "required parameter is missing: order_id",
    ),
    (
        _ONE,
        [_definition()],
        {"order_id": None},
        [(ToolParameterCode.REQUIRED_NULL, ("order_id",))],
        "required parameter is null: order_id",
    ),
    (
        _ONE,
        [_definition()],
        {"order_id": 5},
        [(ToolParameterCode.NOT_A_STRING, ("order_id",))],
        "parameter must be a string: order_id",
    ),
    (
        _ONE,
        [_definition("INTEGER")],
        {"order_id": "5"},
        [(ToolParameterCode.NOT_AN_INTEGER, ("order_id",))],
        "parameter must be an integer: order_id",
    ),
    (
        _ONE,
        [_definition("INTEGER")],
        {"order_id": True},
        [(ToolParameterCode.NOT_AN_INTEGER, ("order_id",))],
        "parameter must be an integer: order_id",
    ),
    (
        _ONE,
        [_definition("NUMBER")],
        {"order_id": "1.5"},
        [(ToolParameterCode.NOT_NUMERIC, ("order_id",))],
        "parameter must be numeric: order_id",
    ),
    (
        _ONE,
        [_definition("NUMBER")],
        {"order_id": math.inf},
        [(ToolParameterCode.NOT_FINITE, ("order_id",))],
        "parameter must be finite: order_id",
    ),
    (
        _ONE,
        [_definition("BOOLEAN")],
        {"order_id": 1},
        [(ToolParameterCode.NOT_A_BOOLEAN, ("order_id",))],
        "parameter must be boolean: order_id",
    ),
    (
        _ONE,
        [_definition("DATE")],
        {"order_id": 20240105},
        [(ToolParameterCode.NOT_AN_ISO_DATE, ("order_id",))],
        "parameter must be an ISO date string: order_id",
    ),
    (
        _ONE,
        [_definition("DATE")],
        {"order_id": "2024-13-01"},
        [(ToolParameterCode.NOT_AN_ISO_DATE, ("order_id",))],
        "parameter must be an ISO date string: order_id",
    ),
    (
        _ONE,
        [_definition(max_length=3)],
        {"order_id": "toolong"},
        [(ToolParameterCode.TOO_LONG, ("order_id",))],
        "parameter exceeds max_length: order_id",
    ),
    (
        _ONE,
        [_UNSUPPORTED],
        {"order_id": 1},
        [(ToolParameterCode.UNSUPPORTED_TYPE, ("DECIMAL",))],
        "unsupported parameter type: DECIMAL",
    ),
    (
        _ONE,
        [_definition(allowed_values=["A", "B"])],
        {"order_id": "C"},
        [(ToolParameterCode.NOT_ALLOWED, ("order_id",))],
        "parameter value is not allowed: order_id",
    ),
    (
        _ONE,
        [_definition("INTEGER", minimum=10)],
        {"order_id": 9},
        [(ToolParameterCode.BELOW_MINIMUM, ("order_id",))],
        "parameter is below minimum: order_id",
    ),
    (
        _ONE,
        [_definition("INTEGER", maximum=10)],
        {"order_id": 11},
        [(ToolParameterCode.ABOVE_MAXIMUM, ("order_id",))],
        "parameter exceeds maximum: order_id",
    ),
]


@pytest.mark.parametrize(
    ("template", "definitions", "values", "issues", "message"),
    REFUSALS,
    ids=[f"{index}-{row[-1]}" for index, row in enumerate(REFUSALS)],
)
def test_every_refusal_carries_its_codes_and_keeps_its_wording(
    template: str,
    definitions: list[ToolParameterDefinition],
    values: dict[str, Any],
    issues: list[tuple[ToolParameterCode, tuple[str, ...]]],
    message: str,
) -> None:
    refusal = _refusal(template, definitions, values)

    assert [(issue.code, issue.names) for issue in refusal.issues] == issues
    assert refusal.code == issues[0][0]
    assert str(refusal) == message


def test_the_table_covers_every_code_the_renderer_can_raise() -> None:
    raised = {code for row in REFUSALS for code, _names in row[3]}

    assert raised == set(ToolParameterCode) - {ToolParameterCode.UNCLASSIFIED}


def test_a_refusal_raised_with_only_a_message_is_unclassified() -> None:
    refusal = ToolParameterError("something the renderer does not have a code for")

    assert refusal.code is ToolParameterCode.UNCLASSIFIED
    assert refusal.issues == (ToolParameterIssue(ToolParameterCode.UNCLASSIFIED),)
    assert str(refusal) == "something the renderer does not have a code for"


def test_a_code_is_its_own_name_and_never_the_wording() -> None:
    """Codes are the contract; the wording is free to improve. Nothing may key on it."""
    for code in ToolParameterCode:
        assert str(code) == code.name


def test_a_code_never_carries_the_value_it_met() -> None:
    value = "VALUE-SENTINEL-2207"
    refusal = _refusal(_ONE, [_definition("INTEGER")], {"order_id": value})

    assert value not in str(refusal)
    assert value not in repr(refusal.issues)
