"""Backward-compatible re-export.

Canonical location: `atlas.platform.context` (tracker ST-04, Phase 1 of
`Docs/40-engineering/06-refactor-plan.md`). Every existing
`from aida.context import ...` caller keeps working unchanged; new code
should import from `atlas.platform.context` directly.
"""

from atlas.platform.context import correlation_id_var, get_correlation_id

__all__ = ["correlation_id_var", "get_correlation_id"]
