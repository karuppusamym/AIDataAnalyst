"""Backward-compatible re-export.

Canonical location: `atlas.platform.logging` (tracker ST-04, Phase 1 of
`Docs/40-engineering/06-refactor-plan.md`). Every existing
`from aida.logging import ...` caller keeps working unchanged; new code
should import from `atlas.platform.logging` directly.
"""

from atlas.platform.logging import configure_logging

__all__ = ["configure_logging"]
