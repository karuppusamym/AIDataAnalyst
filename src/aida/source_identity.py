"""Which code is this process running? A content digest of the source the image ships.

R11-D17. `scripts/check_deployment_parity.py` compared schema, API surface and
settings, so a code-only change -- one that moves none of the three -- could not
be told from a stale image. On 2026-09-19 it reported "the deployment is running
this tree" against an image 40 minutes older than HEAD for exactly that reason.

A commit hash does not close that gap on its own. The image is built from the
working tree, not from a commit, so an image stamped with HEAD may hold
uncommitted edits, and a tree at the same HEAD may have gained more since. This
module hashes the files themselves: the paths the Dockerfile copies, read where
the running package was loaded from. The parity check computes the same digest
over the checkout it runs in, so equal digests mean the same shipped source,
whatever git says about either side.

What it does not cover: the base image and the Dockerfile itself (not copied
into the image), and installed dependency *contents* beyond what `uv.lock` pins.

Standard library only, so it costs nothing to import and cannot drag a layer
into readiness that readiness should not depend on.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path, PurePosixPath

#: The paths the Dockerfile's COPY lines put under `/app`, relative to the application root.
#: `tests/test_source_identity.py` pins these against the Dockerfile, so a new COPY
#: cannot ship code this digest does not see.
SOURCE_DIRECTORIES = ("src", "sdk", "migrations")
SOURCE_FILES = ("pyproject.toml", "uv.lock", "alembic.ini")

UNKNOWN_DIGEST = "unknown"

_SKIPPED_SUFFIXES = frozenset({".pyc", ".pyo"})


def application_root() -> Path:
    """The directory holding `src/`: `/app` in the image, the repository root in a checkout."""
    return Path(__file__).resolve().parents[2]


def _skipped(relative: PurePosixPath) -> bool:
    """Bytecode and dot-entries: produced by running the code, not part of what ships."""
    return relative.suffix in _SKIPPED_SUFFIXES or any(
        part == "__pycache__" or part.startswith(".") for part in relative.parts
    )


def _file_digest(path: Path) -> str:
    # CRLF is normalised to LF: `core.autocrlf` decides a checkout's line endings, so
    # without this a Windows checkout and a Linux build of the same commit would
    # always read as different code.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def source_manifest(root: Path) -> dict[str, str]:
    """Each shipped file's POSIX path relative to `root`, mapped to the SHA-256 of its content.

    Empty when `root` is missing any declared path -- an installed wheel rather than
    the image or a checkout -- because a digest over part of the source would compare
    as drift that is not there.
    """
    manifest: dict[str, str] = {}
    for name in SOURCE_FILES:
        path = root / name
        if not path.is_file():
            return {}
        manifest[name] = _file_digest(path)
    for name in SOURCE_DIRECTORIES:
        directory = root / name
        if not directory.is_dir():
            return {}
        for path in directory.rglob("*"):
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if path.is_file() and not _skipped(relative):
                manifest[str(relative)] = _file_digest(path)
    return manifest


def manifest_digest(manifest: dict[str, str]) -> str:
    """One digest for the whole manifest, independent of the order files were walked."""
    if not manifest:
        return UNKNOWN_DIGEST
    digest = hashlib.sha256()
    for relative in sorted(manifest):
        digest.update(f"{relative}\0{manifest[relative]}\n".encode())
    return digest.hexdigest()


@lru_cache(maxsize=1)
def running_source_digest() -> str:
    """The digest of the source this process loaded, computed once per process.

    Once is enough: the image's `/app` is not writable by the `aida` user, and a
    development server that reloads on edit starts a new process.
    """
    return manifest_digest(source_manifest(application_root()))
