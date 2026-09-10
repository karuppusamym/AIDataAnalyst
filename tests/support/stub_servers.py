"""Real local servers for delivery tests: HTTP, syslog/UDP and syslog/TCP.

F04's acceptance criterion is that *destination receipt is demonstrated*. A
mock of our own transport cannot demonstrate that -- it is precisely the thing
under test, since the defect was a function that reported success without
contacting anything. These are therefore real servers on real loopback
sockets: the transport opens a connection, writes bytes, and the bytes appear
here. What each stub records is what actually crossed the socket.

All three bind to 127.0.0.1 on port 0 and report the port the OS gave them, so
tests never collide and never need a fixed port. Each runs its accept loop on a
daemon thread and is used as a context manager.

`fail_status` / `drop` let a test make a destination behave like an outage and
then recover, which is the other half of F04 and F12: a message queued while a
destination was down must still arrive once it comes back.
"""

from __future__ import annotations

import json
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

_LOOPBACK = "127.0.0.1"


class WebhookStub:
    """An HTTP endpoint that records every POST body it receives.

    `fail_status`, when set, is returned instead of 200 -- an outage a test can
    end by setting it back to None. `delay_seconds` makes the handler sleep so
    a client-side timeout can be exercised against a server that is up but slow.
    """

    def __init__(self, *, fail_status: int | None = None, delay_seconds: float = 0.0) -> None:
        self.received: list[dict[str, Any]] = []
        self.raw: list[bytes] = []
        self.fail_status = fail_status
        self.delay_seconds = delay_seconds
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None, "stub is not running"
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/hook"

    def __enter__(self) -> WebhookStub:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if stub.delay_seconds:
                    threading.Event().wait(stub.delay_seconds)
                stub.raw.append(body)
                try:
                    stub.received.append(json.loads(body.decode("utf-8")))
                except ValueError:
                    pass
                status = stub.fail_status or 200
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                """Silence the default stderr access log."""

        self._server = ThreadingHTTPServer((_LOOPBACK, 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


class SyslogUdpStub:
    """A UDP socket that records every datagram it receives."""

    def __init__(self) -> None:
        self.datagrams: list[bytes] = []
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def port(self) -> int:
        assert self._sock is not None, "stub is not running"
        return int(self._sock.getsockname()[1])

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(65535)
            except (TimeoutError, OSError):
                continue
            self.datagrams.append(data)

    def __enter__(self) -> SyslogUdpStub:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((_LOOPBACK, 0))
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._sock is not None:
            self._sock.close()

    def wait_for(self, count: int, *, timeout: float = 5.0) -> list[bytes]:
        deadline = threading.Event()
        waited = 0.0
        while len(self.datagrams) < count and waited < timeout:
            deadline.wait(0.05)
            waited += 0.05
        return list(self.datagrams)


class SyslogTcpStub:
    """A TCP listener that records every connection's full byte stream.

    Octet-counted framing (RFC 6587 §3.4.1) is deliberately *not* parsed here:
    the test asserts on the raw stream, so a wrong length prefix is visible
    rather than silently corrected by the stub.
    """

    def __init__(self) -> None:
        self.streams: list[bytes] = []
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None, "stub is not running"
        return int(self._server.server_address[1])

    def __enter__(self) -> SyslogTcpStub:
        stub = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                chunks: list[bytes] = []
                self.request.settimeout(2.0)
                while True:
                    try:
                        chunk = self.request.recv(4096)
                    except (TimeoutError, OSError):
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                stub.streams.append(b"".join(chunks))

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server((_LOOPBACK, 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def wait_for(self, count: int, *, timeout: float = 5.0) -> list[bytes]:
        event = threading.Event()
        waited = 0.0
        while len(self.streams) < count and waited < timeout:
            event.wait(0.05)
            waited += 0.05
        return list(self.streams)


def unused_tcp_port() -> int:
    """A port nothing is listening on -- a destination that refuses.

    Binding and immediately closing leaves the number free, so a connection to
    it is refused rather than accepted. That is the cheapest honest way to
    exercise the "destination is down" path without a firewall.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_LOOPBACK, 0))
        return int(sock.getsockname()[1])
