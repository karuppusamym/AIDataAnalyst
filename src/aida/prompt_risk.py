import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Literal

PROMPT_RISK_CLASSIFIER_VERSION = "deterministic-prompt-risk-v1"


@dataclass(frozen=True, slots=True)
class PromptRiskAssessment:
    decision: Literal["ALLOW", "BLOCK"]
    score: float
    reason_codes: list[str]
    signal_count: int
    classifier_version: str = PROMPT_RISK_CLASSIFIER_VERSION

    def evidence(self) -> dict[str, object]:
        """Return value-free evidence; matched prompt fragments are intentionally excluded."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class _RiskSignal:
    reason_code: str
    pattern: re.Pattern[str]
    weight: float


def _signal(reason_code: str, pattern: str, weight: float) -> _RiskSignal:
    return _RiskSignal(reason_code, re.compile(pattern, re.IGNORECASE), weight)


RISK_SIGNALS = (
    _signal(
        "INSTRUCTION_OVERRIDE_ATTEMPT",
        r"\b(?:ignore|disregard|forget|override)\b.{0,45}\b(?:previous|prior|system|developer|all)\b.{0,30}\b(?:instructions?|rules?|messages?|controls?)\b",
        1.0,
    ),
    _signal(
        "SYSTEM_PROMPT_EXTRACTION_ATTEMPT",
        r"\b(?:reveal|print|dump|return|expose|repeat|show me)\b.{0,40}\b"
        r"(?:system prompt|developer message|hidden instructions?|internal prompt)\b",
        1.0,
    ),
    _signal(
        "CREDENTIAL_EXTRACTION_ATTEMPT",
        r"\b(?:reveal|print|dump|return|expose|give me)\b.{0,45}\b"
        r"(?:actual |raw |full )?"
        r"(?:api keys?|passwords?|credentials?|access tokens?|secrets?)\b",
        1.0,
    ),
    _signal(
        "SECURITY_CONTROL_BYPASS_ATTEMPT",
        r"\b(?:bypass|disable|evade|circumvent|turn off|ignore)\b.{0,45}\b"
        r"(?:authorization|policy|policies|security|guardrails?|controls?|audit|"
        r"row[- ]level security)\b",
        1.0,
    ),
    _signal(
        "MASKING_BYPASS_ATTEMPT",
        r"\b(?:unmask|disable masking|without masking|bypass masking|"
        r"raw unredacted|remove redaction)\b",
        1.0,
    ),
    _signal(
        "PRIVILEGE_ESCALATION_ATTEMPT",
        r"\b(?:act as|become|you are now|impersonate|switch to)\b.{0,35}\b"
        r"(?:root|administrator|admin|platform admin|security officer)\b",
        0.9,
    ),
    _signal(
        "UNBOUNDED_DATA_EXTRACTION_ATTEMPT",
        r"\b(?:dump|export|return|extract)\b.{0,30}\b(?:all|every|entire)\b.{0,35}\b(?:customer|account|transaction|cardholder|patient)\b.{0,20}\b(?:rows?|records?|values?|data)\b",
        0.9,
    ),
)


class DeterministicPromptRiskClassifier:
    """Versioned, content-minimizing pre-retrieval policy screen."""

    block_threshold = 0.8

    @staticmethod
    def _normalize(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text)
        return re.sub(r"\s+", " ", normalized).strip().lower()

    def assess(self, text: str) -> PromptRiskAssessment:
        normalized = self._normalize(text)
        matched = [signal for signal in RISK_SIGNALS if signal.pattern.search(normalized)]
        score = min(1.0, round(sum(signal.weight for signal in matched), 4))
        reason_codes = sorted({signal.reason_code for signal in matched})
        return PromptRiskAssessment(
            decision="BLOCK" if score >= self.block_threshold else "ALLOW",
            score=score,
            reason_codes=reason_codes or ["NO_PROMPT_RISK_SIGNAL"],
            signal_count=len(matched),
        )
