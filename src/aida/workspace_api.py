"""Backward-compatible re-export shim.

Canonical location: `atlas.modules.identity_tenancy.router`, moved under
ST-07 Commit C for the identity_tenancy bounded context (analog of the
observability_audit module's own Commit C) on 2026-09-03. Every existing
`from aida.workspace_api import ...` caller keeps working unchanged.

Externally-used symbols at the time of the move:

* `router` -- `aida.main` (mounts it on the app).

No test imports a handler function from this module directly at the time of
the move (checked via `grep -rn "from aida.workspace_api import" src/ tests/`),
but every top-level route handler is re-exported anyway -- same precaution
the catalog and observability_audit shims took -- so a future test that
wants to bypass HTTP for one of them doesn't have to change import paths
first:

* `create_workspace_route`
* `list_workspaces`
* `add_member`
* `list_members`
* `request_source_binding`
* `decide_source_binding`
* `list_source_bindings`
* `create_business_node`
* `get_business_tree`
* `create_business_assignment`
* `get_rollup`
* `list_access_policies`
* `create_access_policy`
* `probe_authorization`
* `simulate_authorization`

New code should import from `atlas.modules.identity_tenancy.router`
directly.
"""

from atlas.modules.identity_tenancy.router import (
    add_member,
    create_access_policy,
    create_business_assignment,
    create_business_node,
    create_workspace_route,
    decide_source_binding,
    get_business_tree,
    get_rollup,
    list_access_policies,
    list_members,
    list_source_bindings,
    list_workspaces,
    probe_authorization,
    request_source_binding,
    router,
    simulate_authorization,
)

__all__ = [
    "router",
    "create_workspace_route",
    "list_workspaces",
    "add_member",
    "list_members",
    "request_source_binding",
    "decide_source_binding",
    "list_source_bindings",
    "create_business_node",
    "get_business_tree",
    "create_business_assignment",
    "get_rollup",
    "list_access_policies",
    "create_access_policy",
    "probe_authorization",
    "simulate_authorization",
]
