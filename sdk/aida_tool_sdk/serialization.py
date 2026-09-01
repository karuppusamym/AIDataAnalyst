"""Serialize a `ToolCandidate` to the exact draft-submission wire shape.

`candidate_to_wire_model` builds an `aida.schemas.GovernedToolVersionCreate`
directly from the candidate -- the real pydantic model
`POST /v1/projects/{project_id}/tools` (`aida.tool_api.create_tool_version`)
parses its request body into -- rather than hand-assembling a dict that
merely resembles that shape. That means:

* every field name, type, and length/pattern constraint the server enforces
  is enforced here too, automatically, by the same class;
* the model's own cross-field validation (`validate_unique_names_and_roles`)
  runs, so duplicate parameter names or duplicate/blank roles are caught
  locally with the same error the server would raise;
* if the server's request schema ever changes, this SDK's serialization
  changes with it the next time it's run against an updated `aida` --
  there is no second copy of the shape to fall out of sync.

`candidate_to_draft_payload` then calls `.model_dump(mode="json")` on that
model, which is the literal JSON body an HTTP client should POST.
"""

from typing import Any
from uuid import UUID

from pydantic import ValidationError

from aida.schemas import GovernedToolVersionCreate
from aida_tool_sdk.candidate import ToolCandidate
from aida_tool_sdk.errors import ToolCandidateValidationError


def candidate_to_wire_model(candidate: ToolCandidate) -> GovernedToolVersionCreate:
    """Build the real `GovernedToolVersionCreate` request model from a candidate.

    Raises:
        ToolCandidateValidationError: if the candidate fails the wire
            schema's own pydantic validation (bad slug pattern, duplicate
            parameter names, empty ``allowed_roles``, etc).
    """
    try:
        return GovernedToolVersionCreate(
            slug=candidate.slug,
            name=candidate.name,
            description=candidate.description,
            datasource_id=candidate.datasource_id,
            semantic_model_version_id=candidate.semantic_model_version_id,
            sql_template=candidate.sql_template,
            parameters=list(candidate.parameters),
            allowed_roles=list(candidate.allowed_roles),
        )
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        ]
        raise ToolCandidateValidationError(errors) from exc


def candidate_to_draft_payload(candidate: ToolCandidate) -> dict[str, Any]:
    """The exact JSON body `POST /v1/projects/{project_id}/tools` expects.

    Raises:
        ToolCandidateValidationError: see `candidate_to_wire_model`.
    """
    payload: dict[str, Any] = candidate_to_wire_model(candidate).model_dump(mode="json")
    return payload


def draft_submission_url(base_url: str, project_id: UUID) -> str:
    """The draft-submission endpoint's path for `project_id`, joined onto
    `base_url`. Shared by `ToolDraftClient` and available standalone for
    callers building their own HTTP request.
    """
    return f"{base_url.rstrip('/')}/v1/projects/{project_id}/tools"
