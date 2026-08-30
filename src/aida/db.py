"""Backward-compatible re-export.

Canonical location: `atlas.platform.db` (tracker ST-04, Phase 1 of
`Docs/40-engineering/06-refactor-plan.md`). Every existing
`from aida.db import ...` caller keeps working unchanged; new code should
import from `atlas.platform.db` directly.
"""

from atlas.platform.db import (
    NAMING_CONVENTION,
    Base,
    engine,
    get_session,
    session_factory,
    settings,
)

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "engine",
    "get_session",
    "session_factory",
    "settings",
]
