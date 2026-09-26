"""HTTP front end for the control console.

The server is intentionally small: it parses the request, hands ``/api`` calls
to the console and serves the static console pages from ``web_root``.
"""

from __future__ import annotations

import json
import logging
import posixpath
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .console import Console, Request

LOGGER = logging.getLogger("cranefleet.server")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class ControlServer(ThreadingHTTPServer):
    """Threaded server so concurrent crane reports exercise the interlocks."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], console: Console) -> None:
        super().__init__(address, ControlRequestHandler)
        self.console = console


class ControlRequestHandler(BaseHTTPRequestHandler):
    server: ControlServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._handle("POST")

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        if path.startswith("/api"):
            body, valid = self._read_body()
            request = Request(
                method=method,
                path=path,
                query=parse_qs(parsed.query),
                body=body,
                body_valid=valid,
            )
            status, payload = self.server.console.dispatch(request)
            self._write_json(status, payload)
            return
        self._serve_static(path)

    def _read_body(self) -> tuple[dict[str, Any], bool]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}, True
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}, False
        if not isinstance(parsed, dict):
            return {}, False
        return parsed, True

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        web_root = Path(self.server.console.web_root).resolve()
        relative = posixpath.normpath(unquote(path).lstrip("/"))
        if relative in ("", "."):
            relative = "index.html"
        target = (web_root / relative).resolve()
        if web_root != target and web_root not in target.parents:
            self._write_plain(403, "forbidden")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._write_plain(404, "not found")
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_plain(self, status: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(console: Console, host: str = "127.0.0.1", port: int = 8080) -> ControlServer:
    """Create (but do not start) the HTTP server for ``console``."""

    return ControlServer((host, port), console)


def run(console: Console, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Serve until interrupted."""

    server = serve(console, host, port)
    LOGGER.info("CraneFleet listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
