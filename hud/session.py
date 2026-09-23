#!/usr/bin/env python3
"""Wires the HUD subsystems together and owns their lifecycle.

``zoom_record.py`` only ever calls :meth:`LiveSession.start` and
:meth:`LiveSession.stop`. Everything else -- the HTTP server, live tap, STT and
answer engine -- is managed here. All failures are contained: a HUD that cannot
start leaves the recording completely unaffected.
"""

from __future__ import annotations

import os
import json
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .answers import AnswerEngine
from .budget import BudgetGovernor
from .config import HudConfig
from .identity import build_identity, meaningful_folder_name, write_identity
from .local_http import url_host
from .server import HudServer
from .state import LiveState
from .stt import LiveTranscriber
from .transcript_writeback import TranscriptWriteback

HUD_URLFILE = Path.home() / ".zoom_recorder_hud.url"


class LiveSession:
    def __init__(self, cfg: HudConfig, outdir: Path, log: Callable[[str], None],
                 mic_name: Optional[str], system_name: Optional[str] = None,
                 model_path: Optional[Path] = None,
                 on_stop: Optional[Callable[[], None]] = None,
                 started_at: Optional[datetime] = None) -> None:
        self.cfg = cfg
        self.outdir = Path(outdir)
        self.log = log
        self.mic_name = mic_name
        self.system_name = system_name
        self.model_path = model_path
        self.on_stop = on_stop
        self.started_at = started_at or datetime.now()

        self.state = LiveState()
        self.budget = BudgetGovernor(cfg.budget_tpm, cfg.budget_tpd)
        self.token = secrets.token_urlsafe(18)
        self.server: Optional[HudServer] = None
        self.stt: Optional[LiveTranscriber] = None
        self.answers: Optional[AnswerEngine] = None
        self._started = False
        self._port = 0
        self._stop = threading.Event()
        self._flush_thread: Optional[threading.Thread] = None
        self._writeback: Optional[TranscriptWriteback] = None
        self._native_proc: Optional[subprocess.Popen] = None
        self._hud_surface = "none"
        self._lifecycle_stages: Dict[str, float] = {}

    def _record_lifecycle_stage(self, name: str, started_at: float) -> None:
        self._lifecycle_stages[str(name)] = round(max(0.0, time.time() - started_at), 4)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> Optional[int]:
        # Enforce the privacy switch before any network-capable component
        # starts: offline blocks every non-loopback HTTP call in the process.
        try:
            from .llm import set_offline
            set_offline(self.cfg.offline)
        except Exception:  # noqa: BLE001
            pass
        if self.cfg.offline:
            self.log("offline mode: network access disabled (loopback only)")
        try:
            self.server = HudServer(self.state, self.cfg.host, self.cfg.port, self.log,
                                    token=self.token, on_ask=self._on_ask,
                                    on_pause=self._on_pause,
                                    on_speaker_label=self._on_speaker_label,
                                    on_stop=self.on_stop)
            self._port = self.server.start()
        except Exception as exc:  # noqa: BLE001
            self.log("live HUD: HTTP server failed ({}); continuing without HUD".format(exc))
            self.server = None
            return None

        self.state.set_status("recording", answers_backend=self.cfg.answers_backend,
                              answers_enabled=self.cfg.answers_enabled,
                              stt_backend=self.cfg.stt_backend)
        self.state.set_budget(self.budget.snapshot())
        self._publish_devices()

        if self.cfg.transcript_writeback_dir:
            self._writeback = TranscriptWriteback(
                self.state, self.cfg.transcript_writeback_dir,
                self.outdir.name, self.log)
            if not self._writeback.start():
                self._writeback = None

        try:
            self.stt = LiveTranscriber(self.state, self.log, self.cfg,
                                       self.mic_name, self.system_name, self.model_path)
            self.stt.start()
        except Exception as exc:  # noqa: BLE001
            self.log("live HUD: transcription failed to start ({})".format(exc))
            self.state.set_status("recording", stt_error=str(exc))

        try:
            self.answers = AnswerEngine(self.state, self.log, self.cfg, self.budget,
                                        outdir=self.outdir)
            self.answers.start()
        except Exception as exc:  # noqa: BLE001
            self.log("live HUD: answer engine failed to start ({})".format(exc))
            self.state.set_status("recording", answers_error=str(exc))

        if self.cfg.open_browser:
            self._open_surface()

        if self.cfg.persist_seconds > 0:
            self._flush_thread = threading.Thread(target=self._flush_loop,
                                                  name="hud-flush", daemon=True)
            self._flush_thread.start()

        try:
            HUD_URLFILE.write_text(self.url, encoding="utf-8")
            os.chmod(str(HUD_URLFILE), 0o600)
        except OSError:
            pass

        self._started = True
        self.log("Live HUD: {} (transcript column{}; answers: {})".format(
            self.url,
            "" if self.cfg.answers_enabled else " only",
            self.cfg.answers_backend if self.cfg.answers_enabled else "off"))
        return self._port

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._stop.set()
        stop_started = time.time()
        if self._flush_thread is not None:
            stage_started = time.time()
            self._flush_thread.join(timeout=2.5)
            self._record_lifecycle_stage("flush_thread_join", stage_started)

        # Stop capture/STT before generating a summary so the final recognized
        # words are present in both the summary and the identity record.
        if self.stt is not None:
            stage_started = time.time()
            try:
                self.stt.stop()
            except Exception:  # noqa: BLE001
                pass
            self._record_lifecycle_stage("stt_stop", stage_started)
        summary = None
        if self.answers is not None:
            stage_started = time.time()
            try:
                summary = self.answers.finish()
            except Exception as exc:  # noqa: BLE001
                self.log("live HUD: summary failed ({})".format(exc))
            self._record_lifecycle_stage("answer_finish", stage_started)
        if self.answers is not None:
            stage_started = time.time()
            try:
                self.answers.stop()
            except Exception:  # noqa: BLE001
                pass
            self._record_lifecycle_stage("answer_stop", stage_started)
        if self._writeback is not None:
            stage_started = time.time()
            try:
                self._writeback.stop()
            except Exception as exc:  # noqa: BLE001
                self.log("live transcript writeback shutdown failed ({})".format(exc))
            self._record_lifecycle_stage("writeback_stop", stage_started)
        identity = self._finalize_identity()
        self._record_lifecycle_stage("identity_finalize", stop_started)
        if self._writeback is not None:
            stage_started = time.time()
            self._writeback.finalize_name(str(identity.get("slug") or ""))
            self._record_lifecycle_stage("writeback_rename", stage_started)
            self._writeback = None
        self.state.set_meta(
            shutdown_reason="requested",
            lifecycle_stages=dict(self._lifecycle_stages),
            lifecycle_total_seconds=round(max(0.0, time.time() - stop_started), 4))
        stage_started = time.time()
        self._persist(summary=summary)
        self._record_lifecycle_stage("persist", stage_started)
        self.state.set_status("stopped")
        try:
            HUD_URLFILE.unlink(missing_ok=True)
        except OSError:
            pass
        self._stop_native_window()
        if self.server is not None:
            stage_started = time.time()
            try:
                self.server.stop()
            except Exception:  # noqa: BLE001
                pass
            self._record_lifecycle_stage("server_stop", stage_started)

    def _finalize_identity(self) -> Dict[str, Any]:
        """Write evidence-backed metadata and add a readable folder slug."""
        from datetime import datetime as _datetime

        identity = build_identity(
            self.started_at, _datetime.now(), self.outdir.name,
            self.state.transcript_text(), self.cfg,
            speaker_mappings=self.state.speaker_mappings())
        original = self.outdir
        target_name = meaningful_folder_name(original.name, identity)
        target = original.parent / target_name
        if target != original:
            suffix = 1
            candidate = target
            while candidate.exists():
                suffix += 1
                candidate = target.with_name("{}-{}".format(target.name, suffix))
            try:
                original.rename(candidate)
                self.outdir = candidate
                identity["folder"] = candidate.name
                identity["path"] = str(candidate)
            except OSError as exc:
                self.log("session folder kept numeric name ({}): {}".format(
                    original, exc))
        try:
            write_identity(self.outdir / "session.json", identity)
        except OSError as exc:
            self.log("session identity could not be written ({}): {}".format(
                self.outdir, exc))
        self.log("session identity: {}".format(identity.get("title") or "Meeting"))
        return identity

    # -- helpers -----------------------------------------------------------
    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        host = self.server.host if self.server is not None else self.cfg.host
        base = "http://{}:{}/".format(url_host(host), self._port)
        return "{}?token={}".format(base, self.token) if self.token else base

    def _on_ask(self, text: str, expand: bool) -> bool:
        if self.answers is None:
            return False
        return self.answers.ask(text, expand)

    def _on_pause(self, paused: bool) -> None:
        if self.answers is not None:
            self.answers.pause(paused)
        self.state.set_meta(answers_paused=paused)

    def _on_speaker_label(self, speaker_id: str, label: str) -> bool:
        """Apply a presentation-only name override from the HUD."""
        return self.state.set_speaker_label(speaker_id, label, source="user")

    def update_devices(self, mic_name: Optional[str], system_name: Optional[str]) -> None:
        """Follow a recorder device switch (headphones, failover, route change)."""
        if (mic_name, system_name) == (self.mic_name, self.system_name):
            return
        self.mic_name = mic_name
        self.system_name = system_name
        if self.stt is not None:
            try:
                self.stt.update_devices(mic_name, system_name)
            except Exception as exc:  # noqa: BLE001
                self.log("live HUD: device update failed ({})".format(exc))
        self._publish_devices()

    def _publish_devices(self) -> None:
        warning = ""
        if not self.system_name:
            warning = ("No system/loopback input selected; recording microphone only, so "
                       "the other party will not be captured.")
        else:
            try:
                from .system_tap import SOURCE_NAME as TAP_SOURCE
            except ImportError:
                TAP_SOURCE = None
            if TAP_SOURCE is not None and self.system_name == TAP_SOURCE:
                # Tap capture does not change any routing; the loopback
                # advice does not apply and would only mislead.
                warning = ""
            else:
                try:
                    from .devices import system_advice, system_priority
                    warning = system_advice() or ""
                    if not warning and system_priority(self.system_name) <= 20:
                        warning = ("'{}' only carries Zoom's own audio, not general system "
                                   "output; the other party may not be captured. Install "
                                   "BlackHole and use a Multi-Output Device.").format(self.system_name)
                except Exception:  # noqa: BLE001
                    warning = ""
        self.state.set_meta(mic_device=self.mic_name or "",
                            system_device=self.system_name or "",
                            system_audio_warning=warning)

    def _open_browser(self) -> None:
        time.sleep(0.4)
        try:
            webbrowser.open(self.url)
        except Exception:  # noqa: BLE001
            pass

    def _open_surface(self) -> None:
        """Open the native Mac HUD, falling back to the browser safely."""
        if self.cfg.native_window and sys.platform == "darwin":
            try:
                from .native_window import capture_protection_label
                host = Path(__file__).with_name("native_window.py")
                self._native_proc = subprocess.Popen(
                    [sys.executable, str(host), "--url", self.url,
                     "--title", "Meeting HUD"],
                    cwd=str(host.parent.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                # AppKit startup is asynchronous. A process that exits during
                # import/window creation is a failed surface, not success.
                time.sleep(0.2)
                if self._native_proc.poll() is None:
                    self._hud_surface = "native"
                    self.state.set_meta(
                        hud_surface="native",
                        hud_capture_protection=capture_protection_label())
                    self.log("Live HUD: native Mac window active")
                    return
                self._native_proc = None
            except Exception as exc:  # noqa: BLE001 - browser remains valid
                self.log("Live HUD: native window unavailable ({}); using browser".format(exc))

        self._hud_surface = "browser"
        self.state.set_meta(hud_surface="browser", hud_capture_protection="none")
        threading.Thread(target=self._open_browser, daemon=True).start()

    def _stop_native_window(self) -> None:
        proc = self._native_proc
        self._native_proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except Exception:  # noqa: BLE001 - shutdown must remain best effort
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(path))

    def _write_outputs(self) -> list:
        derived = self.outdir / "derived"
        derived.mkdir(parents=True, exist_ok=True)
        written = []
        try:
            from .replay import write_jsonl
            write_jsonl(derived / "live_events.jsonl", self.state.since(0))
            written.append("live_events.jsonl")
            self._atomic_write(
                derived / "meeting_memory.json",
                json.dumps(self.state.memory(), indent=2, ensure_ascii=False) + "\n")
            written.append("meeting_memory.json")
            self._atomic_write(
                derived / "live_diagnostics.json",
                json.dumps({"meta": self.state.snapshot().get("meta", {}),
                            "metrics": self.state.metrics()}, indent=2,
                           ensure_ascii=False, sort_keys=True) + "\n")
            written.append("live_diagnostics.json")
        except (OSError, TypeError, ValueError) as exc:
            self.log("Live HUD: could not persist event/memory log ({})".format(exc))
        transcript = self.state.transcript_text()
        if transcript:
            self._atomic_write(derived / "live_transcript.txt", transcript + "\n")
            written.append("live_transcript.txt")
        answers = self.state.answers_markdown()
        if answers:
            self._atomic_write(derived / "live_answers.md", answers)
            written.append("live_answers.md")
        # Interleaved transcript + answers, so each answer sits next to the
        # speech that prompted it.
        title = "Live conversation — {} {}".format(
            self.outdir.parent.name, self.outdir.name)
        conversation = self.state.timeline_markdown(title=title)
        if conversation:
            self._atomic_write(derived / "live_conversation.md", conversation)
            written.append("live_conversation.md")
        return written

    def _flush_loop(self) -> None:
        interval = max(2.0, float(self.cfg.persist_seconds))
        while not self._stop.wait(interval):
            try:
                self._write_outputs()
            except Exception:  # noqa: BLE001
                pass

    def _write_summary(self, summary: Dict[str, Any]) -> None:
        derived = self.outdir / "derived"
        derived.mkdir(parents=True, exist_ok=True)
        lines = ["# Call summary", "", str(summary.get("summary", "")).strip(), ""]
        items = [str(i).strip() for i in (summary.get("action_items") or []) if str(i).strip()]
        if items:
            lines.append("## Action items")
            lines.extend("- {}".format(i) for i in items)
            lines.append("")
        email = summary.get("follow_up_email")
        if email:
            lines.extend(["## Follow-up email", "", "```", str(email).strip(), "```", ""])
        self._atomic_write(derived / "live_summary.md", "\n".join(lines))

    def _persist(self, summary: Optional[Dict[str, Any]] = None) -> None:
        try:
            written = self._write_outputs()
            if summary:
                self._write_summary(summary)
                written.append("live_summary.md")
            if written:
                self.log("Live HUD: wrote derived/{}".format(", ".join(written)))
        except OSError as exc:
            self.log("live HUD: could not persist outputs ({})".format(exc))
