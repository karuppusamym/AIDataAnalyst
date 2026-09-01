"""AIDA Public Tool SDK (TL-5).

Lets a third-party developer *author* a governed-tool candidate offline --
parameter schema, SQL template with named placeholders, target dialect,
description, and example parameter values -- validate it locally, and
serialize it to the exact JSON body the platform's draft-submission endpoint
(``POST /v1/projects/{project_id}/tools``, see ``aida.tool_api``) expects.

Governance boundary
--------------------
This package can only ever produce or submit a **DRAFT**. There is no
``publish()``, ``approve()``, ``certify()`` or ``execute()`` anywhere on its
public surface, on purpose -- publication is maker-checker on the server
(``POST /tool-versions/{id}/submit`` requests review; a separate checker
role decides it through the governance-review endpoint in
``aida.semantic_api``). Nothing in this SDK calls, wraps, or reaches for
either of those. An author who wants their draft published still has to go
through that human review, exactly as if they had used the API directly.

Reuse, not a shadow copy
-------------------------
Local validation is not a reimplementation of the server's rules that could
drift from them -- it imports and runs the *same* code the server runs:

* ``aida.sql_guard.SqlGuard`` -- the read-only-query allowlist/denylist guard
  (single-statement, no mutation, no wildcard `SELECT *`, no forbidden
  functions, etc.).
* ``aida.tool_rendering`` -- placeholder extraction (``template_placeholders``)
  and parameter-bound rendering (``render_tool_sql``), the exact function the
  server uses both at draft-creation time (placeholder/parameter-name parity)
  and at tool-execution time (rendering a call's arguments into SQL).
* ``aida.schemas.GovernedToolVersionCreate`` / ``ToolParameterDefinition`` --
  the actual pydantic request models the draft-submission endpoint parses.
  Building one of these directly (rather than a hand-rolled dict matching its
  shape) means the SDK's serialized payload is *structurally* guaranteed to
  match the wire contract; it cannot drift from it without a `pydantic`
  validation error surfacing immediately in the SDK's own tests.

The only things this SDK does *not* and cannot mirror locally are checks
that require live server state: whether the given ``datasource_id`` exists
and its SQL dialect, whether a ``semantic_model_version_id`` is published,
and whether every table the SQL references is on that datasource's allowed
list. Those remain genuinely server-side (``tool_api.create_tool_version``)
and are surfaced back to the author as a normal HTTP error on submission.

Packaging note
--------------
This lives as a second top-level package (``sdk/aida_tool_sdk``) inside the
same repository/distribution as ``aida`` -- there is no existing "public SDK
as its own distribution" precedent yet in this repo (CN-6, the sibling
public-connector-SDK tracker row, is itself still TODO), so this follows the
existing multi-package-in-one-wheel shape (``src/aida``, ``src/atlas``)
rather than inventing packaging conventions this task does not need to
settle.

Known caveat, resolved 2026-09-01: ``aida.sql_guard`` (the SQL-safety guard)
always was dependency-light on its own -- pure ``sqlglot``, nothing else.
``aida.tool_rendering`` and ``aida.schemas`` -- reused here for placeholder/
rendering logic and for the exact wire-shape models -- still import
``aida.models`` -> ``aida.db`` -> ``atlas.platform.db`` for historical
reasons, but that chain no longer forces an ``AIDA_ENVIRONMENT``-validated
``Settings`` object or the platform's database driver dependencies (e.g.
``asyncpg``) to be importable merely to *import* this SDK: `atlas.platform.
db`'s engine, session-factory, and settings singletons are now built lazily,
on first real use (``get_engine``/``get_session_factory``, plus a module
``__getattr__`` for backward-compatible ``engine``/``session_factory``/
``settings`` attribute access), rather than constructed as a side effect of
module import. Nothing in this SDK's own code path ever touches any of
those, so importing it -- with or without `pytest` running, with or without
`AIDA_ENVIRONMENT` set -- no longer requires the ORM/DB stack, only the
dependencies its actual checks use (``pydantic``, ``sqlglot``, and
``httpx`` if submitting). "Dependency-light" here means exactly that: this
package declares no dependencies of its own beyond what reusing the real
platform code already requires, and its purely-local checks never touch the
network, a database, or any credential. ``httpx`` is the only optional
piece: imported lazily, only if
:class:`~aida_tool_sdk.client.ToolDraftClient` is actually used to submit.
"""

from aida_tool_sdk.candidate import ToolCandidate, parameter
from aida_tool_sdk.client import ToolDraftClient, ToolDraftSubmissionError
from aida_tool_sdk.errors import ToolCandidateValidationError
from aida_tool_sdk.serialization import candidate_to_draft_payload, candidate_to_wire_model
from aida_tool_sdk.validation import LocalValidationResult, validate_candidate

__all__ = [
    "LocalValidationResult",
    "ToolCandidate",
    "ToolCandidateValidationError",
    "ToolDraftClient",
    "ToolDraftSubmissionError",
    "candidate_to_draft_payload",
    "candidate_to_wire_model",
    "parameter",
    "validate_candidate",
]

__version__ = "0.1.0"
