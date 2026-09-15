"""R11-FP09: the one rule for which mapping kind a catalog table-like object takes.

Shared by the ontology routes, which refuse a mapping whose declared kind differs on every write,
and by retrieval, which ignores such a mapping at question time -- so the two cannot disagree
about what a VIEW mapping may point at.
"""

from __future__ import annotations

from typing import Final

from aida.discovery_selection import table_kind

_VIEW_KINDS: Final = frozenset({"VIEW", "MATERIALIZED_VIEW"})


def table_mapping_kind(object_type: str) -> str:
    """The `OntologyMapping.subject_type` a `MetadataTable` row takes: VIEW for a view or a
    materialized view, TABLE for every other table-like type."""
    return "VIEW" if table_kind(object_type) in _VIEW_KINDS else "TABLE"
