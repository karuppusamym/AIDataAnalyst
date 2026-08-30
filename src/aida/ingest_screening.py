"""Screen source-supplied text at write time, before anything can consume it.

Closes the gap flagged in four separate documents and addressed in none of them:
ADR-0013, threat model T7, `50-security/03` AS-1, and module 13's AG-1 all record that
prompt-risk screening covers the *user's question* and not the metadata that retrieval
subsequently surfaces. A malicious column description reaching model context was already
the acknowledged hole; envelope 1.1 made it much larger, because a stored procedure body
is several kilobytes of source-controlled text that meaning inference and tool generation
are both designed to read.

**Why at write, not at read.** Screening on every read means screening the same text
repeatedly, on the latency-sensitive path, and getting it wrong everywhere a new consumer
is added. Screening once at ingestion is cheaper, complete by construction, and leaves a
durable verdict a steward can review. The cost is that a classifier upgrade does not
retroactively re-screen -- so `screened_with_version` is recorded, and re-screening is a
bulk job like any other projection rebuild.

**Quarantine, not deletion.** Text that fails is stored and marked, never dropped. A
procedure whose body trips the classifier is far more likely to be an awkward comment than
an attack, and deleting a source's own metadata because a regex matched would be both
wrong and undebuggable. What quarantine changes is eligibility: quarantined text is
excluded from model context and flagged for review, while remaining visible to a human
looking at the object.

**This is defence in depth, and it is the weaker layer.** The load-bearing control is
INV-3: even a successful injection produces a structured proposal that cannot execute,
publish or bind a tool. Screening reduces how often a model is exposed to hostile text; it
does not make the model trustworthy, and it is evadable by paraphrase like every other
classifier.
"""

from dataclasses import dataclass

from aida.prompt_risk import (
    PROMPT_RISK_CLASSIFIER_VERSION,
    DeterministicPromptRiskClassifier,
)

CLEAN = "CLEAN"
QUARANTINED = "QUARANTINED"

# Tracks the classifier's own version, so a stored verdict can be told apart from one
# produced by today's rules and a re-screen can be targeted at stale rows.
SCREENING_VERSION = PROMPT_RISK_CLASSIFIER_VERSION

_classifier = DeterministicPromptRiskClassifier()


@dataclass(frozen=True, slots=True)
class ScreeningVerdict:
    status: str
    reason_codes: list[str]
    version: str = SCREENING_VERSION

    @property
    def is_clean(self) -> bool:
        return self.status == CLEAN


def screen_text(text: str | None) -> ScreeningVerdict:
    """Assess one piece of source-supplied text.

    Empty and absent text is clean by definition rather than by evaluation -- running a
    regex set over `None` to conclude nothing is a waste on the ingestion hot path, which
    processes millions of columns.
    """
    if not text or not text.strip():
        return ScreeningVerdict(status=CLEAN, reason_codes=[])
    assessment = _classifier.assess(text)
    if assessment.decision == "BLOCK":
        return ScreeningVerdict(status=QUARANTINED, reason_codes=list(assessment.reason_codes))
    return ScreeningVerdict(status=CLEAN, reason_codes=[])


def screen_many(texts: dict[str, str | None]) -> dict[str, ScreeningVerdict]:
    """Screen several named fields of one object. Convenience, same semantics."""
    return {name: screen_text(value) for name, value in texts.items()}


def is_eligible_for_model_context(screening_status: str) -> bool:
    """The one question every model-context builder must ask before including text.

    Fails closed on an unknown status: a value this function does not recognise is more
    likely to be a new quarantine state than a new safe state.
    """
    return screening_status == CLEAN
