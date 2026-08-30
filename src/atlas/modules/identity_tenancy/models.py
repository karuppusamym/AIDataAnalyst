"""identity tenancy -- PRIVATE. SQLAlchemy models in this module's own schema
(`identity`, per `Docs/10-architecture/04-module-decomposition.md` Sec.6).

Not importable from outside this module once the `module-privacy`
contract (tracker ST-02) is enforced.

Status: scaffold only (tracker ST-01).
"""

from __future__ import annotations
