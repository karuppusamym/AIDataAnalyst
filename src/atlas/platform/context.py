"""Correlation-ID request context (tracker ST-04, Phase 1 of
`Docs/40-engineering/06-refactor-plan.md`).

Moved verbatim from `aida.context`. Pure infrastructure -- no domain
knowledge -- satisfying `platform-purity`
(`Docs/10-architecture/04-module-decomposition.md` Sec.8). `aida.context`
now re-exports from here for backward compatibility; new code should import
from this module directly.
"""

from contextvars import ContextVar

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="unknown")


def get_correlation_id() -> str:
    return correlation_id_var.get()
