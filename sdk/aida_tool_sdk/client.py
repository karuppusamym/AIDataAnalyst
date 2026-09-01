"""HTTP submission of a validated `ToolCandidate` as a DRAFT -- nothing else.

`httpx` is imported lazily inside `ToolDraftClient.submit_draft` so that
pure local validation/serialization (everything else in this package) never
requires it to be installed at all.
"""

from typing import Any
from uuid import UUID

from aida_tool_sdk.candidate import ToolCandidate
from aida_tool_sdk.serialization import candidate_to_draft_payload, draft_submission_url
from aida_tool_sdk.validation import validate_candidate


class ToolDraftSubmissionError(RuntimeError):
    """Raised when the server rejects a draft submission (a non-2xx response
    from `POST /v1/projects/{project_id}/tools`)."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"draft submission rejected ({status_code}): {detail}")


class ToolDraftClient:
    """Submits a locally-validated `ToolCandidate` as a DRAFT.

    This is the SDK's *only* network-writing surface, and it does exactly
    one thing: POST to the draft-submission endpoint
    (`aida.tool_api.create_tool_version`), which always creates a new
    `GovernedToolVersion` in `DRAFT` status.

    There is deliberately no method here, or anywhere else in this package,
    that requests review, decides a review, certifies, or executes a tool --
    those all require roles and a maker-checker step this SDK is not a party
    to (`POST /tool-versions/{id}/submit` and the governance-review decision
    endpoint in `aida.semantic_api`). An author who wants their draft
    published still has to submit it for review and get it approved through
    the platform itself, by a human with the appropriate role.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url
        self._headers = dict(headers or {})
        if token:
            self._headers.setdefault("Authorization", f"Bearer {token}")
        self._timeout = timeout

    def submit_draft(self, project_id: UUID, candidate: ToolCandidate) -> dict[str, Any]:
        """Validate `candidate` locally, then POST it as a new DRAFT tool
        version under `project_id`.

        Returns the server's parsed JSON response (a
        `GovernedToolVersionRead`-shaped dict, always `status == "DRAFT"`
        for a brand-new tool) on success.

        Raises:
            ToolCandidateValidationError: local validation failed; nothing
                was sent over the network.
            ToolDraftSubmissionError: the server rejected the submission
                (e.g. unknown/unauthorized datasource, a table outside the
                datasource's allowlist, or a slug/version conflict).
        """
        validate_candidate(candidate, raise_on_error=True)
        payload = candidate_to_draft_payload(candidate)

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "submitting a draft over HTTP requires httpx; install it with "
                "`pip install aida-tool-sdk[http]` (or plain `pip install httpx`)"
            ) from exc

        url = draft_submission_url(self._base_url, project_id)
        response = httpx.post(url, json=payload, headers=self._headers, timeout=self._timeout)
        if response.status_code >= 400:
            detail: Any = response.text
            try:
                detail = response.json().get("detail", detail)
            except ValueError:
                pass
            raise ToolDraftSubmissionError(response.status_code, str(detail))
        result: dict[str, Any] = response.json()
        return result
