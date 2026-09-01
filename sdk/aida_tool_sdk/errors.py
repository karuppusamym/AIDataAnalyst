"""Error types raised by the SDK's local validation and serialization paths."""


class ToolCandidateValidationError(ValueError):
    """Raised when a :class:`~aida_tool_sdk.candidate.ToolCandidate` fails local
    validation and the caller asked for a hard failure (``validate_candidate(...,
    raise_on_error=True)``, or ``candidate_to_draft_payload``/``ToolDraftClient``,
    which both validate before doing anything else).

    Carries the same human-readable error strings as
    :attr:`aida_tool_sdk.validation.LocalValidationResult.errors`.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) or "tool candidate failed local validation")
