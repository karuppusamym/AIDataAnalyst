"""profiling -- HTTP routes, mounted by the app entrypoint.

Status: scaffold only (tracker ST-01). No routes have moved here yet, and
that is deliberate: review-2026-09-05 point **R04** is about `models.py` and
`schemas.py`, so this pass relocated this module's ORM models and API DTOs
and nothing else. Moving the profiling endpoints out of `aida.api` is the
separate ST-07 Commit C step the four sibling modules went through on
2026-09-03, and it has its own gate (`scripts/openapi_diff.py` must stay
byte-identical across a route move).

This `router` is therefore not mounted by `aida.main` and carries no routes;
it is the empty container the later route move fills. `api.py` correspondingly
does not re-export it yet -- when the routes move, `api.py` gains the
`from atlas.modules.profiling.router import router as router` line the four
sibling `api.py` files already have.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["profiling"])
