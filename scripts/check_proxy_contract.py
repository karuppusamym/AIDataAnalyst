"""Assert that the dev proxy and the production proxy agree on the API paths.

F07 (`Docs/review-2026-09-05/REVIEW.md`): `AgentGatewayScreen` tells an engineer
the MCP endpoint is `${location.origin}/mcp`. `ui-next/vite.config.ts` proxied
`/mcp` to the API in development, but `ui-next/nginx.conf` proxied only `/v1/`,
so in the production topology `/mcp` fell through to the SPA fallback and the
copied URL returned `index.html`. The statement on screen was true in dev and
false in the deployment the engineer was looking at.

The defect class is *divergence*, not a missing line. So this gate does not
hardcode the expected set: it extracts the API-proxied path prefixes from both
files and fails when the two sets differ. Adding a new backend prefix to one
side and forgetting the other fails CI in either direction.

Scope and honesty
-----------------
**This is a static contract check, not a live proxy test.** It parses two
configuration files. It does not start nginx, does not build the image, and
proves nothing about whether the upstream is reachable or answers correctly.
The live counterpart is the `ui-proxy` job in `.github/workflows/ci.yml`, which
runs the real nginx image against this exact config with a stub upstream and
asserts `/mcp` reaches the upstream rather than the SPA. Keep both: this one is
fast and runs anywhere, that one proves the behaviour.

Standard library only. Usage::

    python scripts/check_proxy_contract.py

Exit code 1 on divergence.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NGINX_CONF = REPO_ROOT / "ui-next" / "nginx.conf"
VITE_CONFIG = REPO_ROOT / "ui-next" / "vite.config.ts"

# `location /` (the SPA catch-all) and `location = /health` (answered by nginx
# itself) are not API prefixes and correctly have no Vite counterpart -- in
# development those requests are handled by Vite. Neither declares a
# `proxy_pass`, so both are excluded by construction rather than by name.


def strip_nginx_comments(text: str) -> str:
    """Drop `#` comments, keeping line count so the file still parses by block.

    Necessary because this repository's nginx.conf carries long explanatory
    comments that themselves mention `location /...` and braces.
    """
    out: list[str] = []
    for line in text.splitlines():
        in_quote: str | None = None
        cut = len(line)
        for index, char in enumerate(line):
            if in_quote:
                if char == in_quote:
                    in_quote = None
            elif char in "\"'":
                in_quote = char
            elif char == "#":
                cut = index
                break
        out.append(line[:cut])
    return "\n".join(out)


def strip_js_comments(text: str) -> str:
    """Drop `//` and `/* */` comments from a TypeScript config file."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)
    out: list[str] = []
    for line in text.splitlines():
        in_quote: str | None = None
        cut = len(line)
        index = 0
        while index < len(line):
            char = line[index]
            if in_quote:
                if char == "\\":
                    index += 2
                    continue
                if char == in_quote:
                    in_quote = None
            elif char in "\"'`":
                in_quote = char
            elif char == "/" and line[index + 1 : index + 2] == "/":
                cut = index
                break
            index += 1
        out.append(line[:cut])
    return "\n".join(out)


def normalize(prefix: str) -> str:
    """`/v1`, `/v1/` and `^~ /v1/` all name the same public path prefix."""
    prefix = prefix.strip()
    prefix = re.sub(r"^[\^~=*]+\s*", "", prefix)
    prefix = prefix.rstrip("/")
    return prefix or "/"


def nginx_proxied_prefixes(text: str) -> set[str]:
    """Path prefixes that `nginx.conf` forwards upstream via `proxy_pass`.

    Parsed by walking the `location <match> { ... }` blocks and keeping the ones
    whose body contains a `proxy_pass`. A `location` that only serves files or
    returns a literal is not an API prefix.
    """
    prefixes: set[str] = set()
    for match in re.finditer(r"location\s+([^{]+?)\s*\{", text):
        raw = match.group(1).strip()
        body_start = match.end()
        depth = 1
        index = body_start
        while index < len(text) and depth:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        body = text[body_start : index - 1]
        if not re.search(r"^\s*proxy_pass\b", body, re.MULTILINE):
            continue
        prefixes.add(normalize(raw))
    return prefixes


def nginx_declared_locations(text: str) -> set[str]:
    return {normalize(m.group(1)) for m in re.finditer(r"location\s+([^{]+?)\s*\{", text)}


def vite_proxied_prefixes(text: str) -> set[str]:
    """Path prefixes under `server.proxy` in `vite.config.ts`.

    Vite's proxy keys are string literals in an object literal, each mapping to
    an object with a `target`. Matching `"<key>": {` inside the `proxy: {`
    block is enough and avoids pulling a JS parser into a stdlib-only check.
    """
    proxy_start = text.find("proxy:")
    if proxy_start == -1:
        return set()
    brace = text.find("{", proxy_start)
    depth = 0
    index = brace
    while index < len(text):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                break
        index += 1
    block = text[brace : index + 1]
    return {
        normalize(m.group(1))
        for m in re.finditer(r"[\"']([^\"']+)[\"']\s*:\s*\{", block)
        if m.group(1).startswith("/")
    }


def main() -> int:
    for path in (NGINX_CONF, VITE_CONFIG):
        if not path.is_file():
            print(f"FAIL: {path.relative_to(REPO_ROOT).as_posix()} does not exist")
            return 1

    nginx_text = strip_nginx_comments(NGINX_CONF.read_text(encoding="utf-8"))
    vite_text = strip_js_comments(VITE_CONFIG.read_text(encoding="utf-8"))

    nginx_api = nginx_proxied_prefixes(nginx_text)
    nginx_all = nginx_declared_locations(nginx_text)
    vite_api = vite_proxied_prefixes(vite_text)

    problems: list[str] = []

    if not nginx_api:
        problems.append("ui-next/nginx.conf declares no proxy_pass location at all")
    if not vite_api:
        problems.append("ui-next/vite.config.ts declares no server.proxy entries at all")

    for prefix in sorted(vite_api - nginx_api):
        detail = (
            "it falls through to the SPA `try_files ... /index.html` fallback, so the "
            "browser gets index.html instead of the API"
        )
        if prefix in nginx_all:
            detail = "the location exists but has no proxy_pass"
        problems.append(
            f"'{prefix}' is proxied to the API in development (vite.config.ts) but not "
            f"in production (nginx.conf) -- {detail}"
        )

    for prefix in sorted(nginx_api - vite_api):
        problems.append(
            f"'{prefix}' is proxied to the API in production (nginx.conf) but not in "
            "development (vite.config.ts) -- `npm run dev` will not reach it"
        )

    if problems:
        print("Dev/production proxy contract mismatch:\n")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nEvery public path served by the API must be proxied in BOTH "
            "ui-next/vite.config.ts (development) and ui-next/nginx.conf (production).\n"
            "This is a static configuration check; the live proxy behaviour is covered "
            "by the `ui-proxy` job in .github/workflows/ci.yml."
        )
        return 1

    shared = ", ".join(sorted(nginx_api))
    print(f"OK: dev and production both proxy the same API path prefixes: {shared}")
    print("(static configuration check -- see the `ui-proxy` CI job for the live test)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
