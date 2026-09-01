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

A known caveat, not introduced by this SDK: ``aida.sql_guard`` (the
SQL-safety guard) really is dependency-light on its own -- pure ``sqlglot``,
nothing else. But ``aida.tool_rendering`` and ``aida.schemas`` -- reused here
for placeholder/rendering logic and for the exact wire-shape models -- import
``aida.models`` -> ``aida.db`` -> ``atlas.platform.db`` for historical
reasons, which builds a SQLAlchemy async engine (though it does not connect
to it) and validates an ``AIDA_ENVIRONMENT``-driven ``Settings`` object at
*import* time. Concretely: importing this SDK outside of a `pytest` process
currently requires ``AIDA_ENVIRONMENT`` to be set (any of `development`,
`test`, `staging`, `production` -- matching how the platform itself is
configured) and the platform's full dependency set installed, even though
nothing here opens a database connection, calls the network, or reads that
settings object. That is real coupling worth unwinding in `aida.schemas`/
`aida.tool_rendering` someday so a public SDK can depend on the parameter
and rendering logic without the ORM stack behind it -- out of scope for this
change (`aida/models.py`/`schemas.py` are explicitly read-only for TL-5, and
untangling that import chain is a bigger refactor of its own). Until then,
"dependency-light" here means: this package declares no dependencies of its
own beyond what reusing the real platform code already requires, and its
purely-local checks never touch the network, a database, or any credential
-- not that importing it is as cheap as a from-scratch reimplementation
would be. ``httpx`` is the only optional piece: imported lazily, only if
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
