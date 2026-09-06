"""Assert the backend image copies every package the wheel manifest declares.

REVIEW.md §7: `pyproject.toml` declares

    [tool.hatch.build.targets.wheel]
    packages = ["src/aida", "src/atlas", "sdk/aida_tool_sdk"]

while the backend `Dockerfile` copied only `src` and `migrations`. That did not
fail the build. Hatchling's editable install wrote a `.pth` for the roots it
could find and skipped the missing one, so `docker build` succeeded and produced
an image whose `import aida_tool_sdk` raised `ModuleNotFoundError` -- a manifest
and an image that disagreed, with nothing anywhere to notice.

The decision (recorded in `Docs/60-delivery/20-capability-register.md`) is that
the image is an install of this project's declared distribution, so it ships
every declared package root. This check keeps the two from drifting apart again:
a package root added to `packages` without a matching `COPY` in the Dockerfile
fails here, in either direction.

It is a *manifest* check. The runtime proof -- that the built image can actually
import each package -- is the smoke step in the `docker-build` job of
`.github/workflows/ci.yml`, which needs a real image and so cannot live here.

Standard library only (`tomllib`). Usage::

    python scripts/check_image_packaging.py

Exit code 1 on divergence.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
DOCKERFILE = REPO_ROOT / "Dockerfile"

# `COPY <src> <dst>` -- the source paths only, ignoring flags like --from=build.
COPY_LINE = re.compile(r"^\s*COPY\s+(?P<rest>.+)$", re.MULTILINE | re.IGNORECASE)


def declared_packages(pyproject_text: str) -> list[str]:
    data = tomllib.loads(pyproject_text)
    wheel = data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {})
    packages = wheel.get("wheel", {}).get("packages", [])
    return [str(package) for package in packages]


def copied_sources(dockerfile_text: str) -> set[str]:
    """Source paths the Dockerfile copies into the image, as posix strings."""
    sources: set[str] = set()
    for match in COPY_LINE.finditer(dockerfile_text):
        parts = [p for p in match.group("rest").split() if not p.startswith("--")]
        if len(parts) < 2:
            continue
        for source in parts[:-1]:  # the last token is the destination
            sources.add(source.strip("./").rstrip("/"))
    return sources


def is_covered(package: str, sources: set[str]) -> bool:
    """True when `package` or any ancestor directory of it is copied."""
    parts = Path(package).parts
    return any("/".join(parts[: i + 1]) in sources for i in range(len(parts)))


def main() -> int:
    for path in (PYPROJECT, DOCKERFILE):
        if not path.is_file():
            print(f"FAIL: {path.relative_to(REPO_ROOT).as_posix()} does not exist")
            return 1

    packages = declared_packages(PYPROJECT.read_text(encoding="utf-8"))
    if not packages:
        print("FAIL: pyproject.toml declares no [tool.hatch.build.targets.wheel] packages")
        return 1

    sources = copied_sources(DOCKERFILE.read_text(encoding="utf-8"))
    missing = [package for package in packages if not is_covered(package, sources)]

    if missing:
        print("Backend image does not ship every declared package:\n")
        for package in missing:
            print(
                f"  - '{package}' is in [tool.hatch.build.targets.wheel].packages but no "
                f"COPY in the Dockerfile brings it into the image."
            )
        print(
            "\nAdd `COPY <path> ./<path>` to the Dockerfile, or remove the package from the\n"
            "wheel manifest. `uv sync` will NOT fail on the mismatch -- it writes a .pth for\n"
            "the roots it can see and silently drops the rest, so the image builds green and\n"
            "the import fails at runtime instead."
        )
        return 1

    for package in packages:
        if not (REPO_ROOT / package).is_dir():
            print(f"FAIL: declared package '{package}' does not exist in the repository")
            return 1

    listed = ", ".join(packages)
    print(f"OK: the Dockerfile copies every declared wheel package: {listed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
