#!/usr/bin/env python3
"""Local HTTP server for the HUD.

Binds to 127.0.0.1 on an ephemeral port by default and serves a single
two-column page plus a Server-Sent Events stream. Kept on a background thread
so it never competes with the recorder's main loop.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .local_http import bind_local_server, host_from_header, url_host
from .state import LiveState

STATIC_DIR = Path(__file__).resolve().parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "zoom-recorder-hud/0.1"
    protocol_version = "HTTP/1.1"

    # injected by HudServer
    state: LiveState
    logger: Optional[Callable[[str], None]] = None
    token: str = ""
    on_ask: Optional[Callable[[str, bool], bool]] = None
    on_pause: Optional[Callable[[bool], None]] = None

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.logger:
            try:
                self.logger("hud: " + (fmt % args))
            except Exception:  # noqa: BLE001
                pass

    # -- guards ------------------------------------------------------------
    def _host_ok(self) -> bool:
        host = host_from_header(self.headers.get("Host") or "")
        return host in ALLOWED_HOSTS

    def _token_ok(self, parsed: Any) -> bool:
        if not self.token:
            return True
        query = parse_qs(parsed.query)
        provided = (query.get("token", [""])[0]
                    or self.headers.get("X-Auth-Token", ""))
        return secrets.compare_digest(provided, self.token)

    def _reject(self, code: int, message: str) -> None:
        payload = json.dumps({"ok": False, "error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._host_ok() or not self._token_ok(parsed):
            self._reject(403, "forbidden")
            return
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

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._host_ok() or not self._token_ok(parsed):
            self._reject(403, "forbidden")
            return
        body = self._read_body()
        if parsed.path == "/ask":
            text = str(body.get("text") or "").strip()
            expand = bool(body.get("expand"))
            ok = bool(self.on_ask and text and self.on_ask(text, expand))
            self._send_json({"ok": ok})
        elif parsed.path == "/pause":
            paused = bool(body.get("paused", True))
            if self.on_pause:
                self.on_pause(paused)
            self._send_json({"ok": True, "paused": paused})
        elif parsed.path == "/stop":
            if self.on_stop:
                self.on_stop()
            self._send_json({"ok": True})
        elif parsed.path == "/open-recordings":
            import subprocess
            try:
                from .config import recorder_defaults
                path = os.path.expanduser(recorder_defaults().basedir)
                subprocess.Popen(["open", path])
                self._send_json({"ok": True, "path": path})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(exc)})
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
                 log: Optional[Callable[[str], None]] = None, token: str = "",
                 on_ask: Optional[Callable[[str, bool], bool]] = None,
                 on_pause: Optional[Callable[[bool], None]] = None,
                 on_stop: Optional[Callable[[], None]] = None) -> None:
        self.state = state
        self.host = host
        self.port = port
        self.log = log
        self.token = token
        self.on_ask = on_ask
        self.on_pause = on_pause
        self.on_stop = on_stop
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> int:
        # Wrap callables in staticmethod so assigning them as class attributes
        # does not turn them into bound methods (which would pass `self`).
        handler = type("_BoundHandler", (_Handler,), {
            "state": self.state,
            "logger": staticmethod(self.log) if self.log else None,
            "token": self.token,
            "on_ask": staticmethod(self.on_ask) if self.on_ask else None,
            "on_pause": staticmethod(self.on_pause) if self.on_pause else None,
            "on_stop": staticmethod(self.on_stop) if self.on_stop else None,
        })
        self._httpd, self.host = bind_local_server(handler, self.host, self.port)
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
        base = "http://{}:{}/".format(url_host(self.host), self.port)
        return "{}?token={}".format(base, self.token) if self.token else base

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
