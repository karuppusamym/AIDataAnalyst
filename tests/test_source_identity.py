"""R11-D17: the digest that tells a running image's code from this tree's.

The parity check printed "the deployment is running this tree" on 2026-09-19
against an image 40 minutes older than HEAD, because schema, routes and settings
were all it compared. `aida.source_identity` is what it compares now, so these
tests pin the properties that make the comparison mean something: it sees every
file the image ships and nothing the image does not, it is blind to line
endings and bytecode, and a partial source is "unknown" rather than a digest.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.db import Base
from aida.readiness import (
    BUILD_COMMIT_SIGNAL,
    SOURCE_DIGEST_SIGNAL,
    evaluate_readiness,
    reset_last_success,
)
from aida.source_identity import (
    SOURCE_DIRECTORIES,
    SOURCE_FILES,
    UNKNOWN_DIGEST,
    application_root,
    manifest_digest,
    running_source_digest,
    source_manifest,
)
from atlas.platform.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def _copied_sources(dockerfile: str) -> set[str]:
    """Every source path a COPY line in the Dockerfile puts into the image."""
    sources: set[str] = set()
    for line in dockerfile.splitlines():
        match = re.match(r"^COPY\s+(?!--from)(.+)$", line.strip())
        if match:
            *copied, _destination = match.group(1).split()
            sources.update(copied)
    return sources


def test_the_digest_covers_exactly_what_the_dockerfile_copies() -> None:
    """A COPY this digest does not know about ships code parity cannot see."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert _copied_sources(dockerfile) == set(SOURCE_DIRECTORIES) | set(SOURCE_FILES)


def _write_tree(root: Path, *, body: bytes = b"x = 1\n") -> None:
    for name in SOURCE_FILES:
        (root / name).write_bytes(b"[tool]\n")
    for name in SOURCE_DIRECTORIES:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "src" / "module.py").write_bytes(body)


def test_line_endings_do_not_change_the_digest(tmp_path: Path) -> None:
    """`core.autocrlf` decides a checkout's line endings, not the code."""
    unix, windows = tmp_path / "unix", tmp_path / "windows"
    unix.mkdir()
    windows.mkdir()
    _write_tree(unix, body=b"x = 1\ny = 2\n")
    _write_tree(windows, body=b"x = 1\r\ny = 2\r\n")
    assert manifest_digest(source_manifest(unix)) == manifest_digest(source_manifest(windows))


def test_a_one_character_change_changes_the_digest(tmp_path: Path) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    _write_tree(before, body=b"x = 1\n")
    _write_tree(after, body=b"x = 2\n")
    assert manifest_digest(source_manifest(before)) != manifest_digest(source_manifest(after))


def test_bytecode_and_dot_entries_are_not_source(tmp_path: Path) -> None:
    """Running the code writes `__pycache__`; that must not read as a different image."""
    _write_tree(tmp_path)
    clean = manifest_digest(source_manifest(tmp_path))
    cache = tmp_path / "src" / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-313.pyc").write_bytes(b"\x00bytecode")
    (tmp_path / "src" / "stray.pyc").write_bytes(b"\x00")
    (tmp_path / "src" / ".DS_Store").write_bytes(b"\x00")
    assert manifest_digest(source_manifest(tmp_path)) == clean


def test_a_new_file_changes_the_digest(tmp_path: Path) -> None:
    """An untracked file under src/ ships in the image, so it is part of the identity."""
    _write_tree(tmp_path)
    before = manifest_digest(source_manifest(tmp_path))
    (tmp_path / "migrations" / "0001_new.py").write_bytes(b"revision = '1'\n")
    assert manifest_digest(source_manifest(tmp_path)) != before


def test_a_partial_source_is_unknown_not_a_digest(tmp_path: Path) -> None:
    """An installed wheel has no `migrations/` beside it; a digest over part of the
    source would compare as drift that is not there."""
    _write_tree(tmp_path)
    (tmp_path / "uv.lock").unlink()
    assert source_manifest(tmp_path) == {}
    assert manifest_digest({}) == UNKNOWN_DIGEST


def test_the_digest_does_not_depend_on_walk_order() -> None:
    forward = {"a.py": "1", "b.py": "2"}
    backward = {"b.py": "2", "a.py": "1"}
    assert manifest_digest(forward) == manifest_digest(backward)


def test_a_checkout_digests_itself() -> None:
    assert application_root() == REPO_ROOT
    assert running_source_digest() == manifest_digest(source_manifest(REPO_ROOT))
    assert running_source_digest() != UNKNOWN_DIGEST


@pytest_asyncio.fixture
async def sqlite_sessions() -> AsyncIterator[Any]:
    """Enough of a database for readiness's probes to answer; the signals under test need none."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_readiness_publishes_the_digest_and_the_commit_label(
    monkeypatch: pytest.MonkeyPatch, sqlite_sessions: Any
) -> None:
    monkeypatch.setenv("ATLAS_BUILD_COMMIT", "b76842d0000000000000000000000000000000000")
    reset_last_success()
    report = await evaluate_readiness(
        Settings(temporal_enabled=False),
        temporal_client=None,
        background_tasks={},
        session_factory=sqlite_sessions,
    )
    assert report.signals[SOURCE_DIGEST_SIGNAL] == running_source_digest()
    assert report.signals[BUILD_COMMIT_SIGNAL] == "b76842d0000000000000000000000000000000000"


async def test_an_image_built_without_the_commit_says_so(
    monkeypatch: pytest.MonkeyPatch, sqlite_sessions: Any
) -> None:
    monkeypatch.delenv("ATLAS_BUILD_COMMIT", raising=False)
    reset_last_success()
    report = await evaluate_readiness(
        Settings(temporal_enabled=False),
        temporal_client=None,
        background_tasks={},
        session_factory=sqlite_sessions,
    )
    assert report.signals[BUILD_COMMIT_SIGNAL] == "unknown"
