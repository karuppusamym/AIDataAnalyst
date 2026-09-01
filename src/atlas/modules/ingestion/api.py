"""ingestion -- PUBLIC interface.

Other modules import only from here and from `contracts.py`. Nothing else
in this module is reachable from outside it -- enforced mechanically by
the `module-privacy` import-linter contract once it exists (tracker ST-02).

Status: scaffold only (tracker ST-01). No behavior has moved here yet; see
`Docs/40-engineering/06-refactor-plan.md` for the extraction sequence.
"""

from __future__ import annotations
