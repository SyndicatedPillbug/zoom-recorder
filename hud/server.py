#!/usr/bin/env python3
"""Local HTTP server for the HUD.

Binds to 127.0.0.1 on an ephemeral port by default and serves a single
two-column page plus a Server-Sent Events stream. Kept on a background thread
so it never competes with the recorder's main loop.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .state import LiveState

STATIC_DIR = Path(__file__).resolve().parent / "static"


class _Handler(BaseHTTPRequestHandler):
    server_version = "zoom-recorder-hud/0.1"
    protocol_version = "HTTP/1.1"

    # injected by HudServer
    state: LiveState
    logger: Optional[Callable[[str], None]] = None

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.logger:
            try:
                self.logger("hud: " + (fmt % args))
            except Exception:  # noqa: BLE001
                pass

    # -- helpers -----------------------------------------------------------
    def _send_json(self, obj: Any, status: int = 200) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "hud.html", "text/html; charset=utf-8")
        elif path == "/state":
            query = parse_qs(parsed.query)
            since = query.get("since", ["0"])[0]
            try:
                since_id = int(since)
            except ValueError:
                since_id = 0
            self._send_json(self.state.snapshot() if since_id == 0 else {
                "events": self.state.since(since_id),
                "status": self.state.status,
                "budget": self.state.budget,
                "meta": self.state.meta,
            })
        elif path == "/events":
            self._stream_events(parsed)
        elif path == "/health":
            self._send_json({"ok": True, "status": self.state.status})
        else:
            self.send_error(404, "not found")

    def _stream_events(self, parsed: Any) -> None:
        query = parse_qs(parsed.query)
        try:
            last_id = int(query.get("since", ["0"])[0])
        except ValueError:
            last_id = 0
        if last_id == 0:
            last_id = self.state.latest_id()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Prime the client with current status/budget.
        self._write_sse({"type": "status", "status": self.state.status,
                         "meta": self.state.meta, "budget": self.state.budget})
        try:
            while True:
                events = self.state.wait_for(last_id, timeout=12.0)
                if events:
                    for event in events:
                        self._write_sse(event)
                        last_id = event["id"]
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:  # noqa: BLE001
            return

    def _write_sse(self, obj: Any) -> None:
        data = "data: {}\n\n".format(json.dumps(obj)).encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()


class HudServer:
    def __init__(self, state: LiveState, host: str = "127.0.0.1", port: int = 0,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.log = log
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> int:
        handler = type("_BoundHandler", (_Handler,), {"state": self.state, "logger": self.log})
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="hud-http", daemon=True)
        self._thread.start()
        if self.log:
            self.log("HUD serving at {}".format(self.url))
        return self.port

    @property
    def url(self) -> str:
        return "http://{}:{}/".format(self.host, self.port)

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None


def small_sleep() -> None:
    time.sleep(0.05)
