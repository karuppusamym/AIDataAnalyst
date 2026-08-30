"""Shared data-classification constants.

A leaf module (no imports from this package) so that any layer -- the query
gateway, the lineage/intelligence layer, or a catalog-stewardship module --
can reference the sensitive-classification vocabulary without importing
`aida.query_gateway` merely to reach a constant. Importing the gateway for
this reason was flagged as a misplaced-constant problem (not a real cycle)
in the "C4 / ST-11 lineage and intelligence modules never import the query
gateway" import-linter contract in `pyproject.toml`, and tracked in
`Docs/review-2026-08/gap/05-validate-sql-handoff.md`.
"""

from __future__ import annotations

SENSITIVE_CLASSES: frozenset[str] = frozenset({"CONFIDENTIAL", "PII", "PHI", "PCI", "SECRET"})
