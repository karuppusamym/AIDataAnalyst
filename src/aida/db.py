"""Backward-compatible re-export.

Canonical location: `atlas.platform.db` (tracker ST-04, Phase 1 of
`Docs/40-engineering/06-refactor-plan.md`). Every existing
`from aida.db import ...` caller keeps working unchanged; new code should
import from `atlas.platform.db` directly.

`engine`/`session_factory`/`settings` are re-exported lazily via
`__getattr__`, not imported eagerly here, so that merely importing
`aida.db` (pulled in transitively by `aida.models`) never forces
`atlas.platform.db`'s engine/settings construction -- see that module's
docstring.
"""

from atlas.platform.db import NAMING_CONVENTION, Base, get_session

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "engine",  # noqa: F822 -- resolved lazily by __getattr__ below
    "get_session",
    "session_factory",  # noqa: F822 -- resolved lazily by __getattr__ below
    "settings",  # noqa: F822 -- resolved lazily by __getattr__ below
]


def __getattr__(name: str) -> object:
    if name in {"engine", "session_factory", "settings"}:
        import atlas.platform.db as _platform_db

        return getattr(_platform_db, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
