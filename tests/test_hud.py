#!/usr/bin/env python3
"""Unit tests for the live HUD subsystems.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hud.answers import (AnswerEngine, detect_question, estimate_tokens,  # noqa: E402
                         parse_bullets)
from hud.budget import BudgetGovernor  # noqa: E402
from hud.config import HudConfig, load_config  # noqa: E402
from hud.kb import chunk_markdown  # noqa: E402
from hud.llm import LLMError, LLMResult  # noqa: E402
from hud.menu_state import describe  # noqa: E402
from hud.server import HudServer  # noqa: E402
from hud.state import LiveState  # noqa: E402
from hud.stt import (Chunker, frame_rms_dbfs, overlap_suffix_prefix,  # noqa: E402
                     pcm_to_wav, split_words)


def tone(seconds: float, freq: float = 440.0, amp: int = 16000) -> bytes:
    rate = 16000
    count = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(count)
    )


class OverlapTests(unittest.TestCase):
    def test_overlap_detection(self) -> None:
        prev = split_words("the deadline is friday next week")
        new = split_words("friday next week we ship")
        self.assertEqual(overlap_suffix_prefix(prev, new), 3)

    def test_no_overlap(self) -> None:
        self.assertEqual(
            overlap_suffix_prefix(split_words("alpha beta"), split_words("gamma delta")), 0)

    def test_full_repeat_is_dropped_by_delta_logic(self) -> None:
        # A chunk that is entirely a repeat of the previous tail yields no new words.
        prev = split_words("hello there everyone")
        new = split_words("hello there everyone")
        self.assertEqual(overlap_suffix_prefix(prev, new), 3)


class ChunkerTests(unittest.TestCase):
    def test_speech_emits_chunk(self) -> None:
        c = Chunker(chunk_seconds=1.0, min_speech_seconds=0.2,
                    silence_flush_seconds=0.4)
        out = c.feed(tone(1.2))
        self.assertTrue(out)
        self.assertGreater(len(out[0]), 16000)

    def test_pure_silence_is_dropped(self) -> None:
        c = Chunker(chunk_seconds=1.0, min_speech_seconds=0.2,
                    silence_flush_seconds=0.4)
        out = c.feed(b"\x00\x00" * 16000 * 3)
        self.assertEqual(out, [])

    def test_frame_levels(self) -> None:
        quiet = frame_rms_dbfs(b"\x00\x00" * 100)
        loud = frame_rms_dbfs(tone(0.1))
        self.assertLess(quiet, -100)
        self.assertGreater(loud, -20)


class TextTests(unittest.TestCase):
    def test_detect_question(self) -> None:
        self.assertEqual(detect_question("Can you explain the rollout plan?"),
                         "Can you explain the rollout plan?")
        self.assertIsNotNone(detect_question("How do we handle rollbacks"))
        self.assertIsNone(detect_question("The report is ready."))

    def test_parse_bullets_json(self) -> None:
        raw = json.dumps({"bullets": ["First point", "Second point"]})
        self.assertEqual(parse_bullets(raw), ["First point", "Second point"])

    def test_parse_bullets_markdown(self) -> None:
        raw = "- one\n- two\n1. three"
        self.assertEqual(parse_bullets(raw), ["one", "two", "three"])

    def test_estimate_tokens_positive(self) -> None:
        self.assertGreater(estimate_tokens("x" * 400, 100), 100)


class KBTests(unittest.TestCase):
    def test_chunk_markdown_headings(self) -> None:
        md = "# Alpha\n\nFirst paragraph about alpha.\n\n## Beta\n\nSecond paragraph about beta."
        chunks = chunk_markdown(md, "notes.md", target_chars=40, overlap_chars=0)
        self.assertTrue(chunks)
        headings = " ".join(c["heading"] for c in chunks)
        self.assertIn("Alpha", headings)
        self.assertTrue(all(c["source"] == "notes.md" for c in chunks))

    def test_chunk_markdown_empty(self) -> None:
        self.assertEqual(chunk_markdown("", "x.md"), [])


class BudgetTests(unittest.TestCase):
    def test_afford_and_pause(self) -> None:
        b = BudgetGovernor(tpm=1000, tpd=10000)
        self.assertTrue(b.can_afford(100))
        b.pause(retry_after=5)
        self.assertFalse(b.can_afford(100))
        self.assertGreater(b.blocked_seconds(), 0)

    def test_header_accounting(self) -> None:
        b = BudgetGovernor()
        b.record({"x-ratelimit-remaining-tokens": "5000",
                  "x-ratelimit-remaining-requests": "900"},
                 {"total_tokens": 123})
        snap = b.snapshot()
        self.assertEqual(snap["header_tpm_remaining"], 5000)
        self.assertEqual(snap["header_rpd_remaining"], 900)
        self.assertEqual(snap["total_tokens"], 123)

    def test_unlimited_default_trusts_headers(self) -> None:
        b = BudgetGovernor()  # tpm=tpd=0 -> no local limit
        self.assertTrue(b.can_afford(10_000))
        self.assertFalse(b.daily_budget_low())
        # Provider-reported remaining tokens still gate spending.
        b.record({"x-ratelimit-remaining-tokens": "50"}, {"total_tokens": 1})
        self.assertFalse(b.can_afford(100))


class ConfigTests(unittest.TestCase):
    def test_file_load_and_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "answers": {"backend": "openrouter", "interval": 20},
                "kb": {"dirs": [tmp]},
                "api_keys": {"openrouter": "secret"},
            }))
            cfg = load_config(path)
            self.assertEqual(cfg.answers_backend, "openrouter")
            self.assertEqual(cfg.answer_interval, 20.0)
            self.assertEqual(cfg.kb_dirs, [tmp])
            self.assertEqual(cfg.api_key_for("openrouter"), "secret")

    def test_env_overrides_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}")
            with mock.patch.dict(os.environ, {"ZOOM_HUD_STT_BACKEND": "local"}, clear=False):
                cfg = load_config(path)
            self.assertEqual(cfg.stt_backend, "local")

    def test_malformed_config_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json")
            cfg = load_config(path)
            self.assertEqual(cfg.answers_backend, "groq")

    def test_provider_model_resolution(self) -> None:
        cfg = HudConfig(answers_backend="groq")
        self.assertTrue(cfg.resolve_chat_model())
        self.assertTrue(cfg.resolve_rolling_model())


class StateTests(unittest.TestCase):
    def test_events_and_snapshot(self) -> None:
        state = LiveState()
        state.add("transcript", text="hello")
        state.add("transcript", text="world")
        state.add("answer", kind="rolling", bullets=["a"])
        snap = state.snapshot()
        self.assertEqual(len(snap["transcript"]), 2)
        self.assertEqual(len(snap["answers"]), 1)
        self.assertEqual(state.transcript_text(), "hello world")
        self.assertIn("Talking points", state.answers_markdown())

    def test_since_and_latest(self) -> None:
        state = LiveState()
        first = state.add("transcript", text="a")
        state.add("transcript", text="b")
        self.assertEqual(state.latest_id(), 2)
        events = state.since(first["id"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], "b")

    def test_timeline_interleaves_transcript_and_answers(self) -> None:
        state = LiveState()
        state.add("transcript", text="hello there")
        state.add("answer", kind="rolling", bullets=["point a"])
        state.add("transcript", text="what is the plan?")
        state.add("answer", kind="question", question="what is the plan?",
                  bullets=["phase 1"], sources=["notes.md"])
        md = state.timeline_markdown(title="Live conversation")
        self.assertIn("Live conversation", md)
        # Chronological: speech, its answer, next speech, its answer.
        self.assertLess(md.index("hello there"), md.index("point a"))
        self.assertLess(md.index("point a"), md.index("what is the plan?"))
        self.assertIn("Talking points", md)
        self.assertIn("notes.md", md)

    def test_timeline_empty(self) -> None:
        self.assertEqual(LiveState().timeline_markdown(), "")


class ServerTests(unittest.TestCase):
    def test_health_and_state(self) -> None:
        import urllib.request

        state = LiveState()
        server = HudServer(state, port=0)
        port = server.start()
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/health".format(port), timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue(json.loads(resp.read())["ok"])
            state.add("transcript", text="live text")
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/state".format(port), timeout=5) as resp:
                snap = json.loads(resp.read())
                self.assertEqual(snap["transcript"][0]["text"], "live text")
        finally:
            server.stop()


class MenuStateTests(unittest.TestCase):
    def test_idle(self) -> None:
        state = describe(recording=False, hud_active=False)
        self.assertEqual(state["toggle_title"], "Start Recording")
        self.assertEqual(state["live_title"], "Start with Live HUD")
        self.assertTrue(state["live_enabled"])
        self.assertFalse(state["open_enabled"])

    def test_recording_without_hud(self) -> None:
        state = describe(recording=True, hud_active=False)
        self.assertEqual(state["toggle_title"], "Stop Recording")
        # No duplicate "Stop Recording": the HUD item is a disabled starter.
        self.assertFalse(state["live_enabled"])
        self.assertNotEqual(state["live_title"], "Stop Recording")
        self.assertFalse(state["open_enabled"])

    def test_recording_with_hud(self) -> None:
        state = describe(recording=True, hud_active=True)
        self.assertEqual(state["live_title"], "Live HUD active ✓")
        self.assertFalse(state["live_enabled"])
        self.assertTrue(state["open_enabled"])

    def test_icons_differ(self) -> None:
        self.assertNotEqual(describe(False, False)["icon"], describe(True, False)["icon"])
        self.assertNotEqual(describe(True, False)["icon"], describe(True, True)["icon"])


class AnswerEngineTests(unittest.TestCase):
    class _FakeClient:
        def __init__(self) -> None:
            self.response_formats = []

        def chat(self, messages, model, max_tokens=600, temperature=0.2,
                 response_format=None, timeout=None):
            self.response_formats.append(response_format)
            if response_format is not None:
                # Simulate Groq rejecting truncated JSON from a reasoning model.
                raise LLMError("invalid json", status=400, body="Failed to validate JSON")
            return LLMResult(text="- first point\n- second point", model=model,
                             usage={"total_tokens": 12})

    class _Chain:
        def __init__(self, entries):
            self.entries = entries

    def test_json_mode_400_falls_back_to_plain_text(self) -> None:
        from hud.state import LiveState

        cfg = HudConfig(answers_backend="groq", rolling_enabled=False, kb_enabled=False)
        state = LiveState()
        engine = AnswerEngine(state, lambda _m: None, cfg)
        client = self._FakeClient()
        engine._chain = self._Chain([{
            "name": "groq", "client": client, "chat_model": "m", "rolling_model": "m",
            "structured": True,
        }])
        engine._buffer = [(0.0, "What is the rollout plan?")]
        ok = engine._answer(kind="question", question="What is the rollout plan?")

        self.assertTrue(ok)
        # Tried structured first, then retried plain.
        self.assertEqual(client.response_formats, [{"type": "json_object"}, None])
        answers = state.snapshot()["answers"]
        self.assertEqual(answers[0]["bullets"], ["first point", "second point"])


class WavTests(unittest.TestCase):
    def test_pcm_to_wav_roundtrip(self) -> None:
        import io
        import wave

        data = pcm_to_wav(tone(0.1))
        with wave.open(io.BytesIO(data), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.getsampwidth(), 2)


if __name__ == "__main__":
    unittest.main()
