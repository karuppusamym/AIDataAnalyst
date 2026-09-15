"""A structured 409 body a person can read, and a bulk decision can report.

The governance decision paths report a refused item as `str(exc.detail)` (bulk decisions, the
reviewer agent). A plain dict prints as its repr there; this prints the sentence meant for a
person, while the JSON body keeps the code and the reason for a client that branches on them.
"""

from typing import Any


class RefusalDetail(dict[str, Any]):
    def __str__(self) -> str:
        return str(self.get("message", ""))
