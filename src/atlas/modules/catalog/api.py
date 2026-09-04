"""catalog -- PUBLIC interface.

Other modules import only from here and from `contracts.py`. Nothing else
in this module is reachable from outside it -- enforced mechanically by
the `module-privacy` import-linter contract once it exists (tracker ST-02).

Status: scaffold only (tracker ST-01). No behavior has moved here yet; see
`Docs/40-engineering/06-refactor-plan.md` for the extraction sequence.
"""

from __future__ import annotations

# ST-07: the module's HTTP surface is part of its public interface. `main.py`
# is the application's composition root and mounts this router; it must not
# reach past `api.py` into `router.py`, which the `catalog module privacy`
# import-linter contract protects. Re-exported here (rather than adding
# `aida.main` to that contract's allowed_importers) so the protected boundary
# keeps meaning exactly what it says: only this module's public face is
# reachable from outside.
from atlas.modules.catalog.router import router as router

__all__ = ["router"]
