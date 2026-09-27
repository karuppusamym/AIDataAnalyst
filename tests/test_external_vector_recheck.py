"""R11-MP16: an external vector index's answer is re-checked, not trusted.

`ExternalVectorIndex.search` sends the allowlist as owner ids only, and used to
return whatever owner type and id the remote service answered. A remote index
that matched an id of the wrong owner type, or answered for another
organization or another index signature, put an object the policy never
admitted into retrieval.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from aida.config import Settings
from aida.vector_store import EmbeddingRef, ExternalVectorIndex

SIGNATURE = "bge-large:1.5:1024:v3"
ORG = uuid4()


def _index(monkeypatch: pytest.MonkeyPatch, matches: list[Any]) -> ExternalVectorIndex:
    index = ExternalVectorIndex(
        Settings(vector_index_url="https://vectors.bank.internal", _env_file=None)
    )

    async def _answer(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert path.endswith("/search")
        return {"matches": matches}

    monkeypatch.setattr(index, "_post", _answer)
    return index


def _match(owner_type: str, owner_id: str, **extra: Any) -> dict[str, Any]:
    return {"owner_type": owner_type, "owner_id": owner_id, "score": 0.9, **extra}


async def _search(
    index: ExternalVectorIndex, candidates: Any, limit: int = 10
) -> list[tuple[str, str]]:
    matches = await index.search(
        None,  # type: ignore[arg-type]
        ORG,
        (1.0, 0.0),
        signature=SIGNATURE,
        candidates=candidates,
        limit=limit,
    )
    return [(m.ref.owner_type, m.ref.owner_id) for m in matches]


ALLOWED = (EmbeddingRef("TABLE", "t1"), EmbeddingRef("COLUMN", "c1"))


@pytest.mark.asyncio
async def test_a_match_of_the_wrong_owner_type_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "c1" is an allowed id -- but as a COLUMN, not a TABLE.
    index = _index(monkeypatch, [_match("TABLE", "t1"), _match("TABLE", "c1")])
    assert await _search(index, ALLOWED) == [("TABLE", "t1")]


@pytest.mark.asyncio
async def test_an_answer_for_another_organization_or_signature_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(
        monkeypatch,
        [
            _match("TABLE", "t1", organization_id=str(uuid4())),
            _match("COLUMN", "c1", index_signature="other-model:1:768:v1"),
            _match("TABLE", "t1", organization_id=str(ORG), index_signature=SIGNATURE),
        ],
    )
    assert await _search(index, ALLOWED) == [("TABLE", "t1")]


@pytest.mark.asyncio
async def test_malformed_and_ownerless_matches_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(monkeypatch, ["not a match", _match("", "t1"), _match("TABLE", "t1")])
    assert await _search(index, ALLOWED) == [("TABLE", "t1")]


@pytest.mark.asyncio
async def test_no_more_than_the_limit_comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    index = _index(monkeypatch, [_match("TABLE", "t1"), _match("COLUMN", "c1")])
    assert len(await _search(index, ALLOWED, limit=1)) == 1


@pytest.mark.asyncio
async def test_without_an_allowlist_only_the_organization_and_signature_checks_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(
        monkeypatch, [_match("TABLE", "anything"), _match("TABLE", "x", organization_id="no")]
    )
    assert await _search(index, None) == [("TABLE", "anything")]
