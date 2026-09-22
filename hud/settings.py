#!/usr/bin/env python3
"""Standalone local settings GUI for the HUD.

Serves a small form on 127.0.0.1 that reads and writes
``~/.config/zoom-recorder/config.json``. It deliberately:

  * binds loopback only and requires a random per-run token (so another local
    page or a DNS-rebinding attempt cannot read your settings/keys);
  * never sends stored API keys to the browser (only configured / not set);
  * writes the config atomically with a backup, preserving unknown keys;
  * shuts itself down when idle, so there is no persistent background process.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .config import (CONFIG_PATH, PROVIDERS, config_from_dict, config_to_dict,
                     get_provider, load_config, save_config, _defaults, _deep_merge)
from .llm import LLMClient, LLMError

STATIC_DIR = Path(__file__).resolve().parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
SETTINGS_PIDFILE = Path.home() / ".zoom_recorder_settings.pid"
SETTINGS_URLFILE = Path.home() / ".zoom_recorder_settings.url"


class SettingsApp:
    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 config_path: Optional[Path] = None,
                 log: Optional[Callable[[str], None]] = None,
                 open_browser: bool = True, idle_timeout: float = 900.0,
                 markers: bool = True) -> None:
        self.host = host
        self.port = port
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self.log = log or (lambda _m: None)
        self.open_browser = open_browser
        self.idle_timeout = idle_timeout
        self.markers = markers
        self.token = secrets.token_urlsafe(18)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._last_request = time.time()
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> int:
        handler = type("_BoundHandler", (_Handler,), {"app": self})
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="settings-http", daemon=True)
        self._thread.start()
        if self.idle_timeout > 0:
            self._watchdog = threading.Thread(target=self._idle_watch, name="settings-idle",
                                              daemon=True)
            self._watchdog.start()
        if self.markers:
            try:
                SETTINGS_PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
                SETTINGS_URLFILE.write_text(self.url, encoding="utf-8")
                os.chmod(str(SETTINGS_PIDFILE), 0o600)
                os.chmod(str(SETTINGS_URLFILE), 0o600)
            except OSError:
                pass
        self.log("settings GUI at {}".format(self.url))
        return self.port

    def _idle_watch(self) -> None:
        while not self._stop.wait(5.0):
            if time.time() - self._last_request > self.idle_timeout:
                self.log("settings GUI idle for {:.0f}s; shutting down".format(self.idle_timeout))
                self._shutdown_async()
                return

    def _shutdown_async(self) -> None:
        def _do() -> None:
            time.sleep(0.1)
            self.stop()
        threading.Thread(target=_do, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._httpd = None
        for thread in (self._thread, self._watchdog):
            if thread is not None:
                thread.join(timeout=3.0)
        self._thread = None
        self._watchdog = None
        try:
            if self.markers:
                if SETTINGS_PIDFILE.is_file() and SETTINGS_PIDFILE.read_text().strip() == str(os.getpid()):
                    SETTINGS_PIDFILE.unlink(missing_ok=True)
                SETTINGS_URLFILE.unlink(missing_ok=True)
        except OSError:
            pass

    @property
    def url(self) -> str:
        return "http://{}:{}/?token={}".format(self.host, self.port, self.token)

    def open(self) -> None:
        try:
            webbrowser.open(self.url)
        except Exception:  # noqa: BLE001
            pass

    # -- request helpers ---------------------------------------------------
    def touch(self) -> None:
        self._last_request = time.time()

    def provider_client(self, provider_name: str,
                        override_key: Optional[str] = None) -> LLMClient:
        provider = get_provider(provider_name)
        key = override_key if override_key is not None else load_config(self.config_path).api_key_for(provider_name)
        return LLMClient(provider.base_url, key, timeout=20.0)


class _Handler(BaseHTTPRequestHandler):
    server_version = "zoom-recorder-settings/0.1"
    protocol_version = "HTTP/1.1"

    app: SettingsApp

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    # -- guards ------------------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ALLOWED_HOSTS

    def _token_ok(self, parsed: Any) -> bool:
        query = parse_qs(parsed.query)
        provided = (query.get("token", [""])[0]
                    or self.headers.get("X-Auth-Token", ""))
        return secrets.compare_digest(provided, self.app.token)

    def _reject(self, code: int, message: str) -> None:
        payload = json.dumps({"ok": False, "error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # -- responses ---------------------------------------------------------
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

    def _read_body(self) -> Dict[str, Any]:
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
        if not self._host_ok():
            self._reject(403, "bad host")
            return
        if parsed.path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "settings.html", "text/html; charset=utf-8")
            return
        if not self._token_ok(parsed):
            self._reject(403, "bad token")
            return
        self.app.touch()
        if parsed.path == "/api/config":
            self._send_json(self._config_payload())
        elif parsed.path == "/health":
            self._send_json({"ok": True})
        else:
            self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._host_ok():
            self._reject(403, "bad host")
            return
        if not self._token_ok(parsed):
            self._reject(403, "bad token")
            return
        self.app.touch()
        body = self._read_body()
        try:
            if parsed.path == "/api/config":
                self._save_config(body)
            elif parsed.path == "/api/test":
                self._test_provider(body)
            elif parsed.path == "/api/models":
                self._models(body)
            elif parsed.path == "/api/pick-dir":
                self._pick_dir()
            elif parsed.path == "/api/quit":
                self._send_json({"ok": True})
                self.app._shutdown_async()
            else:
                self.send_error(404, "not found")
        except LLMError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=200)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, status=500)

    # -- handlers ----------------------------------------------------------
    def _config_payload(self) -> Dict[str, Any]:
        cfg = load_config(self.app.config_path)
        data = config_to_dict(cfg, include_keys=False)
        keys_set = {name: bool(cfg.api_key_for(name))
                    for name in PROVIDERS
                    if get_provider(name).api_key_env}
        return {
            "ok": True,
            "config": data,
            "api_keys_set": keys_set,
            "providers": sorted(PROVIDERS.keys()),
            "config_path": str(self.app.config_path),
        }

    def _save_config(self, body: Dict[str, Any]) -> None:
        incoming = body.get("config") or {}
        merged = _deep_merge(_defaults(), incoming)
        cfg = config_from_dict(merged)

        existing = load_config(self.app.config_path).api_keys
        keys = dict(existing)
        for name in body.get("api_key_clear") or []:
            keys[str(name)] = ""
        for name, value in (body.get("api_key_new") or {}).items():
            if value:
                keys[str(name)] = str(value)
        path = save_config(cfg, self.app.config_path, api_keys=keys)
        self._send_json({"ok": True, "path": str(path)})

    def _test_provider(self, body: Dict[str, Any]) -> None:
        name = str(body.get("provider") or "")
        override = body.get("api_key") or None
        client = self.app.provider_client(name, override)
        models = client.models()
        self._send_json({"ok": True, "count": len(models),
                         "sample": models[:5]})

    def _models(self, body: Dict[str, Any]) -> None:
        name = str(body.get("provider") or "")
        override = body.get("api_key") or None
        client = self.app.provider_client(name, override)
        self._send_json({"ok": True, "models": client.models()})

    def _pick_dir(self) -> None:
        script = ('POSIX path of (choose folder with prompt '
                  '"Choose a notes folder for the knowledge base")')
        try:
            proc = subprocess.run(["osascript", "-e", script],
                                  capture_output=True, text=True, timeout=120)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)})
            return
        if proc.returncode != 0:
            self._send_json({"ok": False, "error": "cancelled"})
            return
        self._send_json({"ok": True, "path": proc.stdout.strip()})


def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="zoom-recorder HUD settings GUI")
    parser.add_argument("--port", type=int, default=0, help="port (default: random)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="idle seconds before shutting down (0 = never)")
    parser.add_argument("--config", default=None, help="config path override")
    args = parser.parse_args(argv)

    app = SettingsApp(port=args.port, config_path=Path(args.config) if args.config else None,
                      open_browser=not args.no_browser, idle_timeout=args.timeout,
                      log=lambda m: print("[settings] " + m, flush=True))
    port = app.start()
    url_no_tok = "http://127.0.0.1:{}/?token=…".format(port)
    print("Settings GUI: {}".format(url_no_tok), flush=True)
    if not args.no_browser:
        app.open()
    try:
        while app._thread is not None and app._thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
