"""catalog -- HTTP routes, mounted by the app entrypoint.

Status: scaffold only (tracker ST-01). No routes have moved here yet.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["catalog"])
