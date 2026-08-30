"""Tests for the OpenAPI diff gate (tracker TS-4).

Two things are covered:

  1. The compatibility classifier (`diff_specs`) is exercised directly
     against small synthetic before/after OpenAPI fragments, so the
     breaking/non-breaking rules are verified without needing to touch the
     real (large) application spec.
  2. The committed baseline (`Docs/90-reference/openapi-baseline.json`)
     is asserted to match `app.openapi()` exactly, so the CI gate starts
     green and only turns red on a real, subsequent change.
"""

import json
from typing import Any

from aida.main import app
from scripts.openapi_diff import DEFAULT_BASELINE, breaking_changes, diff_specs


def _spec(paths: dict[str, Any], components: dict[str, Any] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "0.1.0"},
        "paths": paths,
    }
    if components is not None:
        spec["components"] = components
    return spec


# --------------------------------------------------------------------------
# Path / operation level
# --------------------------------------------------------------------------


def test_removed_path_is_breaking() -> None:
    old = _spec({"/v1/widgets": {"get": {"responses": {"200": {"description": "ok"}}}}})
    new = _spec({})

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "removed path '/v1/widgets'" in breaking[0].message


def test_added_path_is_not_breaking() -> None:
    old = _spec({})
    new = _spec({"/v1/widgets": {"get": {"responses": {"200": {"description": "ok"}}}}})

    changes = diff_specs(old, new)

    assert not breaking_changes(changes)
    assert any("added path" in c.message for c in changes)


def test_removed_operation_is_breaking() -> None:
    old = _spec(
        {
            "/v1/widgets": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "post": {"responses": {"201": {"description": "created"}}},
            }
        }
    )
    new = _spec({"/v1/widgets": {"get": {"responses": {"200": {"description": "ok"}}}}})

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "removed operation POST /v1/widgets" in breaking[0].message


def test_renamed_operation_is_a_removal_plus_an_addition() -> None:
    old = _spec({"/v1/widgets": {"get": {"responses": {"200": {"description": "ok"}}}}})
    new = _spec({"/v1/widgets": {"put": {"responses": {"200": {"description": "ok"}}}}})

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "removed operation GET /v1/widgets" in breaking[0].message
    assert any("added operation PUT /v1/widgets" in c.message for c in changes)


# --------------------------------------------------------------------------
# Request parameters and request-body fields
# --------------------------------------------------------------------------


def _op_with_request_schema(schema: dict[str, Any], required: bool = True) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": required,
            "content": {"application/json": {"schema": schema}},
        },
        "responses": {"200": {"description": "ok"}},
    }


def test_removed_required_request_field_is_breaking() -> None:
    old_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
        "required": ["name", "email"],
    }
    new_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    old = _spec({"/v1/widgets": {"post": _op_with_request_schema(old_schema)}})
    new = _spec({"/v1/widgets": {"post": _op_with_request_schema(new_schema)}})

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "email" in breaking[0].message
    assert "removed required request field" in breaking[0].message


def test_added_optional_request_field_is_not_breaking() -> None:
    old_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    new_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "nickname": {"type": "string"}},
        "required": ["name"],
    }
    old = _spec({"/v1/widgets": {"post": _op_with_request_schema(old_schema)}})
    new = _spec({"/v1/widgets": {"post": _op_with_request_schema(new_schema)}})

    changes = diff_specs(old, new)

    assert not breaking_changes(changes)
    assert any("nickname" in c.message and "added new request field" in c.message for c in changes)


def test_request_field_becoming_required_is_breaking() -> None:
    old_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
        "required": ["name"],
    }
    new_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
        "required": ["name", "email"],
    }
    old = _spec({"/v1/widgets": {"post": _op_with_request_schema(old_schema)}})
    new = _spec({"/v1/widgets": {"post": _op_with_request_schema(new_schema)}})

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "email" in breaking[0].message
    assert "became required" in breaking[0].message


def test_request_field_becoming_optional_is_not_breaking() -> None:
    old_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
        "required": ["name", "email"],
    }
    new_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
        "required": ["name"],
    }
    old = _spec({"/v1/widgets": {"post": _op_with_request_schema(old_schema)}})
    new = _spec({"/v1/widgets": {"post": _op_with_request_schema(new_schema)}})

    changes = diff_specs(old, new)

    assert not breaking_changes(changes)


def test_new_required_query_param_is_breaking() -> None:
    old = _spec(
        {"/v1/widgets": {"get": {"parameters": [], "responses": {"200": {"description": "ok"}}}}}
    )
    new = _spec(
        {
            "/v1/widgets": {
                "get": {
                    "parameters": [
                        {
                            "name": "tenant",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "'tenant'" in breaking[0].message
    assert "required parameter" in breaking[0].message


def test_new_optional_query_param_is_not_breaking() -> None:
    old = _spec(
        {"/v1/widgets": {"get": {"parameters": [], "responses": {"200": {"description": "ok"}}}}}
    )
    new = _spec(
        {
            "/v1/widgets": {
                "get": {
                    "parameters": [
                        {
                            "name": "sort",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )

    changes = diff_specs(old, new)

    assert not breaking_changes(changes)
    assert any("'sort'" in c.message for c in changes)


def test_removed_required_query_param_is_breaking() -> None:
    old = _spec(
        {
            "/v1/widgets": {
                "get": {
                    "parameters": [
                        {
                            "name": "tenant",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )
    new = _spec(
        {"/v1/widgets": {"get": {"parameters": [], "responses": {"200": {"description": "ok"}}}}}
    )

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "removed required parameter 'tenant'" in breaking[0].message


# --------------------------------------------------------------------------
# Response fields and status codes
# --------------------------------------------------------------------------


def _op_with_response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return {"responses": {"200": {"content": {"application/json": {"schema": schema}}}}}


def test_removed_response_field_is_breaking() -> None:
    old_schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "owner": {"type": "string"}},
    }
    new_schema = {"type": "object", "properties": {"id": {"type": "string"}}}
    old = _spec({"/v1/widgets/{id}": {"get": _op_with_response_schema(old_schema)}})
    new = _spec({"/v1/widgets/{id}": {"get": _op_with_response_schema(new_schema)}})

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "owner" in breaking[0].message
    assert "removed response field" in breaking[0].message


def test_added_response_field_is_not_breaking() -> None:
    old_schema = {"type": "object", "properties": {"id": {"type": "string"}}}
    new_schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "createdAt": {"type": "string"}},
    }
    old = _spec({"/v1/widgets/{id}": {"get": _op_with_response_schema(old_schema)}})
    new = _spec({"/v1/widgets/{id}": {"get": _op_with_response_schema(new_schema)}})

    changes = diff_specs(old, new)

    assert not breaking_changes(changes)
    assert any(
        "createdAt" in c.message and "added new response field" in c.message for c in changes
    )


def test_response_field_no_longer_guaranteed_is_breaking() -> None:
    old_schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
        "required": ["id", "status"],
    }
    new_schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
        "required": ["id"],
    }
    old = _spec({"/v1/widgets/{id}": {"get": _op_with_response_schema(old_schema)}})
    new = _spec({"/v1/widgets/{id}": {"get": _op_with_response_schema(new_schema)}})

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "status" in breaking[0].message
    assert "no longer guarantees" in breaking[0].message


def test_removed_status_code_is_breaking() -> None:
    old = _spec(
        {
            "/v1/widgets": {
                "post": {
                    "responses": {
                        "201": {"description": "created"},
                        "409": {"description": "conflict"},
                    }
                }
            }
        }
    )
    new = _spec({"/v1/widgets": {"post": {"responses": {"201": {"description": "created"}}}}})

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "removed response status code 409" in breaking[0].message


def test_added_status_code_is_not_breaking() -> None:
    old = _spec({"/v1/widgets": {"post": {"responses": {"201": {"description": "created"}}}}})
    new = _spec(
        {
            "/v1/widgets": {
                "post": {
                    "responses": {
                        "201": {"description": "created"},
                        "409": {"description": "conflict"},
                    }
                }
            }
        }
    )

    changes = diff_specs(old, new)

    assert not breaking_changes(changes)
    assert any("added response status code 409" in c.message for c in changes)


# --------------------------------------------------------------------------
# Enums and types (request or response, via $ref'd components too)
# --------------------------------------------------------------------------


def test_narrowed_enum_is_breaking() -> None:
    old_schema = {"type": "string", "enum": ["ACTIVE", "INACTIVE", "ARCHIVED"]}
    new_schema = {"type": "string", "enum": ["ACTIVE", "INACTIVE"]}
    old = _spec(
        {
            "/v1/widgets": {
                "post": _op_with_request_schema(
                    {"type": "object", "properties": {"status": old_schema}}
                )
            }
        }
    )
    new = _spec(
        {
            "/v1/widgets": {
                "post": _op_with_request_schema(
                    {"type": "object", "properties": {"status": new_schema}}
                )
            }
        }
    )

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "enum narrowed" in breaking[0].message
    assert "ARCHIVED" in breaking[0].message


def test_widened_enum_is_not_breaking() -> None:
    old_schema = {"type": "string", "enum": ["ACTIVE", "INACTIVE"]}
    new_schema = {"type": "string", "enum": ["ACTIVE", "INACTIVE", "ARCHIVED"]}
    old = _spec(
        {
            "/v1/widgets": {
                "post": _op_with_request_schema(
                    {"type": "object", "properties": {"status": old_schema}}
                )
            }
        }
    )
    new = _spec(
        {
            "/v1/widgets": {
                "post": _op_with_request_schema(
                    {"type": "object", "properties": {"status": new_schema}}
                )
            }
        }
    )

    changes = diff_specs(old, new)

    assert not breaking_changes(changes)
    assert any("enum widened" in c.message for c in changes)


def test_narrowed_enum_via_component_ref_is_breaking() -> None:
    components_old = {"schemas": {"Status": {"type": "string", "enum": ["ACTIVE", "ARCHIVED"]}}}
    components_new = {"schemas": {"Status": {"type": "string", "enum": ["ACTIVE"]}}}
    ref_schema = {"$ref": "#/components/schemas/Status"}
    old = _spec(
        {
            "/v1/widgets/{id}": {
                "get": _op_with_response_schema(
                    {"type": "object", "properties": {"status": ref_schema}}
                )
            }
        },
        components=components_old,
    )
    new = _spec(
        {
            "/v1/widgets/{id}": {
                "get": _op_with_response_schema(
                    {"type": "object", "properties": {"status": ref_schema}}
                )
            }
        },
        components=components_new,
    )

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "enum narrowed" in breaking[0].message


def test_changed_field_type_is_breaking() -> None:
    old_schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    new_schema = {"type": "object", "properties": {"count": {"type": "string"}}}
    old = _spec({"/v1/widgets/{id}": {"get": _op_with_response_schema(old_schema)}})
    new = _spec({"/v1/widgets/{id}": {"get": _op_with_response_schema(new_schema)}})

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "type changed from 'integer' to 'string'" in breaking[0].message


# --------------------------------------------------------------------------
# Composite scenario -- several changes at once, correctly split
# --------------------------------------------------------------------------


def test_mixed_breaking_and_non_breaking_changes_are_both_reported_and_correctly_classified() -> (
    None
):
    old = _spec(
        {
            "/v1/widgets": {
                "get": {"parameters": [], "responses": {"200": {"description": "ok"}}},
                "post": _op_with_request_schema(
                    {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    }
                ),
            }
        }
    )
    new = _spec(
        {
            "/v1/widgets": {
                "get": {
                    "parameters": [
                        {
                            "name": "sort",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
                # POST removed entirely -- breaking.
            },
            "/v1/gadgets": {"get": {"responses": {"200": {"description": "ok"}}}},
        }
    )

    changes = diff_specs(old, new)

    breaking = breaking_changes(changes)
    assert len(breaking) == 1
    assert "removed operation POST /v1/widgets" in breaking[0].message
    non_breaking_messages = [c.message for c in changes if c.severity == "info"]
    assert any("added path '/v1/gadgets'" in m for m in non_breaking_messages)
    assert any("'sort'" in m for m in non_breaking_messages)


# --------------------------------------------------------------------------
# Baseline freshness -- the gate must start green.
# --------------------------------------------------------------------------


def test_committed_baseline_matches_current_app_openapi_output() -> None:
    assert DEFAULT_BASELINE.exists(), (
        f"OpenAPI baseline missing at {DEFAULT_BASELINE}; regenerate with "
        "`uv run python scripts/openapi_diff.py --accept-baseline`."
    )
    baseline = json.loads(DEFAULT_BASELINE.read_text())
    current = json.loads(json.dumps(app.openapi()))

    assert baseline == current, (
        "Committed OpenAPI baseline is stale relative to app.openapi(). "
        "Regenerate it with `uv run python scripts/openapi_diff.py --accept-baseline` "
        "after confirming any differences are intentional and reviewed."
    )


def test_diff_of_baseline_against_itself_has_no_breaking_changes() -> None:
    baseline = json.loads(DEFAULT_BASELINE.read_text())
    current = json.loads(json.dumps(app.openapi()))

    changes = diff_specs(baseline, current)

    assert not breaking_changes(changes)


def test_default_baseline_path_lives_under_docs_reference() -> None:
    assert DEFAULT_BASELINE.parts[-3:] == ("Docs", "90-reference", "openapi-baseline.json")
