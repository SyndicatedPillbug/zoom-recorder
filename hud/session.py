#!/usr/bin/env python3
"""Wires the HUD subsystems together and owns their lifecycle.

``zoom_record.py`` only ever calls :meth:`LiveSession.start` and
:meth:`LiveSession.stop`. Everything else -- the HTTP server, live tap, STT and
answer engine -- is managed here. All failures are contained: a HUD that cannot
start leaves the recording completely unaffected.
"""

from __future__ import annotations

import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional

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
        self.server: Optional[HudServer] = None
        self.stt: Optional[LiveTranscriber] = None
        self.answers: Optional[AnswerEngine] = None
        self._started = False
        self._port = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> Optional[int]:
        try:
            self.server = HudServer(self.state, self.cfg.host, self.cfg.port, self.log)
            self._port = self.server.start()
        except Exception as exc:  # noqa: BLE001
            self.log("live HUD: HTTP server failed ({}); continuing without HUD".format(exc))
            self.server = None
            return None

        self.state.set_status("recording", answers_backend=self.cfg.answers_backend,
                              answers_enabled=self.cfg.answers_enabled,
                              stt_backend=self.cfg.stt_backend)
        self.state.set_budget(self.budget.snapshot())

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
        for component in (self.answers, self.stt):
            if component is not None:
                try:
                    component.stop()
                except Exception:  # noqa: BLE001
                    pass
        self._persist()
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
        return "http://{}:{}/".format(self.cfg.host, self._port)

    def _open_browser(self) -> None:
        time.sleep(0.4)
        try:
            webbrowser.open(self.url)
        except Exception:  # noqa: BLE001
            pass

    def _persist(self) -> None:
        derived = self.outdir / "derived"
        written = []
        try:
            derived.mkdir(parents=True, exist_ok=True)
            transcript = self.state.transcript_text()
            if transcript:
                (derived / "live_transcript.txt").write_text(
                    transcript + "\n", encoding="utf-8")
                written.append("live_transcript.txt")
            answers = self.state.answers_markdown()
            if answers:
                (derived / "live_answers.md").write_text(answers, encoding="utf-8")
                written.append("live_answers.md")
            # Interleaved transcript + answers, so each answer sits next to the
            # speech that prompted it.
            title = "Live conversation — {} {}".format(
                self.outdir.parent.name, self.outdir.name)
            conversation = self.state.timeline_markdown(title=title)
            if conversation:
                (derived / "live_conversation.md").write_text(conversation, encoding="utf-8")
                written.append("live_conversation.md")
            if written:
                self.log("Live HUD: wrote derived/{}".format(", ".join(written)))
        except OSError as exc:
            self.log("live HUD: could not persist outputs ({})".format(exc))
