"""A stub upstream for the live `ui-proxy` CI check. Not production code.

`scripts/check_proxy_contract.py` compares two configuration files and can only
prove they agree. This stub is the other half: the `ui-proxy` job in
`.github/workflows/ci.yml` runs the real ui-next image (nginx serving the built
SPA with `ui-next/nginx.conf`) in front of this process, then asserts that
`/mcp` and `/v1/...` arrive HERE while every other path gets the SPA shell.
That is what F07 actually broke -- `/mcp` silently returned `index.html`.

It answers every request with a marker line naming the method and path, so the
assertion in CI is a plain string match with no JSON parsing. It never binds
anything but the loopback-reachable container port and holds no state.

Usage::

    python scripts/proxy_contract_stub_api.py [port]   # default 8000
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MARKER = "ATLAS-STUB-UPSTREAM"


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        authorization = "present" if self.headers.get("Authorization") else "absent"
        body = (
            f"{MARKER} method={self.command} path={self.path} authorization={authorization}\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond
    do_PUT = _respond
    do_DELETE = _respond
    do_PATCH = _respond

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("stub-api: " + (format % args) + "\n")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    # S104: binding all interfaces is the point -- this runs inside a throwaway
    # CI container that nginx reaches by its Docker network alias.
    server = ThreadingHTTPServer(("0.0.0.0", port), StubHandler)  # noqa: S104
    sys.stderr.write(f"stub-api: listening on 0.0.0.0:{port}\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
