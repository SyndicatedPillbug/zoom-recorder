#!/usr/bin/env python3
"""Control Center: the non-technical user's window into zoom-recorder.

A local, loopback-only web app (same security model as hud/settings.py: random
per-run token, host check, idle shutdown, no API keys ever sent to the
browser). Four tabs:

  Setup      -- guided first-run check with one-click fixes
  Recordings -- what has been recorded, with transcript/summary links
  Settings   -- Basics (plain language) and Advanced (everything)
  Help       -- plain-language FAQ

Launched from the menu bar; nothing here requires a terminal.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hud.config import (CONFIG_PATH, PROVIDERS, _defaults, _deep_merge,  # noqa: E402
                        config_from_dict, config_to_dict, get_provider,
                        load_config, recorder_defaults, save_config)
from hud.llm import LLMClient, LLMError  # noqa: E402
from hud.recordings import list_recordings  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
CONTROL_PIDFILE = Path.home() / ".zoom_recorder_control.pid"
CONTROL_URLFILE = Path.home() / ".zoom_recorder_control.url"
MIC_SETTINGS_URL = ("x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Microphone")


def _repo(*parts: str) -> Path:
    return REPO.joinpath(*parts)


class ControlApp:
    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 config_path: Optional[Path] = None,
                 log: Optional[Callable[[str], None]] = None,
                 open_browser: bool = True, idle_timeout: float = 1800.0,
                 markers: bool = True, initial_tab: Optional[str] = None) -> None:
        self.host = host
        self.port = port
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self.log = log or (lambda _m: None)
        self.open_browser = open_browser
        self.idle_timeout = idle_timeout
        self.markers = markers
        self.initial_tab = initial_tab or "setup"
        self.token = secrets.token_urlsafe(18)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._last_request = time.time()
        self._stop = threading.Event()
        self._test_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> int:
        handler = type("_BoundHandler", (_Handler,), {"app": self})
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="control-http", daemon=True)
        self._thread.start()
        if self.idle_timeout > 0:
            self._watchdog = threading.Thread(target=self._idle_watch,
                                              name="control-idle", daemon=True)
            self._watchdog.start()
        if self.markers:
            try:
                CONTROL_PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
                CONTROL_URLFILE.write_text(self.url, encoding="utf-8")
            except OSError:
                pass
        self.log("Control Center at {}".format(self.url))
        return self.port

    def _idle_watch(self) -> None:
        while not self._stop.wait(5.0):
            if time.time() - self._last_request > self.idle_timeout:
                self.log("Control Center idle; shutting down")
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
                if CONTROL_PIDFILE.is_file() and CONTROL_PIDFILE.read_text().strip() == str(os.getpid()):
                    CONTROL_PIDFILE.unlink(missing_ok=True)
                CONTROL_URLFILE.unlink(missing_ok=True)
        except OSError:
            pass

    @property
    def url(self) -> str:
        return "http://{}:{}/?token={}&tab={}".format(
            self.host, self.port, self.token, self.initial_tab)

    def open(self) -> None:
        try:
            webbrowser.open(self.url)
        except Exception:  # noqa: BLE001
            pass

    def touch(self) -> None:
        self._last_request = time.time()

    def provider_client(self, name: str, override_key: Optional[str] = None) -> LLMClient:
        provider = get_provider(name)
        key = (override_key if override_key is not None
               else load_config(self.config_path).api_key_for(name))
        return LLMClient(provider.base_url, key, timeout=20.0)


# ------------------------------------------------------------------ actions

def doctor_checks(probe_seconds: float = 1.2) -> List[Dict[str, Any]]:
    from hud import doctor

    checks = [doctor.check_macos(), doctor.check_python()]
    checks += doctor.check_tools()
    checks += [doctor.check_rumps(), doctor.check_blackhole(), doctor.check_routing(),
               doctor.check_output_volume(), doctor.check_microphone(probe_seconds),
               doctor.check_transcription()]
    return [{"name": c.name, "ok": c.ok, "detail": c.detail,
             "fix": c.fix, "critical": c.critical} for c in checks]


def run_test_recording(seconds: float = 10.0, mode: str = "both") -> Dict[str, Any]:
    """Record a short clip with the real recorder and report what was captured.

    This is the wizard's proof step: it exercises the exact production path
    (routing setup, capture, verification, auto-restore) and returns coverage
    numbers plus the mixed file for playback.
    """
    tmp = Path(tempfile.mkdtemp(prefix="zoomrec_test_"))
    cmd = [sys.executable, str(_repo("zoom_record.py")), "0.5", "--no-transcribe",
           "--basedir", str(tmp), "--system-capture", "loopback"]
    if mode == "mic":
        cmd.append("--no-system")
    elif mode == "system":
        cmd.append("--system-only")
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    try:
        proc = subprocess.Popen(cmd, cwd=str(REPO), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return {"ok": False, "error": "could not start the recorder: {}".format(exc)}
    time.sleep(max(4.0, float(seconds)))
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)

    sessions = sorted(tmp.glob("*/*"), reverse=True)
    if not sessions:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "error": "no recording was produced"}

    session = sessions[0]
    log_text = ""
    try:
        log_text = (session / "capture.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass

    def coverage(label: str) -> Optional[float]:
        import re
        match = re.search(r"\[{}\].*?Coverage:\s*(\d+)%".format(label), log_text)
        return float(match.group(1)) if match else None

    mic_cov = coverage("mic")
    sys_cov = coverage("system")
    mixed = session / "derived" / "recording_mixed.wav"
    expected = []
    if mode in ("both", "mic"):
        expected.append(mic_cov)
    if mode in ("both", "system"):
        expected.append(sys_cov)
    ok = all(c is not None and c >= 25 for c in expected) and any(
        p.is_file() for p in (session / "recording_mic.wav", session / "recording_sys.wav"))
    return {
        "ok": bool(ok),
        "mode": mode,
        "session": str(session),
        "temp_root": str(tmp),
        "mic_coverage": mic_cov,
        "system_coverage": sys_cov,
        "mixed": str(mixed) if mixed.is_file() else None,
        "log_tail": log_text.strip().splitlines()[-6:],
    }


def play_file(path: str) -> Dict[str, Any]:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        return {"ok": False, "error": "file not found"}
    try:
        subprocess.Popen(["afplay", str(candidate)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


# ------------------------------------------------------------------ handler

class _Handler(BaseHTTPRequestHandler):
    server_version = "zoom-recorder-control/0.1"
    protocol_version = "HTTP/1.1"

    app: ControlApp

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

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

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._host_ok():
            self._reject(403, "bad host")
            return
        if parsed.path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "control.html", "text/html; charset=utf-8")
            return
        if not self._token_ok(parsed):
            self._reject(403, "bad token")
            return
        self.app.touch()
        if parsed.path == "/api/status":
            self._status()
        elif parsed.path == "/api/recordings":
            self._recordings()
        elif parsed.path == "/api/devices":
            self._devices()
        elif parsed.path == "/api/config":
            self._send_json(self._config_payload())
        elif parsed.path == "/health":
            self._send_json({"ok": True})
        else:
            self.send_error(404, "not found")

    # -- POST --------------------------------------------------------------
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
            elif parsed.path == "/api/fix":
                self._fix(body)
            elif parsed.path == "/api/open":
                self._open(body)
            elif parsed.path == "/api/mic-test":
                self._mic_test(body)
            elif parsed.path == "/api/test-recording":
                self._test_recording(body)
            elif parsed.path == "/api/play":
                self._send_json(play_file(str(body.get("path") or "")))
            elif parsed.path == "/api/test-provider":
                self._test_provider(body)
            elif parsed.path == "/api/models":
                self._models(body)
            elif parsed.path == "/api/pick-dir":
                self._pick_dir()
            elif parsed.path == "/api/login-agent":
                self._login_agent(body)
            elif parsed.path == "/api/setup/complete":
                self._setup_complete()
            elif parsed.path == "/api/quit":
                self._send_json({"ok": True})
                self.app._shutdown_async()
            else:
                self.send_error(404, "not found")
        except LLMError as exc:
            self._send_json({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, status=500)

    # -- handlers ----------------------------------------------------------
    def _status(self) -> None:
        from hud import routing_fix
        cfg = load_config(self.app.config_path)
        try:
            checks = doctor_checks()
        except Exception as exc:  # noqa: BLE001
            checks = [{"name": "checks", "ok": False, "detail": str(exc),
                       "fix": "", "critical": True}]
        pidfile = Path.home() / ".zoom_recorder.pid"
        recording = False
        if pidfile.is_file():
            try:
                os.kill(int(pidfile.read_text().strip()), 0)
                recording = True
            except (OSError, ValueError):
                recording = False
        self._send_json({
            "ok": True,
            "checks": checks,
            "recording": recording,
            "routing_active": routing_fix.is_loopback_active(),
            "volume": routing_fix.get_output_volume(),
            "mode": cfg.recorder.mode,
            "onboarded": cfg.onboarded,
            "offline": cfg.offline,
            "basedir": cfg.recorder.basedir,
            "config_path": str(self.app.config_path),
        })

    def _recordings(self) -> None:
        cfg = load_config(self.app.config_path)
        items = [r.as_dict() for r in list_recordings(cfg.recorder.basedir)]
        self._send_json({"ok": True, "basedir": cfg.recorder.basedir, "items": items})

    def _devices(self) -> None:
        from hud.devices import read_system_profiler
        from hud import routing_fix
        from zoom_record import list_devices
        inputs, _outputs = list_devices()
        topo = read_system_profiler()
        mics = [d.name for d in inputs if topo.looks_like_mic(d.name)]
        outputs = routing_fix.list_real_outputs(topo)
        self._send_json({"ok": True, "microphones": mics, "outputs": outputs})

    def _config_payload(self) -> Dict[str, Any]:
        cfg = load_config(self.app.config_path)
        data = config_to_dict(cfg, include_keys=False)
        keys_set = {name: bool(cfg.api_key_for(name))
                    for name in PROVIDERS if get_provider(name).api_key_env}
        return {"ok": True, "config": data, "api_keys_set": keys_set,
                "providers": sorted(PROVIDERS.keys()),
                "config_path": str(self.app.config_path)}

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

    def _fix(self, body: Dict[str, Any]) -> None:
        from hud import routing_fix
        action = str(body.get("action") or "")
        if action == "routing":
            output = body.get("output") or None
            result = routing_fix.fix_routing(physical_output=output, assume_yes=True)
        elif action == "restore":
            result = routing_fix.restore_routing()
        elif action == "open-mic-settings":
            subprocess.Popen(["open", MIC_SETTINGS_URL])
            self._send_json({"ok": True})
            return
        elif action == "install-whisper":
            subprocess.Popen(["osascript", "-e",
                              'tell application "Terminal" to do script '
                              '"brew install whisper.cpp"'])
            self._send_json({"ok": True,
                             "message": "Installing whisper.cpp in Terminal..."})
            return
        elif action == "download-model":
            model = str(body.get("model") or "ggml-base.en.bin")
            dest = Path.home() / ".cache" / "whisper-cpp" / model
            url = ("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
                   + model)
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["curl", "-L", "--fail", "-o", str(dest), url])
            self._send_json({"ok": True, "path": str(dest),
                             "message": "Downloading {} (about 150 MB)...".format(model)})
            return
        elif action == "open-advanced":
            subprocess.Popen([sys.executable, str(_repo("settings.py"))],
                             cwd=str(REPO))
            self._send_json({"ok": True})
            return
        elif action == "open-terminal":
            command = str(body.get("command") or "")
            if not command:
                self._send_json({"ok": False, "error": "no command"})
                return
            subprocess.Popen(["osascript", "-e",
                              'tell application "Terminal" to do script "{}"'.format(
                                  command.replace('"', '\\"'))])
            self._send_json({"ok": True})
            return
        else:
            self._send_json({"ok": False, "error": "unknown action"})
            return
        self._send_json({"ok": result.ok, "changed": result.changed,
                         "message": result.message})

    def _open(self, body: Dict[str, Any]) -> None:
        target = str(body.get("target") or "path")
        if target == "recordings":
            cfg = load_config(self.app.config_path)
            path = Path(cfg.recorder.basedir).expanduser()
        else:
            path = Path(str(body.get("path") or "")).expanduser()
        # Only allow opening things under the user's home.
        try:
            path.resolve().relative_to(Path.home())
        except ValueError:
            self._send_json({"ok": False, "error": "path outside home"})
            return
        if not path.exists():
            self._send_json({"ok": False, "error": "not found: {}".format(path)})
            return
        subprocess.Popen(["open", str(path)])
        self._send_json({"ok": True})

    def _mic_test(self, body: Dict[str, Any]) -> None:
        from zoom_record import list_devices, probe_level
        from hud.devices import read_system_profiler
        inputs, _ = list_devices()
        topo = read_system_profiler()
        real = [d for d in inputs if topo.looks_like_mic(d.name)]
        default = topo.device(topo.default_input)
        device = next((d for d in real if default and d.name == default.name),
                      real[0] if real else None)
        if device is None:
            self._send_json({"ok": False, "error": "no microphone found"})
            return
        probe = probe_level(device, 1.2, max_wait=4.0)
        self._send_json({"ok": bool(probe.ok), "device": device.name,
                         "max_db": probe.max_db,
                         "silent": (probe.max_db is None or probe.max_db < -80.0),
                         "error": probe.error})

    def _test_recording(self, body: Dict[str, Any]) -> None:
        seconds = float(body.get("seconds") or 10.0)
        mode = str(body.get("mode") or "both")
        with self.app._test_lock:
            result = run_test_recording(seconds, mode)
        self._send_json(result)

    def _test_provider(self, body: Dict[str, Any]) -> None:
        name = str(body.get("provider") or "")
        override = body.get("api_key") or None
        models = self.app.provider_client(name, override).models()
        self._send_json({"ok": True, "count": len(models), "sample": models[:5]})

    def _models(self, body: Dict[str, Any]) -> None:
        name = str(body.get("provider") or "")
        override = body.get("api_key") or None
        self._send_json({"ok": True,
                         "models": self.app.provider_client(name, override).models()})

    def _pick_dir(self) -> None:
        script = ('POSIX path of (choose folder with prompt '
                  '"Choose where recordings should be saved")')
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

    def _login_agent(self, body: Dict[str, Any]) -> None:
        enable = bool(body.get("enabled"))
        script = _repo("install-launch-agent.sh")
        cmd = [str(script)] if enable else [str(script), "--disable"]
        proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                              timeout=60)
        self._send_json({"ok": proc.returncode == 0, "enabled": enable,
                         "output": (proc.stdout or proc.stderr).strip()})

    def _setup_complete(self) -> None:
        cfg = load_config(self.app.config_path)
        cfg.onboarded = True
        save_config(cfg, self.app.config_path)
        self._send_json({"ok": True})


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="zoom-recorder Control Center")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--tab", default=None)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    app = ControlApp(port=args.port,
                     config_path=Path(args.config) if args.config else None,
                     open_browser=not args.no_browser, idle_timeout=args.timeout,
                     initial_tab=args.tab,
                     log=lambda m: print("[control] " + m, flush=True))
    app.start()
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
