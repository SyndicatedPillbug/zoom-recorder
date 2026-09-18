#!/usr/bin/env python3
"""Wires the HUD subsystems together and owns their lifecycle.

``zoom_record.py`` only ever calls :meth:`LiveSession.start` and
:meth:`LiveSession.stop`. Everything else -- the HTTP server, live tap, STT and
answer engine -- is managed here. All failures are contained: a HUD that cannot
start leaves the recording completely unaffected.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .answers import AnswerEngine
from .budget import BudgetGovernor
from .config import HudConfig
from .server import HudServer
from .state import LiveState
from .stt import LiveTranscriber

HUD_URLFILE = Path.home() / ".zoom_recorder_hud.url"


class LiveSession:
    def __init__(self, cfg: HudConfig, outdir: Path, log: Callable[[str], None],
                 mic_name: Optional[str], system_name: Optional[str] = None,
                 model_path: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.outdir = Path(outdir)
        self.log = log
        self.mic_name = mic_name
        self.system_name = system_name
        self.model_path = model_path

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

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> Optional[int]:
        try:
            self.server = HudServer(self.state, self.cfg.host, self.cfg.port, self.log,
                                    token=self.token, on_ask=self._on_ask,
                                    on_pause=self._on_pause)
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

        try:
            self.stt = LiveTranscriber(self.state, self.log, self.cfg,
                                       self.mic_name, self.system_name, self.model_path)
            self.stt.start()
        except Exception as exc:  # noqa: BLE001
            self.log("live HUD: transcription failed to start ({})".format(exc))
            self.state.set_status("recording", stt_error=str(exc))

        try:
            self.answers = AnswerEngine(self.state, self.log, self.cfg, self.budget)
            self.answers.start()
        except Exception as exc:  # noqa: BLE001
            self.log("live HUD: answer engine failed to start ({})".format(exc))
            self.state.set_status("recording", answers_error=str(exc))

        if self.cfg.open_browser:
            threading.Thread(target=self._open_browser, daemon=True).start()

        if self.cfg.persist_seconds > 0:
            self._flush_thread = threading.Thread(target=self._flush_loop,
                                                  name="hud-flush", daemon=True)
            self._flush_thread.start()

        try:
            HUD_URLFILE.write_text(self.url, encoding="utf-8")
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
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=2.5)
        summary = None
        if self.answers is not None:
            try:
                summary = self.answers.finish()
            except Exception as exc:  # noqa: BLE001
                self.log("live HUD: summary failed ({})".format(exc))
        self._persist(summary=summary)
        for component in (self.answers, self.stt):
            if component is not None:
                try:
                    component.stop()
                except Exception:  # noqa: BLE001
                    pass
        self.state.set_status("stopped")
        try:
            HUD_URLFILE.unlink(missing_ok=True)
        except OSError:
            pass
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:  # noqa: BLE001
                pass

    # -- helpers -----------------------------------------------------------
    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        base = "http://{}:{}/".format(self.cfg.host, self._port)
        return "{}?token={}".format(base, self.token) if self.token else base

    def _on_ask(self, text: str, expand: bool) -> bool:
        if self.answers is None:
            return False
        return self.answers.ask(text, expand)

    def _on_pause(self, paused: bool) -> None:
        if self.answers is not None:
            self.answers.pause(paused)
        self.state.set_meta(answers_paused=paused)

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

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(path))

    def _write_outputs(self) -> list:
        derived = self.outdir / "derived"
        derived.mkdir(parents=True, exist_ok=True)
        written = []
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
