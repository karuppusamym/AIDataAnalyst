"""Backward-compatible re-export.

Canonical location: `atlas.platform.config` (tracker ST-04, Phase 1 of
`Docs/40-engineering/06-refactor-plan.md`). Every existing
`from aida.config import ...` caller keeps working unchanged; new code
should import from `atlas.platform.config` directly.
"""

from atlas.platform.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
