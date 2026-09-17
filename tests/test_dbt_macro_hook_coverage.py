"""Finding F06.4: dbt macro and hook presence, reported as bounded coverage.

The gap the review names: `dbt_artifacts.SUPPORTED_RESOURCE_TYPES` excludes
`macro`, the manifest keys the parser reads omit `macros` entirely, and a
repo-wide search for `macro`, `hook`, `on-run`, `pre_hook` or `post_hook` across
the four dbt modules returned nothing. So a project that runs four hooks and
thirty-seven macros ingested exactly like one that runs none, and nothing said
so.

What this proves is deliberately *reporting*, not resolution. Macro expansion is
not resolved into lineage here, and claiming it was would be the precise failure
F06.4 warns about. What is proven is that a macro-produced model and a hooked
project are **visible** as bounded rather than silently counted as understood,
and that nothing about either is stored beyond a count -- a hook body is
arbitrary SQL that routinely carries literal values (INV-6).
"""

from __future__ import annotations

from typing import Any

import pytest

from aida.capability_states import CapabilityState
from aida.dbt_artifacts import (
    HOOK_RESOURCE_TYPE,
    MANIFEST_COLLECTION_KEYS,
    SUPPORTED_RESOURCE_TYPES,
    ParsedDbtResource,
    parse_dbt_manifest,
)
from aida.engine_capability_matrix import build_engine_capability_matrix


def _manifest(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "metadata": {"dbt_schema_version": "v12", "dbt_version": "1.9.0"},
        "nodes": {},
        "sources": {},
        "exposures": {},
        "metrics": {},
        "semantic_models": {},
        "saved_queries": {},
        "macros": {},
    }
    manifest.update(overrides)
    return manifest


def _model(
    unique_id: str = "model.shop.customers",
    *,
    macros: list[str] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "resource_type": "model",
        "package_name": "shop",
        "name": "customers",
        "schema": "analytics",
        "database": "warehouse",
        "compiled_code": "SELECT id FROM analytics.raw_customers",
        "depends_on": {"nodes": [], "macros": macros or []},
        "config": config or {"materialized": "table"},
        "columns": {},
        "tags": [],
    }


# ---------------------------------------------------------------------------
# Macros.
# ---------------------------------------------------------------------------


def test_macro_is_still_not_an_ingested_resource_type() -> None:
    """Pins the boundary of what was built. Adding `macro` here would change
    what is stored, and the review asks for coverage reporting, not ingestion."""
    assert "macro" not in SUPPORTED_RESOURCE_TYPES
    assert "macros" not in MANIFEST_COLLECTION_KEYS


def test_a_project_reports_how_many_macros_it_declares() -> None:
    manifest = _manifest(
        nodes={"model.shop.customers": _model()},
        macros={
            "macro.shop.grant_select": {"name": "grant_select"},
            "macro.dbt.star": {"name": "star"},
        },
    )
    artifact = parse_dbt_manifest(manifest, "postgres")
    assert artifact.macro_count == 2
    assert len(artifact.resources) == 1, "the macros themselves are still not resources"


def test_a_model_whose_sql_a_macro_produced_is_visibly_bounded() -> None:
    """The compiled SQL is real lineage -- the expansion already happened -- and
    the count is what says the macro behind it is not modelled."""
    manifest = _manifest(
        nodes={
            "model.shop.customers": _model(macros=["macro.dbt.star", "macro.shop.pii"]),
            "model.shop.orders": {
                **_model("model.shop.orders"),
                "name": "orders",
            },
        },
        macros={"macro.dbt.star": {}, "macro.shop.pii": {}},
    )
    artifact = parse_dbt_manifest(manifest, "postgres")
    by_id = {resource.unique_id: resource for resource in artifact.resources}
    assert by_id["model.shop.customers"].macro_dependency_count == 2
    assert by_id["model.shop.orders"].macro_dependency_count == 0
    assert artifact.macro_dependent_resource_count == 1
    # And the SQL is still parsed: a macro-produced model is not downgraded.
    assert by_id["model.shop.customers"].sql_parse_status == "PARSED"


def test_a_manifest_with_no_macro_key_reports_none_rather_than_failing() -> None:
    manifest = _manifest(nodes={"model.shop.customers": _model()})
    del manifest["macros"]
    artifact = parse_dbt_manifest(manifest, "postgres")
    assert artifact.macro_count == 0
    assert artifact.macro_dependent_resource_count == 0


def test_a_malformed_macros_key_does_not_break_ingestion() -> None:
    artifact = parse_dbt_manifest(
        _manifest(nodes={"model.shop.customers": _model()}, macros=["not", "an", "object"]),
        "postgres",
    )
    assert artifact.macro_count == 0


# ---------------------------------------------------------------------------
# Hooks.
# ---------------------------------------------------------------------------


def test_project_hooks_are_counted_and_still_not_parsed() -> None:
    """dbt compiles `on-run-start` / `on-run-end` into `operation` nodes. They
    are skipped, so any relation they read or write is invisible to lineage --
    and the count is what makes that visible instead of silent."""
    manifest = _manifest(
        nodes={
            "model.shop.customers": _model(),
            "operation.shop.shop-on-run-start-0": {
                "resource_type": HOOK_RESOURCE_TYPE,
                "package_name": "shop",
                "name": "shop-on-run-start-0",
                "compiled_code": "INSERT INTO audit.run_log VALUES (1)",
                "config": {},
            },
            "operation.shop.shop-on-run-end-0": {
                "resource_type": HOOK_RESOURCE_TYPE,
                "package_name": "shop",
                "name": "shop-on-run-end-0",
                "compiled_code": "ANALYZE analytics.customers",
                "config": {},
            },
        }
    )
    artifact = parse_dbt_manifest(manifest, "postgres")
    assert artifact.project_hook_count == 2
    assert HOOK_RESOURCE_TYPE not in SUPPORTED_RESOURCE_TYPES
    assert {resource.unique_id for resource in artifact.resources} == {
        "model.shop.customers"
    }
    # Nothing from the hook's own SQL reached storage.
    assert all(
        "audit.run_log" not in (resource.compiled_sql_redacted or "")
        for resource in artifact.resources
    )


@pytest.mark.parametrize(
    ("config", "pre", "post"),
    [
        ({"pre-hook": ["A", "B"], "post-hook": ["C"]}, 2, 1),
        ({"pre_hook": "A", "post_hook": "B"}, 1, 1),
        ({"post-hook": [{"sql": "GRANT SELECT ON x TO y", "transaction": True}]}, 0, 1),
        ({}, 0, 0),
    ],
)
def test_model_hooks_are_counted_under_either_spelling(
    config: dict[str, Any], pre: int, post: int
) -> None:
    artifact = parse_dbt_manifest(
        _manifest(
            nodes={
                "model.shop.customers": _model(
                    config={"materialized": "table", **config}
                )
            }
        ),
        "postgres",
    )
    resource = artifact.resources[0]
    assert resource.pre_hook_count == pre
    assert resource.post_hook_count == post
    assert artifact.model_hook_count == pre + post


def test_a_hooks_own_sql_is_never_stored() -> None:
    """INV-6. A hook body is arbitrary SQL and routinely carries literals, so
    the only thing taken from it is how many there are."""
    artifact = parse_dbt_manifest(
        _manifest(
            nodes={
                "model.shop.customers": _model(
                    config={
                        "materialized": "table",
                        "post-hook": ["DELETE FROM audit WHERE ssn = '123-45-6789'"],
                    }
                )
            }
        ),
        "postgres",
    )
    resource = artifact.resources[0]
    assert resource.post_hook_count == 1
    for value in (
        resource.compiled_sql_redacted or "",
        str(resource.extra_metadata),
    ):
        assert "123-45-6789" not in value
        assert "audit" not in value


def test_the_new_fields_default_so_an_existing_caller_is_unaffected() -> None:
    """`ParsedDbtResource` is constructed by keyword in several places. The
    coverage fields default to zero so nothing outside `dbt_artifacts` has to
    change to keep working."""
    resource = ParsedDbtResource(
        unique_id="model.shop.x",
        resource_type="MODEL",
        package_name="shop",
        name="x",
        database_name=None,
        schema_name=None,
        relation_name=None,
        materialization=None,
        original_file_path=None,
        description=None,
        compiled_sql_hash=None,
        compiled_sql_redacted=None,
        sql_parse_status="NOT_PRESENT",
        column_names=[],
        tags=[],
        depends_on_unique_ids=[],
    )
    assert resource.macro_dependency_count == 0
    assert resource.pre_hook_count == 0
    assert resource.post_hook_count == 0


# ---------------------------------------------------------------------------
# And the matrix says so.
# ---------------------------------------------------------------------------


def test_the_capability_matrix_publishes_the_dbt_bound() -> None:
    """The review asks for bounded coverage *reporting*. A count nobody
    publishes is not a report, so the matrix carries the same four aspects."""
    coverage = {row.aspect: row for row in build_engine_capability_matrix().dbt_coverage}
    assert coverage["macro definitions"].state == CapabilityState.UNSUPPORTED.value
    assert (
        coverage["project hooks (on-run-start / on-run-end)"].state
        == CapabilityState.UNSUPPORTED.value
    )
    assert (
        coverage["model hooks (pre_hook / post_hook)"].state
        == CapabilityState.UNSUPPORTED.value
    )
    macro_model = coverage["a model whose SQL a macro produced"]
    assert macro_model.state == CapabilityState.PARTIAL.value
    assert "macro_dependency_count" in macro_model.evidence


def test_the_matrix_never_claims_macro_expansion_is_resolved() -> None:
    """The failure F06.4 names, asserted directly: no dbt aspect may read
    SUPPORTED, because none of them is."""
    for row in build_engine_capability_matrix().dbt_coverage:
        assert row.state != CapabilityState.SUPPORTED.value, row
