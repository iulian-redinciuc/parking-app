"""Tiny internal HTTP server every worker runs (api.md §5.2).

Stdlib `http.server` in a daemon thread, port 9000 by default, reachable only inside the
private network. Every request needs `Authorization: Bearer <WORKER_TOKEN>`; without a
configured token the server isn't started at all (never an open control port).

| Method | Path | Handler call |
|--------|------|--------------|
| GET | `/control/snapshot?annotated=true` | `snapshot(annotated) -> JPEG bytes or None` (503) |
| POST | `/control/reload` | `reload() -> dict` (200 JSON; `ValueError` -> 400) |
| POST | `/control/save-reference` | `save_reference() -> dict` (200 JSON; `ValueError` -> 409) |
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)

DEFAULT_PORT = 9000


class ControlHandler(Protocol):
    def snapshot(self, annotated: bool) -> bytes | None: ...

    def reload(self) -> dict[str, Any]: ...

    def save_reference(self) -> dict[str, Any]: ...


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


class ControlServer:
    """`start()` serves in a background thread; `stop()` shuts it down. `port` 0 = any free port."""

    def __init__(self, handler: ControlHandler, token: str, host: str = "0.0.0.0", port: int = 0):
        if not token:
            raise ValueError("the control server needs WORKER_TOKEN")
        self.handler = handler
        self._token = f"Bearer {token}".encode()
        self._server = ThreadingHTTPServer((host, port), self._request_class())
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> ControlServer:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="control", daemon=True
        )
        self._thread.start()
        log.info("control server on port %d", self.port)
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def _authorized(self, header: str | None) -> bool:
        return header is not None and hmac.compare_digest(header.encode(), self._token)

    def _request_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Request(BaseHTTPRequestHandler):
            server_version = "parking-worker"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("control: " + fmt, *args)

            def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, data: dict[str, Any]) -> None:
                self._send(status, json.dumps(data).encode())

            def _route(self, method: str) -> None:
                if not server._authorized(self.headers.get("Authorization")):
                    self._json(401, _err("unauthorized", "missing or wrong worker token"))
                    return
                url = urlsplit(self.path)
                query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                try:
                    if method == "GET" and url.path == "/control/snapshot":
                        jpeg = server.handler.snapshot(_flag(query.get("annotated", "false")))
                        if jpeg is None:
                            self._json(503, _err("unavailable", "no frame read yet"))
                        else:
                            self._send(200, jpeg, "image/jpeg")
                    elif method == "POST" and url.path == "/control/reload":
                        self._call(server.handler.reload, 400, "bad_request")
                    elif method == "POST" and url.path == "/control/save-reference":
                        self._call(server.handler.save_reference, 409, "conflict")
                    else:
                        self._json(404, _err("not_found", f"{method} {url.path}"))
                except Exception:
                    log.exception("control: %s %s failed", method, url.path)
                    self._json(500, _err("internal", "see the worker log"))

            def _call(self, fn, error_status: int, error_code: str) -> None:
                try:
                    self._json(200, fn())
                except ValueError as e:
                    self._json(error_status, _err(error_code, str(e)))

            def do_GET(self) -> None:  # noqa: N802 (http.server naming)
                self._route("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._route("POST")

        return Request


def _err(code: str, message: str) -> dict[str, Any]:
    """The api.md §1 error format."""
    return {"error": {"code": code, "message": message}}
