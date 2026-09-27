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

A second contract lives in the same file (tracker R11-AUD05): the request-body
limit. `location /v1/` sets no `client_max_body_size`, so nginx's own 1 MiB
default applied to every API route -- including the two that carry a metadata
envelope, which the ingestion contract allows to be tens of MiB. A 2 MiB push
answered 413 through the UI proxy while the same body reached authentication on
the API's own port. The fix is a dedicated location for those two routes, and
the defect class is again a *disagreement between two things nobody compares*
(the proxy's number and the routes' real bodies), so this gate resolves each
concrete route path through the location-matching rules nginx itself applies and
asserts the limit that request would get. It fails when an envelope route falls
back to `/v1/`'s default, when its limit drops below what a body at the
synchronous caps needs, when any limit is unlimited, and when the rest of `/v1/`
was loosened along with it. Since R11-AUD11 it also holds the three file-upload
routes (the workbook import and the OKF bundle import and preview) to exactly
the 32 MiB the API itself accepts.

Scope and honesty
-----------------
**This is a static contract check, not a live proxy test.** It parses two
configuration files. It does not start nginx, does not build the image, and
proves nothing about whether the upstream is reachable or answers correctly.
The location resolver below is a re-implementation of nginx's documented
matching order for the modifiers this config uses (`=`, `^~`, prefix, `~`,
`~*`), not nginx: a directive it does not model can make it wrong, which is why
the live counterpart exists. That is the `ui-proxy` job in
`.github/workflows/ci.yml`, which runs the real nginx image against this exact
config with a stub upstream, asserts `/mcp` reaches the upstream rather than the
SPA, and posts oversize bodies to the envelope routes and to an ordinary `/v1/`
route. Keep both: this one is fast and runs anywhere, that one proves the
behaviour.

The numbers pinned here are bound to the code by `tests/test_proxy_body_limits.py`
(the route list to the FastAPI route table, the floor to the synchronous caps in
`atlas.modules.ingestion.schemas`), so this file can stay standard-library only
and still not drift from the routes it describes. Usage::

    python scripts/check_proxy_contract.py

Exit code 1 on divergence.
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NGINX_CONF = REPO_ROOT / "ui-next" / "nginx.conf"
VITE_CONFIG = REPO_ROOT / "ui-next" / "vite.config.ts"

MIB = 1024 * 1024

# --- the request-body contract (R11-AUD05) ------------------------------------------------------

#: What nginx applies when neither the location nor the server sets
#: `client_max_body_size`. The ordinary `/v1/` routes are supposed to keep it.
NGINX_DEFAULT_BODY_BYTES = 1 * MIB

#: The routes whose request body is a metadata envelope, as the concrete paths a producer sends
#: (the ids are placeholders). Both bodies obey the same ceiling: a batch chunk is validated
#: through the synchronous envelope model, so it can be no larger than a synchronous push.
#: `tests/test_proxy_body_limits.py` compares this list with every POST route of the application
#: whose request body is an envelope or a chunk (read from the published OpenAPI document), so a
#: new envelope-carrying route cannot be added without also being listed -- and therefore
#: checked -- here.
_PLACEHOLDER_ID = "00000000-0000-0000-0000-000000000000"
INGESTION_BODY_ROUTES: tuple[str, ...] = (
    f"/v1/datasources/{_PLACEHOLDER_ID}/metadata-ingestions",
    f"/v1/metadata-ingestion-batches/{_PLACEHOLDER_ID}/chunks",
)

#: The least an envelope route must accept, in bytes. A body at the synchronous caps (250,000
#: columns and 50,000 tables) serialises to about 54 MiB with 32-character identifiers and every
#: optional field written out as null; 64 MiB leaves about 19 % for descriptions and whitespace.
#: `tests/test_proxy_body_limits.py` recomputes the 54 MiB from the schema's own caps and fails if
#: this floor stops covering it.
INGESTION_MIN_BODY_BYTES = 64 * MIB

#: The most an envelope route may accept. Twice the floor: enough room to raise the floor once
#: without touching this, not enough for `0` (unlimited) or a `1g` typed in place of `1m`.
INGESTION_MAX_BODY_BYTES = 128 * MIB

#: R11-AUD11. The routes whose request body is an uploaded file, sent raw with its filename as a
#: query parameter: the model workbook import and an edited OKF bundle's preview and apply.
#: `tests/test_proxy_body_limits.py` checks that each is a POST route of the application.
UPLOAD_BODY_ROUTES: tuple[str, ...] = (
    f"/v1/datasources/{_PLACEHOLDER_ID}/model/import",
    f"/v1/context-product-versions/{_PLACEHOLDER_ID}/okf-bundle/imports",
    f"/v1/context-product-versions/{_PLACEHOLDER_ID}/okf-bundle/imports/preview",
)

#: What the API itself accepts on each upload route, and so exactly what nginx must pass: less
#: refuses a file the API would take, more buffers bytes the API will refuse anyway.
#: `tests/test_proxy_body_limits.py` binds it to `model_import.MAX_UPLOAD_BYTES` and
#: `okf_import_bundle.MAX_ARCHIVE_BYTES`.
UPLOAD_BODY_BYTES = 32 * MIB

#: How long nginx must wait for the API's answer on the envelope and upload routes (R11-AUD11).
#: Each parses, diffs and records a large body inside one synchronous request, and nginx's own
#: 60 s default would answer 504 while the API went on to commit: the caller told "failed" about
#: a batch that exists. Measured 2026-09-21: the workbook's worst case, a sheet just under the
#: reader's 128 MiB uncompressed cap, parsed in about 10 s on a developer machine; the diff is one
#: query and a batch records at most 5,000 changes. The synchronous envelope at its caps was not
#: measured. 300 s is the bound `/mcp` already uses; a push that needs longer belongs in the batch
#: contract.
LONG_REQUEST_READ_TIMEOUT_SECONDS = 300

#: Real API routes that are NOT envelope routes. Each must still resolve to a proxied location
#: limited to nginx's default, which is what "raise the limit for the ingestion routes only, do
#: not loosen `/v1/` generally" means when it is checked rather than intended. The batch manifest,
#: `finalize` and the batch read sit next to the envelope routes on purpose: a pattern written
#: loosely enough to catch `metadata-ingestion-batches` would raise their limit too. The query
#: gateway is the route where a large body is least welcome. `tests/test_proxy_body_limits.py`
#: checks every entry against the application's own route table, so none of them can be a path
#: that only looks like an API route.
GENERAL_API_ROUTES: tuple[str, ...] = (
    f"/v1/projects/{_PLACEHOLDER_ID}/datasources",
    f"/v1/datasources/{_PLACEHOLDER_ID}",
    f"/v1/datasources/{_PLACEHOLDER_ID}/query-executions",
    f"/v1/datasources/{_PLACEHOLDER_ID}/metadata-ingestion-batches",
    f"/v1/metadata-ingestion-batches/{_PLACEHOLDER_ID}/finalize",
    f"/v1/metadata-ingestion-batches/{_PLACEHOLDER_ID}",
)

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


@dataclass(frozen=True)
class NginxLocation:
    """One `location <modifier> <pattern> { ... }` block, in file order.

    `modifier` is one of `""` (a plain prefix), `=`, `^~`, `~` (case-sensitive regex) and `~*`
    (case-insensitive regex) -- the five forms nginx's matching order is defined over.
    """

    modifier: str
    pattern: str
    body: str

    @property
    def proxied(self) -> bool:
        return re.search(r"^\s*proxy_pass\b", self.body, re.MULTILINE) is not None

    @property
    def is_regex(self) -> bool:
        return self.modifier in {"~", "~*"}


_LOCATION_OPEN = re.compile(r"(?:^|(?<=[;{}]))\s*location\s+([^{]+?)\s*\{", re.MULTILINE)


def _block_end(text: str, body_start: int) -> int:
    """Index one past the `}` closing the block whose body starts at `body_start`."""
    depth = 1
    index = body_start
    while index < len(text) and depth:
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        index += 1
    return index


def _split_match(raw: str) -> tuple[str, str]:
    parts = raw.strip().split(None, 1)
    if len(parts) == 2 and parts[0] in {"=", "^~", "~", "~*"}:
        return parts[0], parts[1].strip()
    return "", raw.strip()


def parse_nginx_locations(text: str) -> list[NginxLocation]:
    """Every `location` block of a comment-stripped nginx config, in file order.

    Raises `ValueError` for a location nested inside another: nginx allows it, but this
    check's directive scan would then attribute the inner block's `client_max_body_size` to
    the outer one, and a wrong answer that looks right is worse than a refusal to answer.
    """
    locations: list[NginxLocation] = []
    for match in _LOCATION_OPEN.finditer(text):
        modifier, pattern = _split_match(match.group(1))
        body = text[match.end() : _block_end(text, match.end()) - 1]
        if _LOCATION_OPEN.search(body):
            raise ValueError(
                f"location '{pattern}' contains a nested location block, which "
                "scripts/check_proxy_contract.py does not model; flatten it or extend the check"
            )
        locations.append(NginxLocation(modifier, pattern, body))
    return locations


def nginx_proxied_prefixes(text: str) -> set[str]:
    """Path prefixes that `nginx.conf` forwards upstream via `proxy_pass`.

    Parsed by walking the `location <match> { ... }` blocks and keeping the ones
    whose body contains a `proxy_pass`. A `location` that only serves files or
    returns a literal is not an API prefix.

    A regex location (`~`, `~*`) is not a prefix and is left out: it refines what an
    enclosing prefix already proxies -- the ingestion routes' body limit is one -- rather
    than naming a new public path, so it has no Vite counterpart to disagree with.
    """
    return {
        normalize(location.pattern)
        for location in parse_nginx_locations(text)
        if location.proxied and not location.is_regex
    }


def nginx_declared_locations(text: str) -> set[str]:
    return {
        normalize(location.pattern)
        for location in parse_nginx_locations(text)
        if not location.is_regex
    }


# --- the request-body contract ------------------------------------------------------------------

_BODY_LIMIT = re.compile(
    r"(?:^|(?<=[;{}]))\s*client_max_body_size\s+(\d+)([kKmMgG]?)\s*;", re.MULTILINE
)
_UNIT_BYTES = {"": 1, "k": 1024, "m": MIB, "g": 1024 * MIB}
_READ_TIMEOUT = re.compile(
    r"(?:^|(?<=[;{}]))\s*proxy_read_timeout\s+(\d+)(ms|s|m|h|d)?\s*;", re.MULTILINE
)
_UNIT_SECONDS = {"ms": 0.001, "": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _limit_from(text: str) -> float | None:
    """The `client_max_body_size` a block sets, in bytes, or `None` if it sets none.

    `0` is nginx's spelling of "do not check", returned as infinity so that every
    comparison below treats it as larger than any finite bound.
    """
    match = _BODY_LIMIT.search(text)
    if match is None:
        return None
    size = int(match.group(1)) * _UNIT_BYTES[match.group(2).lower()]
    return math.inf if size == 0 else float(size)


def _read_timeout_from(text: str) -> float | None:
    """The `proxy_read_timeout` a block sets, in seconds, or `None` if it sets none.

    nginx reads a bare number as seconds, and so does this.
    """
    match = _READ_TIMEOUT.search(text)
    if match is None:
        return None
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2) or ""]


def server_level_read_timeout(text: str) -> float | None:
    """The read timeout set on the `server` itself, which a location without its own inherits."""
    remaining = text
    for match in reversed(list(_LOCATION_OPEN.finditer(text))):
        remaining = remaining[: match.start()] + remaining[_block_end(text, match.end()) :]
    return _read_timeout_from(remaining)


def server_level_body_limit(text: str) -> float | None:
    """The limit set on the `server` itself: the text left once every location is removed."""
    remaining = text
    for match in reversed(list(_LOCATION_OPEN.finditer(text))):
        remaining = remaining[: match.start()] + remaining[_block_end(text, match.end()) :]
    return _limit_from(remaining)


def effective_body_limit(location: NginxLocation, server_limit: float | None) -> float:
    """What nginx would enforce for a request served by `location`, in bytes.

    The directive inherits downward, so a location that sets none takes the server's, and one
    that sets neither takes nginx's built-in 1 MiB.
    """
    own = _limit_from(location.body)
    if own is not None:
        return own
    return float(NGINX_DEFAULT_BODY_BYTES) if server_limit is None else server_limit


def resolve_location(locations: list[NginxLocation], path: str) -> NginxLocation | None:
    """The location nginx would select for `path`, following its documented order.

    1. An exact match (`= /x`) wins outright.
    2. Otherwise the longest matching prefix is remembered, and if it carries `^~` it wins.
    3. Otherwise regex locations are tried in file order and the first match wins -- over the
       remembered prefix. This is the rule the ingestion routes' limit depends on: it is why a
       regex location can refine `/v1/` at all, and why the order of two regex blocks matters.
    4. Otherwise the remembered prefix.
    """
    for location in locations:
        if location.modifier == "=" and location.pattern == path:
            return location
    prefix: NginxLocation | None = None
    for location in locations:
        if location.modifier in {"", "^~"} and path.startswith(location.pattern):
            if prefix is None or len(location.pattern) > len(prefix.pattern):
                prefix = location
    if prefix is not None and prefix.modifier == "^~":
        return prefix
    for location in locations:
        if location.is_regex:
            flags = re.IGNORECASE if location.modifier == "~*" else 0
            if re.search(location.pattern, path, flags):
                return location
    return prefix


def _mib(size: float) -> str:
    return "unlimited" if math.isinf(size) else f"{size / MIB:g} MiB"


def body_limit_problems(nginx_text: str) -> list[str]:
    """Violations of the request-body contract, for a comment-stripped nginx config."""
    try:
        locations = parse_nginx_locations(nginx_text)
    except ValueError as error:
        return [str(error)]
    server_limit = server_level_body_limit(nginx_text)
    problems: list[str] = []

    if server_limit is not None and math.isinf(server_limit):
        problems.append("the server block sets `client_max_body_size 0`, which turns the check off")
    for location in locations:
        limit = _limit_from(location.body)
        if limit is not None and math.isinf(limit):
            problems.append(
                f"location '{location.pattern}' sets `client_max_body_size 0`, which turns "
                "the check off; give it a finite bound"
            )

    for path in INGESTION_BODY_ROUTES:
        location = resolve_location(locations, path)
        if location is None or not location.proxied:
            problems.append(
                f"'{path}' is an envelope-carrying route but nginx does not proxy it to the API"
            )
            continue
        limit = effective_body_limit(location, server_limit)
        if limit < INGESTION_MIN_BODY_BYTES:
            problems.append(
                f"'{path}' is served by location '{location.pattern}' with a body limit of "
                f"{_mib(limit)}; a metadata envelope at the synchronous caps needs at least "
                f"{_mib(INGESTION_MIN_BODY_BYTES)}. Give the ingestion routes their own "
                "location with a `client_max_body_size` that large (R11-AUD05)"
            )
        elif limit > INGESTION_MAX_BODY_BYTES:
            problems.append(
                f"'{path}' is served by location '{location.pattern}' with a body limit of "
                f"{_mib(limit)}, above the {_mib(INGESTION_MAX_BODY_BYTES)} ceiling: nginx "
                "buffers and forwards the body before the API authenticates anything, so "
                "this is what each unauthenticated request may cost"
            )

    for path in UPLOAD_BODY_ROUTES:
        location = resolve_location(locations, path)
        if location is None or not location.proxied:
            problems.append(
                f"'{path}' is a file-upload route but nginx does not proxy it to the API"
            )
            continue
        limit = effective_body_limit(location, server_limit)
        if limit != UPLOAD_BODY_BYTES:
            problems.append(
                f"'{path}' is served by location '{location.pattern}' with a body limit of "
                f"{_mib(limit)}; the API accepts up to {_mib(UPLOAD_BODY_BYTES)} there, and the "
                "proxy should admit exactly that much (R11-AUD11)"
            )

    server_timeout = server_level_read_timeout(nginx_text)
    for path in (*INGESTION_BODY_ROUTES, *UPLOAD_BODY_ROUTES):
        location = resolve_location(locations, path)
        if location is None or not location.proxied:
            continue  # already reported above
        timeout = _read_timeout_from(location.body)
        if timeout is None:
            timeout = server_timeout
        if timeout is None or timeout < LONG_REQUEST_READ_TIMEOUT_SECONDS:
            shown = "nginx's 60 s default" if timeout is None else f"{timeout:g} s"
            problems.append(
                f"'{path}' is served by location '{location.pattern}' with a read timeout of "
                f"{shown}; a body this large is parsed and recorded inside the request, so the "
                f"proxy must wait at least {LONG_REQUEST_READ_TIMEOUT_SECONDS} s rather than "
                "answer 504 while the API commits (R11-AUD11)"
            )

    for path in GENERAL_API_ROUTES:
        location = resolve_location(locations, path)
        if location is None or not location.proxied:
            problems.append(f"'{path}' is an API route but nginx does not proxy it to the API")
            continue
        limit = effective_body_limit(location, server_limit)
        if limit > NGINX_DEFAULT_BODY_BYTES:
            problems.append(
                f"'{path}' is not an envelope route (nor an upload route) but is served by "
                f"location "
                f"'{location.pattern}' with a body limit of {_mib(limit)}; only those routes "
                f"may exceed nginx's {_mib(NGINX_DEFAULT_BODY_BYTES)} default -- "
                "loosening `/v1/` generally is not what R11-AUD05 asked for"
            )
    return problems


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


def contract_problems(nginx_text: str, vite_text: str) -> list[str]:
    """Every violation of the proxy contract, for two comment-stripped config texts.

    Kept free of file access so a test can hand it the real configs and then a
    deliberately broken copy of each: a gate that has never been seen to fail is not
    known to work.
    """
    try:
        nginx_api = nginx_proxied_prefixes(nginx_text)
        nginx_all = nginx_declared_locations(nginx_text)
    except ValueError as error:
        return [str(error)]
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

    problems.extend(body_limit_problems(nginx_text))
    return problems


def main() -> int:
    for path in (NGINX_CONF, VITE_CONFIG):
        if not path.is_file():
            print(f"FAIL: {path.relative_to(REPO_ROOT).as_posix()} does not exist")
            return 1

    nginx_text = strip_nginx_comments(NGINX_CONF.read_text(encoding="utf-8"))
    vite_text = strip_js_comments(VITE_CONFIG.read_text(encoding="utf-8"))

    problems = contract_problems(nginx_text, vite_text)

    if problems:
        print("Proxy contract violation:\n")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nEvery public path served by the API must be proxied in BOTH "
            "ui-next/vite.config.ts (development) and ui-next/nginx.conf (production), and "
            "the two envelope-carrying ingestion routes must accept a body at the synchronous "
            "caps and the file-upload routes the API's own upload limit, while the rest of /v1/ "
            "keeps nginx's default.\n"
            "This is a static configuration check; the live proxy behaviour is covered "
            "by the `ui-proxy` job in .github/workflows/ci.yml."
        )
        return 1

    shared = ", ".join(sorted(nginx_proxied_prefixes(nginx_text)))
    print(f"OK: dev and production both proxy the same API path prefixes: {shared}")
    print(
        f"OK: the {len(INGESTION_BODY_ROUTES)} envelope-carrying ingestion routes resolve to a "
        f"location limited to between {_mib(INGESTION_MIN_BODY_BYTES)} and "
        f"{_mib(INGESTION_MAX_BODY_BYTES)}; {len(GENERAL_API_ROUTES)} other /v1/ routes keep "
        f"nginx's {_mib(NGINX_DEFAULT_BODY_BYTES)} default"
    )
    print(
        f"OK: the {len(UPLOAD_BODY_ROUTES)} file-upload routes admit exactly the "
        f"{_mib(UPLOAD_BODY_BYTES)} the API accepts"
    )
    print(
        f"OK: every envelope and upload route waits at least {LONG_REQUEST_READ_TIMEOUT_SECONDS} s "
        "for the API's answer"
    )
    print("(static configuration check -- see the `ui-proxy` CI job for the live test)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
