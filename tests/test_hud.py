#!/usr/bin/env python3
"""Unit tests for the live HUD subsystems.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import math
import os
import ctypes
import queue
import struct
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hud.answers import (AnswerEngine, Turn, detect_question_text,  # noqa: E402
                         detect_question_turns, detect_questions_since,
                         estimate_tokens, grounded_in, parse_answer_claims,
                         verify_answer_claims, is_ambiguous_question, parse_bullets,
                         parse_point_objects)
from hud.budget import BudgetGovernor  # noqa: E402
from hud.config import (HudConfig, config_from_dict, config_to_dict,  # noqa: E402
                        load_config, save_config)
from hud.diarization import (diarization_readiness,  # noqa: E402
                             run_post_call_diarization)
from hud.evaluation import normalize_words, word_error_stats  # noqa: E402
from hud.identity import (build_identity, derive_title, meaningful_folder_name,
                          slugify)  # noqa: E402
from hud.kb import KBIndex, _lexical_score, chunk_markdown  # noqa: E402
from hud.local_http import host_from_header, url_host  # noqa: E402
from hud.llm import LLMError, LLMResult  # noqa: E402
from hud.memory import extract_memory, format_memory  # noqa: E402
from hud.menu_state import describe  # noqa: E402
from hud.replay import benchmark, replay  # noqa: E402
from hud.server import HudServer  # noqa: E402
from hud.state import LiveState, is_duplicate_point  # noqa: E402
from hud.stt import (Chunker, LiveTranscriber, LocalWhisperSTT, _Source,  # noqa: E402
                     frame_rms_dbfs, StablePartialDecoder, fuzzy_overlap,
                     looks_hallucinated,
                     overlap_suffix_prefix,
                     pcm_to_wav, split_words)
from hud.transcript_writeback import TranscriptWriteback  # noqa: E402
from hud.voice_profiles import VoiceProfileStore  # noqa: E402
from hud.vad import EnergyVAD, NoiseFloor, build_vad, frame_level_dbfs  # noqa: E402


def tone(seconds: float, freq: float = 440.0, amp: int = 16000) -> bytes:
    rate = 16000
    count = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(count)
    )


class OverlapTests(unittest.TestCase):
    def test_partial_decoder_commits_only_stable_prefix(self) -> None:
        decoder = StablePartialDecoder()
        self.assertEqual(decoder.accept("what is the"), "")
        self.assertEqual(decoder.provisional_text, "what is the")
        self.assertEqual(decoder.accept("what is the plan"), "what is the")
        self.assertEqual(decoder.provisional_text, "plan")
        self.assertEqual(decoder.accept("what is the plan for tomorrow"), "plan")
        self.assertEqual(decoder.provisional_text, "for tomorrow")

    def test_partial_decoder_rebases_after_window_moves(self) -> None:
        decoder = StablePartialDecoder()
        decoder.accept("we should ship the beta")
        self.assertEqual(decoder.accept("we should ship the beta next week"),
                         "we should ship the beta")
        # The rolling window no longer contains the original prefix. It must
        # not crash or emit an unbounded duplicate prefix.
        decoder.accept("the beta next week after launch")
        self.assertLessEqual(len(decoder.provisional_text.split()), 5)

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

    def test_fuzzy_overlap_exact(self) -> None:
        self.assertEqual(
            fuzzy_overlap(split_words("the deadline is friday next week"),
                          split_words("friday next week we ship")), 3)

    def test_fuzzy_overlap_tolerates_asr_variants(self) -> None:
        overlap = fuzzy_overlap(
            split_words("we will roll out the feature"),
            split_words("rollout the feature next week"))
        self.assertGreaterEqual(overlap, 3)

    def test_fuzzy_overlap_no_match(self) -> None:
        self.assertEqual(
            fuzzy_overlap(split_words("alpha beta"), split_words("gamma delta")), 0)


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

    def test_phrase_pause_flushes_before_full_chunk(self) -> None:
        c = Chunker(chunk_seconds=10.0, min_speech_seconds=0.2,
                    silence_flush_seconds=3.0, phrase_silence_seconds=0.2,
                    phrase_min_speech_seconds=1.0)
        out = c.feed(tone(1.5) + b"\x00\x00" * 16000)  # 1.5s speech + 1s silence
        self.assertTrue(out)
        self.assertLess(len(out[0]), int(10.0 * 16000 * 2))

    def test_adaptive_target_retunes_and_preserves_overlap(self) -> None:
        c = Chunker(2.0, min_speech_seconds=0.2, min_chunk_seconds=1.0,
                    max_chunk_seconds=3.0, overlap_seconds=0.5)
        first = c.feed(tone(2.1, freq=440.0) + tone(2.0, freq=880.0))
        self.assertGreaterEqual(len(first), 2)
        overlap_bytes = int(0.5 * 16000 * 2)
        self.assertEqual(first[0][-overlap_bytes:], first[1][:overlap_bytes])
        self.assertEqual(c.set_target_seconds(0.2), 1.0)
        self.assertEqual(c.set_target_seconds(4.0), 3.0)


class SttPipelineTests(unittest.TestCase):
    def _transcriber(self, cfg):
        return LiveTranscriber(LiveState(), lambda _m: None, cfg, "Mic", None)

    def test_local_cli_retries_without_metal_after_process_failure(self) -> None:
        logs = []

        def fake_run(command, **_kwargs):
            if "-ng" in command:
                out_base = Path(command[command.index("-of") + 1])
                out_base.with_suffix(".txt").write_text("recovered text", encoding="utf-8")
                return mock.Mock(returncode=0, stderr="")
            return mock.Mock(returncode=-11, stderr="Metal buffer allocation failed")

        stt = LocalWhisperSTT(Path("/tmp/model.bin"), logs.append, "not-installed")
        stt._cli = "whisper-cli"
        with mock.patch("hud.stt.subprocess.run", side_effect=fake_run):
            result = stt.transcribe(b"\x00\x00" * 1600)
        self.assertEqual(result.text, "recovered text")
        self.assertTrue(any("GPU failed" in item for item in logs))

    def test_local_server_warmup_is_best_effort(self) -> None:
        logs = []
        stt = LocalWhisperSTT.__new__(LocalWhisperSTT)
        stt.log = logs.append
        stt._server_transcribe = mock.Mock(
            side_effect=RuntimeError("test warmup failure"))
        stt._warm_server()
        stt._server_transcribe.assert_called_once()
        self.assertTrue(any("warmup skipped" in item for item in logs))

    def test_interim_server_failure_never_falls_back_to_slow_cli(self) -> None:
        stt = LocalWhisperSTT.__new__(LocalWhisperSTT)
        stt.model = Path("/tmp/base.bin")
        stt.log = lambda _message: None
        stt.allow_cli_fallback = False
        stt._server = object()
        stt._port = 1234
        stt._server_transcribe = mock.Mock(side_effect=ConnectionError("offline"))
        stt._cli_transcribe = mock.Mock(return_value="must not run")
        result = stt.transcribe(b"\x00\x00" * 100)
        self.assertEqual(result.text, "")
        stt._cli_transcribe.assert_not_called()

    def test_partial_lane_uses_available_smaller_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "ggml-base.en.bin"
            model.write_bytes(b"test model")
            cfg = HudConfig(stt_backend="local", stt_partial_model=str(model))
            tr = self._transcriber(cfg)
            with mock.patch("hud.stt.LocalWhisperSTT") as factory:
                expected = object()
                factory.return_value = expected
                self.assertIs(tr._build_partial_stt(), expected)
                factory.assert_called_once_with(
                    model, mock.ANY, "whisper-server", allow_cli_fallback=False)

    def test_partial_lane_can_run_while_final_lane_is_busy(self) -> None:
        cfg = HudConfig(stt_backend="local", stt_partial_window_seconds=0.2,
                        stt_partial_interval_seconds=0.0)
        tr = self._transcriber(cfg)
        tr._partial_stt = mock.Mock()
        tr._partial_stt.transcribe.return_value = "what is the plan"
        src = _Source("You", [])
        tr._sources = [src]
        tr._inference_lock.acquire()
        try:
            for _ in range(4):
                tr._feed_partial(src, b"\x00\x00" * 1600)
            self.assertFalse(src.partial_queue.empty())
        finally:
            tr._inference_lock.release()

    def test_enqueue_drops_oldest_when_full(self) -> None:
        tr = self._transcriber(HudConfig(stt_queue_chunks=1))
        src = _Source("You", [])
        src.queue = queue.Queue(maxsize=1)
        tr._sources = [src]
        tr._enqueue(src, b"first")
        tr._enqueue(src, b"second")
        self.assertEqual(src.dropped, 1)
        _ts, chunk, _margin = src.queue.get_nowait()
        self.assertEqual(chunk, b"second")

    def test_prompt_uses_glossary_and_context(self) -> None:
        tr = self._transcriber(HudConfig(stt_glossary=["Acme", "Q3"]))
        src = _Source("You", [])
        src.context_tail = "we shipped the beta."
        prompt = tr._prompt_for(src)
        self.assertIsNotNone(prompt)
        assert prompt is not None
        self.assertIn("Acme", prompt)
        self.assertIn("Q3", prompt)
        self.assertIn("beta", prompt)

    def test_prompt_trims_to_sentence_boundary(self) -> None:
        tr = self._transcriber(HudConfig())
        src = _Source("You", [])
        src.context_tail = "hello world. and then some"
        self.assertEqual(tr._prompt_for(src), "hello world.")
        src.context_tail = "no terminator in here"
        self.assertIsNone(tr._prompt_for(src))

    def test_worker_transcribes_with_prompt(self) -> None:
        tr = self._transcriber(HudConfig(stt_glossary=["Acme"]))
        seen = {}

        class FakeSTT:
            def transcribe(self, pcm, prompt=None):
                seen["prompt"] = prompt
                return "hello Acme world"

        tr._stt = FakeSTT()
        src = _Source("You", [])
        src.queue = queue.Queue()
        tr._sources = [src]
        worker = threading.Thread(target=tr._stt_worker, args=(src,), daemon=True)
        worker.start()
        tr._enqueue(src, b"\x00\x00" * 100)
        src.queue.put_nowait(None)
        worker.join(timeout=3)
        events = tr.state.snapshot()["transcript"]
        self.assertEqual(events[0]["text"], "hello Acme world")
        self.assertIn("Acme", seen["prompt"])

    def test_local_partial_worker_keeps_draft_out_of_authoritative_text(self) -> None:
        cfg = HudConfig(stt_backend="local", stt_hallucination_filter=False)
        tr = self._transcriber(cfg)
        outputs = iter(["what is the", "what is the plan"])

        class FakeSTT:
            def transcribe(self, pcm, prompt=None):
                return next(outputs)

        tr._stt = FakeSTT()
        src = _Source("Client", [])
        src.partial_queue = queue.Queue(maxsize=1)
        tr._sources = [src]
        worker = threading.Thread(target=tr._partial_worker, args=(src,), daemon=True)
        worker.start()
        src.partial_queue.put((time.time(), b"audio"))
        src.partial_queue.put((time.time(), b"audio"))
        deadline = time.time() + 2
        while time.time() < deadline and not tr.state.snapshot()["transcript"]:
            time.sleep(0.01)
        src.partial_queue.put(None)
        worker.join(timeout=2)
        snap = tr.state.snapshot()
        self.assertEqual(snap["transcript"][0]["text"], "what is the")
        self.assertEqual(snap["partials"][0]["text"], "plan")
        self.assertNotIn("plan", tr.state.transcript_text())

    def test_partial_admission_yields_to_queued_final_audio(self) -> None:
        cfg = HudConfig(stt_backend="local", stt_partial_window_seconds=0.2,
                        stt_partial_interval_seconds=0.0)
        tr = self._transcriber(cfg)
        src = _Source("You", [])
        src.queue.put((0.0, b"final", 0.0))
        tr._sources = [src]
        tr._feed_partial(src, b"\x00\x00" * 3200)
        self.assertTrue(tr._partial_suppressed)
        self.assertTrue(src.partial_queue.empty())

    def test_partial_recognition_is_local_only(self) -> None:
        self.assertFalse(self._transcriber(HudConfig(stt_backend="groq"))._partial_enabled())
        self.assertTrue(self._transcriber(HudConfig(stt_backend="local"))._partial_enabled())

    def test_adaptive_local_minimum_defaults_to_two_point_five_seconds(self) -> None:
        cfg = HudConfig(stt_backend="local")
        tr = self._transcriber(cfg)
        self.assertEqual(cfg.stt_chunk_min_seconds, 2.5)
        self.assertEqual(tr._chunk_bounds(), (2.5, 7.0))

    def test_final_chunk_deduplicates_stable_partial_words(self) -> None:
        cfg = HudConfig(stt_backend="local", stt_hallucination_filter=False)
        tr = self._transcriber(cfg)
        tr._stt = type("FakeSTT", (), {
            "transcribe": lambda _self, _pcm, prompt=None: "what is the"
        })()
        src = _Source("Client", [])
        tr._publish_committed(src, "what is the", finalized=False)
        tr._transcribe_chunk(b"audio", src)
        transcript = tr.state.snapshot()["transcript"]
        self.assertEqual([e["text"] for e in transcript], ["what is the"])
        self.assertTrue(any(e["type"] == "transcript_boundary"
                            for e in tr.state.since(0)))

    def test_transcript_events_carry_revision_identity(self) -> None:
        tr = self._transcriber(HudConfig(stt_backend="local",
                                         stt_hallucination_filter=False))
        src = _Source("Client", [])
        tr._publish_committed(src, "the rollout plan", finalized=False)
        event = tr.state.snapshot()["transcript"][0]
        self.assertEqual(event["segment_id"], "Client:1")
        self.assertEqual(event["revision"], 0)
        self.assertFalse(event["finalized"])

    def test_transcript_events_explain_channel_attribution(self) -> None:
        tr = self._transcriber(HudConfig(stt_backend="local",
                                         stt_hallucination_filter=False))
        src = _Source("Client", [], "remote")
        tr._publish_committed(src, "the rollout plan")
        event = tr.state.snapshot()["transcript"][0]
        self.assertEqual(event["speaker_id"], "remote")
        self.assertEqual(event["speaker_source"], "channel")
        self.assertEqual(event["speaker_confidence"], 1.0)

    def test_runtime_sources_use_stable_channel_ids(self) -> None:
        cfg = HudConfig(speakers_enabled=True, self_name="Dana", remote_name="Client")
        sources = LiveTranscriber(LiveState(), lambda _m: None,
                                  cfg, "Mic", "System")._build_sources()
        self.assertEqual([s.speaker_id for s in sources], ["local", "remote"])

    def test_groq_two_source_chunk_floor_protects_request_rate(self) -> None:
        cfg = HudConfig(stt_backend="groq", stt_chunk_seconds=5.0)
        tr = LiveTranscriber(LiveState(), lambda _m: None, cfg, "Mic", "System")
        tr._sources = [_Source("You", []), _Source("Others", [])]
        self.assertEqual(tr._effective_chunk_seconds(), 7.0)
        cfg.stt_backend = "local"
        self.assertEqual(tr._effective_chunk_seconds(), 5.0)

    def test_local_adaptive_chunking_moves_with_inference_pressure(self) -> None:
        cfg = HudConfig(stt_backend="local", stt_chunk_seconds=5.0,
                        stt_chunk_min_seconds=3.0, stt_chunk_max_seconds=7.0)
        tr = LiveTranscriber(LiveState(), lambda _m: None, cfg, "Mic", None)
        src = _Source("You", [])
        src.queue = queue.Queue()
        src.chunker = Chunker(5.0, min_chunk_seconds=3.0,
                              max_chunk_seconds=7.0, overlap_seconds=0.5)
        tr._retune_chunker(src, inference_seconds=5.0)
        self.assertEqual(src.chunker.target_seconds, 5.0)
        tr._retune_chunker(src, inference_seconds=1.0)
        self.assertEqual(src.chunker.target_seconds, 4.5)
        src.queue.put((0.0, b"one", 0.0))
        src.queue.put((0.0, b"two", 0.0))
        tr._retune_chunker(src, inference_seconds=5.0)
        self.assertEqual(src.chunker.target_seconds, 5.0)


class VADTests(unittest.TestCase):
    def test_frame_level_dbfs(self) -> None:
        self.assertLess(frame_level_dbfs(b"\x00\x00" * 100), -100)
        self.assertGreater(frame_level_dbfs(tone(0.1)), -20)

    def test_noise_floor_absorbs_steady_noise(self) -> None:
        nf = NoiseFloor(initial_db=-45.0)
        for _ in range(50):
            nf.update(-44.0, margin_db=8.0)
        self.assertGreater(nf.noise_db, -45.0)
        self.assertLessEqual(nf.noise_db, -25.0)

    def test_energy_vad_rejects_hum_accepts_speech(self) -> None:
        vad = EnergyVAD(absolute_db=-50.0, margin_db=8.0, calibration_frames=5)
        hum = tone(0.1, freq=120.0, amp=1200)      # steady, well above -50 dBFS
        speech = tone(0.1, freq=300.0, amp=12000)  # clearly louder
        for _ in range(5):
            vad.is_speech(hum)
        self.assertFalse(vad.is_speech(hum))
        self.assertTrue(vad.is_speech(speech))

    def test_build_vad_forced_energy(self) -> None:
        vad = build_vad(HudConfig(stt_vad_backend="energy"), lambda _m: None)
        self.assertEqual(vad.label, "energy")

    def test_build_vad_auto_always_returns_a_vad(self) -> None:
        vad = build_vad(HudConfig(stt_vad_backend="auto"), lambda _m: None)
        self.assertIn(vad.label, ("energy", "webrtcvad"))

    def test_chunker_with_vad_ignores_steady_hum(self) -> None:
        vad = EnergyVAD(absolute_db=-50.0, margin_db=8.0, calibration_frames=5)
        chunker = Chunker(chunk_seconds=1.0, min_speech_seconds=0.2,
                          silence_flush_seconds=0.4, vad=vad)
        hum = tone(1.0, freq=120.0, amp=1200)
        self.assertEqual(chunker.feed(hum * 2), [])  # 2s of steady hum
        self.assertTrue(chunker.feed(tone(1.2, freq=300.0, amp=12000)))

    def test_energy_vad_non_adaptive_matches_old_gate(self) -> None:
        vad = EnergyVAD(absolute_db=-50.0, adaptive=False)
        self.assertFalse(vad.is_speech(tone(0.1, freq=300.0, amp=50)))
        self.assertTrue(vad.is_speech(tone(0.1, freq=300.0, amp=2000)))

    def test_energy_vad_captures_quiet_speech(self) -> None:
        vad = EnergyVAD(absolute_db=-50.0, margin_db=6.0, calibration_frames=3)
        quiet = b"\x00\x00" * 800
        for _ in range(3):
            vad.is_speech(quiet)
        self.assertFalse(vad.is_speech(quiet))
        self.assertTrue(vad.is_speech(tone(0.1, freq=300.0, amp=2000)))


class HallucinationTests(unittest.TestCase):
    def test_repetitive_loop_dropped(self) -> None:
        text = ("we can see that we can see that there are some people who "
                "we can see that there are some people who have a lot of people")
        self.assertTrue(looks_hallucinated(text))

    def test_known_silence_phrases_dropped(self) -> None:
        self.assertTrue(looks_hallucinated("Thank you."))
        self.assertTrue(looks_hallucinated("Thanks for watching!"))
        self.assertTrue(looks_hallucinated("Subtitles by M. Smith"))
        self.assertTrue(looks_hallucinated("[BLANK_AUDIO]"))

    def test_normal_sentence_kept(self) -> None:
        self.assertFalse(looks_hallucinated(
            "The quoted lines are from Shakespeare's play Hamlet."))

    def test_bare_interjection_only_when_marginal(self) -> None:
        self.assertTrue(looks_hallucinated("you", marginal=True))
        self.assertFalse(looks_hallucinated("you", marginal=False))

    def test_low_confidence_segments_dropped(self) -> None:
        self.assertTrue(looks_hallucinated(
            "Some plausible words here now", no_speech_prob=0.9))
        self.assertTrue(looks_hallucinated(
            "Some plausible words here now", compression_ratio=3.0))

    def test_verbose_segments_filtered(self) -> None:
        from hud.llm import LLMResult
        from hud.stt import RemoteSTT

        stt = RemoteSTT(client=None, model="m", log=lambda _m: None)
        result = LLMResult(text="x", data={"segments": [
            {"text": "This is real speech.", "no_speech_prob": 0.1, "avg_logprob": -0.2},
            {"text": "we can see that we can see", "no_speech_prob": 0.9,
             "compression_ratio": 3.0},
        ]})
        out = stt._from_verbose(result)
        self.assertIn("This is real speech", out.text)
        self.assertNotIn("we can see", out.text)
        self.assertGreater(out.no_speech_prob or 0, 0)


class TextTests(unittest.TestCase):
    def test_detect_question_text(self) -> None:
        self.assertEqual(detect_question_text("Can you explain the rollout plan?"),
                         "Can you explain the rollout plan?")
        self.assertIsNotNone(detect_question_text("How do we handle rollbacks"))
        self.assertIsNone(detect_question_text("The report is ready."))

    def test_detect_question_turns_spans_chunks(self) -> None:
        now = time.time()
        turns = [
            Turn(1, now - 30, "Client", "Let me walk you through the rollout."),
            Turn(2, now - 20, "Client", "We stage it in three rings."),
            Turn(3, now - 10, "Client", "What about rollbacks?"),
        ]
        got = detect_question_turns(turns, 90.0, now)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["question"], "What about rollbacks?")
        self.assertEqual(got["speaker"], "Client")
        self.assertIn("three rings", got["context"])

    def test_detect_question_turns_lookback(self) -> None:
        now = time.time()
        turns = [Turn(1, now - 600, "Client", "What is the plan?")]
        self.assertIsNone(detect_question_turns(turns, 90.0, now))

    def test_detect_indirect_question_without_punctuation(self) -> None:
        self.assertEqual(
            detect_question_text(
                "I was wondering whether your team supports staged rollouts"),
            "I was wondering whether your team supports staged rollouts")

    def test_detect_question_turns_spans_stt_chunks(self) -> None:
        now = time.time()
        turns = [
            Turn(1, now - 2, "Client", "I was wondering"),
            Turn(2, now - 1, "Client", "whether your team supports staged rollouts"),
        ]
        got = detect_question_turns(turns, 90.0, now)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertIn("whether your team supports staged rollouts", got["question"])

    def test_split_question_is_not_reemitted_on_next_turn(self) -> None:
        now = time.time()
        turns = [
            Turn(1, now - 3, "Client", "I was wondering"),
            Turn(2, now - 2, "Client", "whether your team supports staged rollouts"),
            Turn(3, now - 1, "Client", "across the three regions"),
        ]
        found = detect_questions_since(turns, 0, 90.0, now)
        self.assertEqual(len(found), 1)

    def test_is_ambiguous_question(self) -> None:
        self.assertTrue(is_ambiguous_question("What about that?"))
        self.assertTrue(is_ambiguous_question("Why?"))
        self.assertFalse(is_ambiguous_question(
            "What are the three rollout rings and their timelines?"))
        self.assertFalse(is_ambiguous_question(
            "Could you explain the three rollout rings and their timelines?"))

    def test_parse_bullets_json(self) -> None:
        raw = json.dumps({"bullets": ["First point", "Second point"]})
        self.assertEqual(parse_bullets(raw), ["First point", "Second point"])

    def test_parse_bullets_markdown(self) -> None:
        raw = "- one\n- two\n1. three"
        self.assertEqual(parse_bullets(raw), ["one", "two", "three"])

    def test_estimate_tokens_positive(self) -> None:
        self.assertGreater(estimate_tokens("x" * 400, 100), 100)


class KBTests(unittest.TestCase):
    class KeywordEmbedder:
        """Deterministic bag-of-keywords vectors, no third-party deps."""

        label = "test:keywords"
        KEYWORDS = ["alpha", "beta", "gamma", "rollout", "pricing"]

        def __init__(self):
            self.calls = []

        def encode(self, texts):
            self.calls.append(list(texts))
            out = []
            for text in texts:
                low = text.lower()
                out.append([float(low.count(k)) for k in self.KEYWORDS])
            return out

    def test_chunk_markdown_headings(self) -> None:
        md = "# Alpha\n\nFirst paragraph about alpha.\n\n## Beta\n\nSecond paragraph about beta."
        chunks = chunk_markdown(md, "notes.md", target_chars=40, overlap_chars=0)
        self.assertTrue(chunks)
        headings = " ".join(c["heading"] for c in chunks)
        self.assertIn("Alpha", headings)
        self.assertTrue(all(c["source"] == "notes.md" for c in chunks))

    def test_chunk_markdown_empty(self) -> None:
        self.assertEqual(chunk_markdown("", "x.md"), [])

    def test_index_builds_and_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "notes"
            root.mkdir()
            (root / "a.md").write_text("# Alpha\n\nalpha alpha alpha content\n")
            (root / "b.md").write_text("# Beta\n\nbeta beta beta content\n")
            cache = Path(tmp) / "cache"
            index = KBIndex([str(root)], self.KeywordEmbedder(),
                            cache_dir=str(cache), log=lambda _m: None)
            self.assertTrue(index.build())
            hits = index.query("beta", top_k=1)
            self.assertTrue(hits)
            self.assertEqual(hits[0].source, "b.md")

    def test_index_uses_cache_on_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "notes"
            root.mkdir()
            (root / "a.md").write_text("# Alpha\n\nalpha content\n")
            cache = Path(tmp) / "cache"
            KBIndex([str(root)], self.KeywordEmbedder(),
                    cache_dir=str(cache), log=lambda _m: None).build()
            again = KBIndex([str(root)], self.KeywordEmbedder(),
                            cache_dir=str(cache), log=lambda _m: None)
            self.assertTrue(again.build())
            self.assertTrue(again.query("alpha"))

    def test_index_reembeds_only_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "notes"
            root.mkdir()
            (root / "a.md").write_text("# Alpha\n\nalpha content\n")
            (root / "b.md").write_text("# Beta\n\nbeta content\n")
            cache = Path(tmp) / "cache"
            first_embedder = self.KeywordEmbedder()
            self.assertTrue(KBIndex([str(root)], first_embedder,
                                    cache_dir=str(cache), log=lambda _m: None).build())

            (root / "b.md").write_text("# Beta\n\nbeta pricing content\n")
            second_embedder = self.KeywordEmbedder()
            rebuilt = KBIndex([str(root)], second_embedder,
                              cache_dir=str(cache), log=lambda _m: None)
            self.assertTrue(rebuilt.build())
            self.assertEqual(len(second_embedder.calls), 1)
            self.assertEqual(len(second_embedder.calls[0]), 1)
            self.assertIn("beta pricing", second_embedder.calls[0][0].lower())

    def test_index_skips_obsidian_metadata_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            (root / ".obsidian").mkdir(parents=True)
            (root / "notes").mkdir()
            (root / ".obsidian" / "workspace.md").write_text("secret metadata")
            (root / "notes" / "meeting.md").write_text("# Meeting\n\nalpha notes")
            index = KBIndex([str(root)], self.KeywordEmbedder(),
                            cache_dir=str(Path(tmp) / "cache"),
                            log=lambda _m: None, min_score=0.0)
            self.assertTrue(index.build())
            self.assertTrue(all(".obsidian" not in c["source"] for c in index._chunks))

    def test_lexical_score_rewards_exact_reference_terms(self) -> None:
        chunk = {"heading": "Pricing model", "text": "The annual seat price is fixed."}
        self.assertGreaterEqual(_lexical_score("pricing seat", chunk), 1.0)

    def test_is_duplicate_point(self) -> None:
        existing = ["Price is $10 per seat."]
        self.assertTrue(is_duplicate_point("price is 10 per seat", existing))
        self.assertFalse(is_duplicate_point("Latency budget is 200ms", existing))

    def test_hashing_embedder_similarity(self) -> None:
        from hud.kb import HashingEmbedder
        emb = HashingEmbedder(dim=128)
        vecs = emb.encode(["budget meeting quarterly", "budget meeting review",
                          "weather forecast rain"])
        from hud.kb import _dot, _norm

        def cos(a, b):
            na, nb = _norm(a), _norm(b)
            return _dot(a, b) / (na * nb) if na and nb else 0.0

        self.assertGreater(cos(vecs[0], vecs[1]), cos(vecs[0], vecs[2]))

    def test_add_chunks_and_query(self) -> None:
        index = KBIndex([], self.KeywordEmbedder(),
                        log=lambda _m: None, min_score=0.0)
        index.init_empty()
        self.assertEqual(index.query("anything", top_k=5), [])
        added = index.add_chunks([
            {"source": "live_transcript", "heading": "You",
             "text": "alpha alpha alpha rollout discussion"},
            {"source": "live_transcript", "heading": "Others",
             "text": "beta beta beta pricing model"},
        ])
        self.assertEqual(added, 2)
        hits = index.query("alpha", top_k=1)
        self.assertTrue(hits)
        self.assertEqual(hits[0].heading, "You")
        self.assertIn("alpha", hits[0].text.lower())

    def test_add_chunks_max_eviction(self) -> None:
        index = KBIndex([], self.KeywordEmbedder(),
                        log=lambda _m: None, min_score=0.0, max_chunks=2)
        index.init_empty()
        for i in range(5):
            index.add_chunks([{"source": "s", "heading": "h",
                               "text": "alpha " * (i + 1)}])
        self.assertEqual(len(index._chunks), 2)
        self.assertEqual(len(index._vectors), 2)


class LocalHttpTests(unittest.TestCase):
    def test_host_header_canonicalization(self) -> None:
        self.assertEqual(host_from_header("127.0.0.1:43123"), "127.0.0.1")
        self.assertEqual(host_from_header("[::1]:43123"), "::1")
        self.assertEqual(host_from_header("LOCALHOST"), "localhost")

    def test_ipv6_url_formatting(self) -> None:
        self.assertEqual(url_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(url_host("::1"), "[::1]")


class IdentityTests(unittest.TestCase):
    def test_title_uses_first_substantive_transcript_line(self) -> None:
        title, evidence = derive_title(
            "[10:00:00] **You:** Hi everyone\n"
            "[10:00:03] **Client:** We need to finalize the enrollment timeline for fall.")
        self.assertEqual(title, "We need to finalize the enrollment timeline for fall")
        self.assertIn("enrollment timeline", evidence)

    def test_identity_keeps_evidence_and_participants(self) -> None:
        cfg = mock.Mock(record_mic=True, stt_backend="local",
                        stt_model="turbo", answers_backend="groq",
                        chat_model="model")
        identity = build_identity(
            datetime(2026, 9, 22, 10, 11, 12), None, "10-11-12_ab12cd34",
            "Dana: We agreed to launch the new onboarding flow next week.", cfg)
        self.assertEqual(identity["title_source"], "first_substantive_transcript_line")
        self.assertEqual(identity["participants"], ["Dana"])
        self.assertEqual(identity["original_folder"], "10-11-12_ab12cd34")
        self.assertEqual(meaningful_folder_name("10-11-12_ab12cd34", identity),
                         "10-11-12_we-agreed-to-launch-the-new-onboarding-flow-next-week_ab12cd34")

    def test_identity_persists_user_speaker_mappings(self) -> None:
        cfg = mock.Mock(record_mic=True, stt_backend="local", stt_model="turbo",
                        answers_backend="groq", chat_model="model")
        identity = build_identity(
            datetime(2026, 9, 22, 10, 11, 12), None, "10-11-12_ab12cd34",
            "Client: We agreed to launch the new onboarding flow next week.", cfg,
            speaker_mappings={"remote": {"speaker_id": "remote", "label": "Dana"}})
        self.assertEqual(identity["speaker_mappings"]["remote"]["label"], "Dana")

    def test_slugify_is_safe_and_bounded(self) -> None:
        self.assertEqual(slugify("Résumé: Q3 / enrollment?"), "resume-q3-enrollment")
        self.assertLessEqual(len(slugify("word " * 100)), 64)

    def test_post_call_diarization_is_enabled_by_default_but_safe_when_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = HudConfig()
            self.assertTrue(cfg.diarization_enabled)
            self.assertIsNone(run_post_call_diarization(
                Path(tmp) / "remote.wav", [], Path(tmp) / "derived", cfg,
                lambda _m: None))

    def test_diarization_readiness_is_secret_free_and_reflects_setup(self) -> None:
        with mock.patch("hud.diarization._whisperx_binary", return_value="/tmp/whisperx"), \
                mock.patch("hud.diarization._resolve_hf_token", return_value="hf_secret"):
            status = diarization_readiness()
        self.assertTrue(status["ready"])
        self.assertEqual(status["state"], "ready")
        self.assertNotIn("hf_secret", json.dumps(status))

        with mock.patch("hud.diarization._whisperx_binary", return_value=None):
            status = diarization_readiness()
        self.assertFalse(status["ready"])
        self.assertEqual(status["state"], "not_installed")

    def test_post_call_diarization_writes_derived_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "remote.wav"
            audio.write_bytes(b"audio")
            cfg = HudConfig(diarization_enabled=True, diarization_backend="whisperx")
            launched = {}

            def fake_run(command, **_kwargs):
                launched["command"] = command
                outdir = Path(command[command.index("--output_dir") + 1])
                (outdir / "remote.json").write_text(json.dumps({
                    "segments": [{"start": 0.0, "end": 2.0,
                                  "speaker": "SPEAKER_00", "text": "hello"}]
                }), encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.dict(os.environ, {"HF_TOKEN": "test-token"}), \
                    mock.patch("hud.diarization.shutil.which", return_value="whisperx"), \
                    mock.patch("hud.diarization.subprocess.run", side_effect=fake_run):
                result = run_post_call_diarization(
                    audio,
                    [{"type": "transcript", "text": "hello there", "speaker": "Others",
                      "speaker_id": "remote", "captured_at": 101.0, "ts": 101.0}],
                    root / "derived", cfg, lambda _m: None, started_epoch=100.0)
            self.assertIsNotNone(result)
            self.assertIn("--speaker_embeddings", launched["command"])
            self.assertNotIn("--hf_token", launched["command"])
            self.assertTrue((root / "derived" / "diarization.json").is_file())
            rendered = (root / "derived" / "diarized_transcript.md").read_text()
            self.assertIn("Remote 1", rendered)

    def test_voice_profiles_require_manual_enrollment_and_match_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice_profiles.json"
            store = VoiceProfileStore(path, threshold=0.8)
            self.assertIsNone(store.match([1.0, 0.0]))
            profile = store.enroll("Sarah", [1.0, 0.0], "session-a")
            self.assertEqual(profile["label"], "Sarah")
            match = store.match([0.99, 0.05])
            self.assertEqual(match["label"], "Sarah")
            self.assertGreaterEqual(match["confidence"], 0.65)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(store.forget(profile["profile_id"]))
            self.assertIsNone(store.match([1.0, 0.0]))

    def test_diarization_enrolls_only_explicit_single_speaker_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "remote.wav"
            audio.write_bytes(b"audio")
            cfg = HudConfig(diarization_enabled=True, diarization_backend="whisperx",
                            voice_profiles_path=str(root / "profiles.json"))

            def fake_run(command, **_kwargs):
                outdir = Path(command[command.index("--output_dir") + 1])
                (outdir / "remote.json").write_text(json.dumps({
                    "segments": [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00",
                                  "embedding": [1.0, 0.0], "text": "hello"}]
                }), encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.dict(os.environ, {"HF_TOKEN": "test-token"}), \
                    mock.patch("hud.diarization.shutil.which", return_value="whisperx"), \
                    mock.patch("hud.diarization.subprocess.run", side_effect=fake_run):
                result = run_post_call_diarization(
                    audio, [], root / "derived", cfg, lambda _m: None,
                    mappings={"remote": {"label": "Sarah", "source": "user"}})
            self.assertEqual(result["voice_profiles_enrolled"][0]["label"], "Sarah")
            self.assertEqual(result["segments"][0]["speaker_source"], "user")

    def test_session_stop_finalizes_folder_and_metadata(self) -> None:
        from hud.session import LiveSession

        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "10-11-12_ab12cd34"
            original.mkdir()
            order = []
            session = LiveSession(HudConfig(), original, lambda _m: None, "Mic",
                                  started_at=datetime(2026, 9, 22, 10, 11, 12))
            session._started = True
            session.state.add("transcript", source="live", speaker="Client",
                              text="We agreed to finalize enrollment planning next week.")
            session.stt = mock.Mock()
            session.stt.stop.side_effect = lambda: order.append("stt")
            session.answers = mock.Mock()
            session.answers.finish.side_effect = lambda: order.append("finish") or None
            session.answers.stop.side_effect = lambda: order.append("answers")
            session._writeback = mock.Mock()
            session._writeback.stop.side_effect = lambda: order.append("writeback")
            session._persist = lambda summary=None: order.append("persist")
            session.stop()
            self.assertNotEqual(session.outdir, original)
            self.assertIn("enrollment", session.outdir.name)
            self.assertTrue((session.outdir / "session.json").is_file())
            self.assertEqual(order, ["stt", "finish", "answers", "writeback", "persist"])


class MemoryAndReplayTests(unittest.TestCase):
    def test_word_error_stats_normalizes_and_counts_edits(self) -> None:
        self.assertEqual(normalize_words("Hello, WORLD! It's fine."),
                         ["hello", "world", "it's", "fine"])
        stats = word_error_stats("we ship next week", "we shipped next")
        self.assertEqual(stats.substitutions, 1)
        self.assertEqual(stats.deletions, 1)
        self.assertEqual(stats.insertions, 0)
        self.assertEqual(stats.errors, 2)
        self.assertEqual(stats.reference_words, 4)
        self.assertEqual(stats.wer, 0.5)

    def test_memory_extracts_exact_decision_and_number_evidence(self) -> None:
        items = extract_memory("We agreed to launch in Q3 with a $50,000 budget.",
                               "Client", 123.0, 7)
        self.assertEqual({item["kind"] for item in items},
                         {"decision", "fact_with_number"})
        self.assertTrue(all(item["evidence"].startswith("We agreed") for item in items))

    def test_replay_and_benchmark_are_provider_free(self) -> None:
        events = [
            {"type": "transcript", "source": "live", "speaker": "Client",
             "text": "We agreed to ship next week."},
            {"type": "transcript", "source": "live", "speaker": "Client",
             "text": "What is the rollout plan?"},
        ]
        state = replay(events)
        self.assertEqual(len(state.snapshot()["transcript"]), 2)
        self.assertGreaterEqual(len(state.memory()), 1)
        stats = benchmark(events)
        self.assertEqual(stats["events"], 2)
        self.assertGreater(stats["memory_items"], 0)

    def test_memory_prompt_format_preserves_evidence(self) -> None:
        text = format_memory([{"kind": "decision", "text": "ship it",
                               "evidence": "We agreed to ship it", "ts": 1.0}])
        self.assertIn("We agreed to ship it", text)

    def test_state_metrics_expose_percentiles_without_event_noise(self) -> None:
        state = LiveState()
        for value in (1, 2, 3, 4, 5):
            state.observe_metric("latency", value)
        metrics = state.metrics()["latency"]
        self.assertEqual(metrics["count"], 5)
        self.assertEqual(metrics["p50"], 3.0)
        self.assertEqual(metrics["p95"], 5.0)
        self.assertEqual(state.latest_id(), 0)


class TranscriptWritebackTests(unittest.TestCase):
    def test_mirrors_transcript_events_and_drains_on_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = LiveState()
            logs = []
            sink = TranscriptWriteback(state, tmp, "2026-01-01_session/", logs.append)
            self.assertTrue(sink.start())
            state.add("transcript", text="hello from the call", speaker="You")
            state.add("transcript", text="welcome", speaker="Client")
            sink.stop()
            files = list(Path(tmp).glob("*-live-transcript.md"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text(encoding="utf-8")
            self.assertIn("# Live transcript", text)
            self.assertIn("**You:** hello from the call", text)
            self.assertIn("**Client:** welcome", text)

    def test_reused_session_name_gets_a_fresh_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "session-live-transcript.md"
            existing.write_text("old session\n", encoding="utf-8")
            sink = TranscriptWriteback(LiveState(), tmp, "session", lambda _m: None)
            sink.start()
            sink.stop()
            self.assertEqual(sink.path.name, "session-live-transcript-2.md")
            self.assertEqual(existing.read_text(encoding="utf-8"), "old session\n")

    def test_writeback_failure_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "not-a-folder"
            blocked.write_text("occupied", encoding="utf-8")
            logs = []
            sink = TranscriptWriteback(LiveState(), str(blocked), "session", logs.append)
            self.assertTrue(sink.start())
            sink.stop()
            self.assertTrue(any("writeback disabled" in line for line in logs))

    def test_completed_file_can_be_renamed_after_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sink = TranscriptWriteback(LiveState(), tmp, "10-00-00_ab12cd34", lambda _m: None)
            sink.start()
            sink.stop()
            sink.finalize_name("enrollment-planning")
            self.assertTrue((Path(tmp) / "enrollment-planning-live-transcript.md").is_file())


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

    def test_record_trues_up_reservation(self) -> None:
        b = BudgetGovernor(tpm=100000, tpd=10000)
        b.reserve(1000)
        self.assertEqual(b.snapshot()["day_tokens"], 1000)
        b.record({}, {"total_tokens": 300})
        self.assertEqual(b.snapshot()["day_tokens"], 300)


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

    def test_round_trip(self) -> None:
        cfg = HudConfig(
            speakers_enabled=True, self_name="Dana", remote_name="Client",
            answers_backend="openrouter", answers_fallback=["groq", "ollama"],
            kb_dirs=["/a", "/b"], kb_top_k=7, budget_tpm=100, budget_tpd=200,
            port=1234, open_browser=False, answer_interval=20.0,
            chat_model="m1", rolling_model="m2", answers_enabled=False,
            context_max_chars=4321, question_lookback_seconds=42.0,
            question_rewrite=False, kb_embed_backend="ollama",
            kb_embed_model="nomic-embed-text", kb_min_score=0.25,
            answer_self_questions=True, point_dedupe_score=0.8,
            max_context_qa=5, summary_enabled=False, persist_seconds=15.0,
            talking_points_grounded=False, talking_points_max=2,
            talking_points_min_new_words=80, talking_points_min_words=30,
            talking_points_quote_overlap=0.6,
            stt_adaptive_chunking=False, stt_chunk_min_seconds=2.5,
            stt_chunk_max_seconds=8.0, stt_chunk_overlap_seconds=0.25,
            transcript_writeback_dir="~/Obsidian/LiveTranscripts",
            diarization_enabled=True, diarization_backend="whisperx",
            diarization_timeout_seconds=90.0)
        again = config_from_dict(config_to_dict(cfg))
        for attr in ("self_name", "remote_name", "answers_backend", "answers_fallback",
                     "kb_dirs", "kb_top_k", "budget_tpm", "budget_tpd", "port",
                     "open_browser", "answer_interval", "chat_model", "rolling_model",
                     "answers_enabled", "context_max_chars", "question_lookback_seconds",
                     "question_rewrite", "kb_embed_backend", "kb_embed_model",
                     "kb_min_score", "answer_self_questions", "point_dedupe_score",
                     "max_context_qa", "summary_enabled", "persist_seconds",
                     "talking_points_grounded", "talking_points_max",
                     "talking_points_min_new_words", "talking_points_min_words",
                     "talking_points_quote_overlap", "transcript_writeback_dir",
                     "stt_adaptive_chunking", "stt_chunk_min_seconds",
                     "stt_chunk_max_seconds", "stt_chunk_overlap_seconds",
                     "diarization_enabled", "diarization_backend",
                     "diarization_timeout_seconds",
                     "stt_partial_enabled", "stt_partial_window_seconds",
                     "stt_partial_interval_seconds"):
            self.assertEqual(getattr(cfg, attr), getattr(again, attr), attr)

    def test_save_preserves_unknown_keys_and_backs_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"custom": {"keep": 1}, "api_keys": {"groq": "OLD"}}))
            save_config(HudConfig(self_name="Dana", api_keys={"groq": "NEW"}), path)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["custom"], {"keep": 1})
            self.assertEqual(saved["speakers"]["self_name"], "Dana")
            self.assertEqual(saved["api_keys"]["groq"], "NEW")
            self.assertTrue((Path(tmp) / "config.json.bak").is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_save_uses_explicit_api_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"api_keys": {"groq": "KEEP", "openrouter": "DROP"}}))
            save_config(HudConfig(self_name="Dana"), path,
                        api_keys={"groq": "KEEP", "openrouter": ""})
            keys = json.loads(path.read_text())["api_keys"]
            self.assertEqual(keys["groq"], "KEEP")
            self.assertEqual(keys["openrouter"], "")

    def test_load_missing_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp) / "nope.json")
            self.assertEqual(cfg.self_name, "You")
            self.assertEqual(cfg.answers_backend, "groq")
            self.assertTrue(cfg.recorder.transcription_model.endswith(
                "ggml-large-v3-turbo-q5_0.bin"))


class StateTests(unittest.TestCase):
    def test_events_and_snapshot(self) -> None:
        state = LiveState()
        state.add("transcript", text="hello")
        state.add("transcript", text="world")
        state.add_talking_points(["a useful point"])
        snap = state.snapshot()
        self.assertEqual(len(snap["transcript"]), 2)
        self.assertEqual(len(snap["answers"]), 0)
        self.assertEqual(len(snap["talking_points"]), 1)
        text = state.transcript_text()
        self.assertIn("hello", text)
        self.assertIn("world", text)
        self.assertIn("Talking points", state.answers_markdown())

    def test_partial_transcript_is_replaceable_and_not_authoritative(self) -> None:
        state = LiveState()
        state.set_transcript_partial("You", "what is the", "You", 1)
        state.set_transcript_partial("You", "plan", "You", 2)
        state.add("transcript", source="live", speaker="You", text="what is")
        snap = state.snapshot()
        self.assertEqual(len(snap["partials"]), 1)
        self.assertEqual(snap["partials"][0]["text"], "plan")
        self.assertNotIn("plan", state.transcript_text())

    def test_nonfinal_transcript_events_stay_out_of_saved_outputs(self) -> None:
        state = LiveState()
        state.add("transcript", source="live", text="wrong draft", finalized=False)
        state.add("transcript", source="live", text="correct final", finalized=True)
        self.assertNotIn("wrong draft", state.transcript_text())
        self.assertIn("correct final", state.transcript_text())
        self.assertNotIn("wrong draft", state.timeline_markdown())

    def test_talking_points_dedupe_and_snapshot(self) -> None:
        state = LiveState()
        added = state.add_talking_points(["Price is $10 per seat.", "Ship in Q3."])
        self.assertEqual(len(added), 2)
        again = state.add_talking_points([
            "price is 10 per seat", "Latency budget is 200ms"])
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["type"], "talking_point")
        self.assertEqual(again[0]["text"], "Latency budget is 200ms")
        self.assertEqual(len(state.talking_points()), 3)
        # First point event carries the event id the SSE client needs.
        self.assertTrue(added[0]["id"])

    def test_since_and_latest(self) -> None:
        state = LiveState()
        first = state.add("transcript", text="a")
        state.add("transcript", text="b")
        self.assertEqual(state.latest_id(), 2)
        events = state.since(first["id"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["text"], "b")

    def test_set_meta_emits_event(self) -> None:
        state = LiveState()
        state.set_meta(stt_lag_seconds=1.5)
        self.assertEqual(state.snapshot()["meta"]["stt_lag_seconds"], 1.5)
        metas = [e for e in state.since(0) if e["type"] == "meta"]
        self.assertEqual(metas[-1]["meta"]["stt_lag_seconds"], 1.5)

    def test_timeline_interleaves_transcript_and_answers(self) -> None:
        state = LiveState()
        state.add("transcript", text="hello there")
        state.add_talking_points(["point a"])
        state.add("transcript", text="what is the plan?")
        state.add("answer", kind="question", question="what is the plan?",
                  bullets=["phase 1"], sources=["notes.md"])
        md = state.timeline_markdown(title="Live conversation")
        self.assertIn("Live conversation", md)
        # Chronological: speech, its answer, next speech, its answer.
        self.assertLess(md.index("hello there"), md.index("point a"))
        self.assertLess(md.index("point a"), md.index("what is the plan?"))
        self.assertIn("notes.md", md)

    def test_timeline_empty(self) -> None:
        self.assertEqual(LiveState().timeline_markdown(), "")

    def test_speaker_labels_in_outputs(self) -> None:
        state = LiveState()
        state.add("transcript", text="hello there", speaker="Dana")
        state.add("transcript", text="glad to meet you", speaker="Client")
        text = state.transcript_text()
        self.assertIn("Dana: hello there", text)
        self.assertIn("Client: glad to meet you", text)
        timeline = state.timeline_markdown()
        self.assertIn("Dana: hello there", timeline)

    def test_speaker_override_updates_historical_outputs_and_emits_event(self) -> None:
        state = LiveState()
        state.add("transcript", text="hello there", speaker="Others", speaker_id="remote")
        self.assertTrue(state.set_speaker_label("remote", "Dana"))
        self.assertIn("Dana: hello there", state.transcript_text())
        self.assertNotIn("Others: hello there", state.timeline_markdown())
        mapping = [e for e in state.since(0) if e["type"] == "speaker_mapping"][-1]
        self.assertEqual(mapping["speaker_id"], "remote")
        self.assertEqual(state.snapshot()["speaker_mappings"]["remote"]["label"], "Dana")

    def test_clearing_speaker_override_restores_captured_label(self) -> None:
        state = LiveState()
        state.add("transcript", text="hello there", speaker="Others", speaker_id="remote")
        state.set_speaker_label("remote", "Dana")
        state.set_speaker_label("remote", "")
        self.assertIn("Others: hello there", state.transcript_text())


class SourceTests(unittest.TestCase):
    def _transcriber(self, cfg, mic, system):
        return LiveTranscriber(LiveState(), lambda _m: None, cfg, mic, system)

    def test_two_labelled_sources(self) -> None:
        cfg = HudConfig(speakers_enabled=True, self_name="Dana", remote_name="Client")
        sources = self._transcriber(cfg, "Mic", "System")._build_sources()
        self.assertEqual([s.speaker for s in sources], ["Dana", "Client"])
        # Separate taps, no mixing.
        self.assertTrue(all("avfoundation" in s.cmd for s in sources))
        self.assertTrue(all("amix" not in " ".join(s.cmd) for s in sources))

    def test_mixed_when_labels_disabled(self) -> None:
        cfg = HudConfig(speakers_enabled=False)
        sources = self._transcriber(cfg, "Mic", "System")._build_sources()
        self.assertEqual(len(sources), 1)
        self.assertIsNone(sources[0].speaker)
        self.assertIn("amix", " ".join(sources[0].cmd))

    def test_mic_only_is_labelled(self) -> None:
        cfg = HudConfig(speakers_enabled=True, self_name="Dana")
        sources = self._transcriber(cfg, "Mic", None)._build_sources()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].speaker, "Dana")

    def test_file_fixture_is_paced_like_live_audio(self) -> None:
        cfg = HudConfig(audio_file="/tmp/dialogue.wav")
        source = self._transcriber(cfg, None, None)._build_sources()[0]
        self.assertIn("-re", source.cmd)
        self.assertEqual(source.speaker_id, "unknown")


class SettingsServerTests(unittest.TestCase):
    def setUp(self) -> None:
        from hud.settings import SettingsApp

        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.app = SettingsApp(port=0, config_path=self.path, open_browser=False,
                               idle_timeout=0, markers=False, log=lambda _m: None)
        self.port = self.app.start()
        self.base = "http://127.0.0.1:{}".format(self.port)

    def tearDown(self) -> None:
        self.app.stop()
        self.tmp.cleanup()

    def _call(self, path, body=None, token=True):
        import urllib.error
        import urllib.request

        q = "&" if "?" in path else "?"
        url = self.base + path + (q + "token=" + self.app.token if token else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    def test_config_masks_keys(self) -> None:
        status, data = self._call("/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["config"].get("api_keys"), {})
        self.assertIn("groq", data["api_keys_set"])

    def test_token_required(self) -> None:
        status, _ = self._call("/api/config", token=False)
        self.assertEqual(status, 403)

    def test_save_and_reload(self) -> None:
        _, data = self._call("/api/config")
        cfg = data["config"]
        cfg["speakers"]["self_name"] = "Dana"
        cfg["answers"]["backend"] = "openrouter"
        status, res = self._call("/api/config", body={
            "config": cfg, "api_key_new": {"groq": "gsk_TEST"}})
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["speakers"]["self_name"], "Dana")
        self.assertEqual(saved["answers"]["backend"], "openrouter")
        self.assertEqual(saved["api_keys"]["groq"], "gsk_TEST")
        # Reloading reports the key as set, still without leaking it.
        _, data2 = self._call("/api/config")
        self.assertTrue(data2["api_keys_set"]["groq"])
        self.assertNotIn("gsk_TEST", json.dumps(data2))

    def test_blank_key_field_keeps_existing(self) -> None:
        self._call("/api/config", body={"config": {"speakers": {"self_name": "A"}},
                                        "api_key_new": {"groq": "KEEP"}})
        # A later save with no key updates must not clear it.
        self._call("/api/config", body={"config": {"speakers": {"self_name": "B"}}})
        self.assertEqual(json.loads(self.path.read_text())["api_keys"]["groq"], "KEEP")


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
            state.add_talking_points(["a live point"])
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/state".format(port), timeout=5) as resp:
                snap = json.loads(resp.read())
                self.assertEqual(snap["transcript"][0]["text"], "live text")
                self.assertEqual(snap["talking_points"][0]["text"], "a live point")
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/".format(port), timeout=5) as resp:
                html = resp.read().decode("utf-8")
                self.assertIn("Suggestions", html)
                self.assertIn("talking_point", html)
                self.assertIn("Stop recording", html)
        finally:
            server.stop()

    def test_stop_endpoint_calls_callback(self) -> None:
        import urllib.request

        state = LiveState()
        called = {"n": 0}
        server = HudServer(state, port=0, on_stop=lambda: called.__setitem__("n", called["n"] + 1))
        port = server.start()
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:{}/stop".format(port), method="POST",
                data=b"{}", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertTrue(json.loads(resp.read())["ok"])
            self.assertEqual(called["n"], 1)
        finally:
            server.stop()

    def test_speaker_label_endpoint(self) -> None:
        import urllib.request

        state = LiveState()
        server = HudServer(state, port=0,
                           on_speaker_label=state.set_speaker_label)
        port = server.start()
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:{}/speaker-label".format(port), method="POST",
                data=json.dumps({"speaker_id": "remote", "label": "Dana"}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertTrue(json.loads(resp.read())["ok"])
            self.assertEqual(state.speaker_mappings()["remote"]["label"], "Dana")
        finally:
            server.stop()


class HudWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = LiveState()
        self.seen = {}
        self.server = HudServer(
            self.state, port=0, token="secret",
            on_ask=lambda text, expand: self.seen.__setitem__("ask", (text, expand)) or True,
            on_pause=lambda paused: self.seen.__setitem__("paused", paused))
        self.port = self.server.start()
        self.base = "http://127.0.0.1:{}".format(self.port)

    def tearDown(self) -> None:
        self.server.stop()

    def _post(self, path, body):
        import urllib.request

        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def test_token_required(self) -> None:
        import urllib.error
        import urllib.request

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/state", timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_token_allows_state_and_ask(self) -> None:
        import urllib.request

        with urllib.request.urlopen(self.base + "/state?token=secret", timeout=5) as resp:
            self.assertIn("status", json.loads(resp.read()))
        result = self._post("/ask?token=secret", {"text": "what is the plan?"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.seen["ask"], ("what is the plan?", False))
        result = self._post("/ask?token=secret", {"text": "expand me", "expand": True})
        self.assertTrue(result["ok"])
        self.assertEqual(self.seen["ask"], ("expand me", True))

    def test_pause_endpoint(self) -> None:
        result = self._post("/pause?token=secret", {"paused": True})
        self.assertTrue(result["ok"])
        self.assertTrue(self.seen["paused"])


class LLMTests(unittest.TestCase):
    def test_embed_orders_by_index(self) -> None:
        from hud.llm import LLMClient

        client = LLMClient("http://example.invalid/v1", "k")

        def fake_request(path, data, content_type, timeout=None):
            self.assertEqual(path, "/embeddings")
            return ({"data": [
                {"index": 1, "embedding": [1.0, 2.0]},
                {"index": 0, "embedding": [3.0, 4.0]},
            ]}, {})

        client._request = fake_request  # type: ignore[assignment]
        vectors = client.embed(["a", "b"], "m")
        self.assertEqual(vectors, [[3.0, 4.0], [1.0, 2.0]])


class HttpClientTests(unittest.TestCase):
    def _serve(self, handler_cls):
        import http.server
        import threading

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server

    def test_keepalive_connection_is_reused(self) -> None:
        import http.server
        from hud.llm import LLMClient

        connections = {"n": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                connections["n"] += 1
                super().setup()

            def log_message(self, *args):
                return

            def do_GET(self):
                payload = json.dumps({"data": [{"id": "m"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = self._serve(Handler)
        client = LLMClient("http://127.0.0.1:{}/v1".format(server.server_address[1]), None)
        self.addCleanup(client.close)
        for _ in range(3):
            self.assertEqual(client.models(), ["m"])
        # Three requests, one persistent connection.
        self.assertEqual(connections["n"], 1)

    def test_http_error_maps_status_and_retry_after(self) -> None:
        import http.server
        from hud.llm import LLMClient, LLMError

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def do_POST(self):
                body = b'{"error": "slow down"}'
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", "3")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = self._serve(Handler)
        client = LLMClient("http://127.0.0.1:{}".format(server.server_address[1]), None)
        self.addCleanup(client.close)
        with self.assertRaises(LLMError) as ctx:
            client.chat([{"role": "user", "content": "hi"}], "m")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.retry_after, 3.0)

    def test_network_error_raises_llm_error(self) -> None:
        from hud.llm import LLMClient, LLMError

        # Port 1 is not listening; connection refused.
        client = LLMClient("http://127.0.0.1:1", None, timeout=2.0)
        with self.assertRaises(LLMError):
            client.chat([{"role": "user", "content": "hi"}], "m")


class MenuStateTests(unittest.TestCase):
    def test_idle(self) -> None:
        state = describe(recording=False, hud_active=False)
        self.assertEqual(state["toggle_title"], "Start recording")
        self.assertEqual(state["live_title"], "Start with live transcript")
        self.assertTrue(state["live_enabled"])
        self.assertFalse(state["open_enabled"])

    def test_recording_without_hud(self) -> None:
        state = describe(recording=True, hud_active=False, elapsed_s=754)
        self.assertEqual(state["toggle_title"], "Stop recording (12:34)")
        self.assertFalse(state["live_enabled"])
        self.assertFalse(state["open_enabled"])

    def test_recording_with_hud(self) -> None:
        state = describe(recording=True, hud_active=True)
        self.assertEqual(state["live_title"], "Live transcript active ✓")
        self.assertFalse(state["live_enabled"])
        self.assertTrue(state["open_enabled"])

    def test_live_unavailable(self) -> None:
        state = describe(recording=False, hud_active=False, live_available=False)
        self.assertFalse(state["live_enabled"])
        self.assertIn("set up", state["live_title"])

    def test_format_elapsed(self) -> None:
        from hud.menu_state import format_elapsed

        self.assertEqual(format_elapsed(0), "0:00")
        self.assertEqual(format_elapsed(65), "1:05")
        self.assertEqual(format_elapsed(3725), "1:02:05")

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

        def chat_stream(self, messages, model, max_tokens=600, temperature=0.2,
                        response_format=None, timeout=None, on_chunk=None):
            # Delegate to chat; streaming is tested at the LLMClient level.
            result = self.chat(messages, model, max_tokens, temperature,
                              response_format, timeout)
            if on_chunk and result.text:
                on_chunk(result.text)
            return result

    class _EchoClient:
        def chat(self, messages, model, max_tokens=600, temperature=0.2,
                 response_format=None, timeout=None):
            return LLMResult(text=json.dumps({"bullets": ["Point one", "Point two"]}),
                             model=model, usage={"total_tokens": 5})

        def chat_stream(self, messages, model, max_tokens=600, temperature=0.2,
                        response_format=None, timeout=None, on_chunk=None):
            result = self.chat(messages, model, max_tokens, temperature,
                              response_format, timeout)
            if on_chunk and result.text:
                on_chunk(result.text)
            return result

    class _KeywordEmbedder:
        label = "test:keywords"
        KEYWORDS = ["pricing", "dollars", "seat", "rollout", "latency"]

        def encode(self, texts):
            return [[float(t.lower().count(k)) for k in self.KEYWORDS] for t in texts]

    class _Chain:
        def __init__(self, entries):
            self.entries = entries

    def test_answer_claims_require_explicit_evidence_when_provided(self) -> None:
        claims = parse_answer_claims(json.dumps({"claims": [
            {"text": "The launch is Friday", "evidence": "launch is Friday"},
            {"text": "The budget is $5M", "evidence": "budget is $5M"},
        ]}))
        bullets, evidence, dropped = verify_answer_claims(
            claims, "[10:00] Client: The launch is Friday.", [])
        self.assertEqual(bullets, ["The launch is Friday"])
        self.assertEqual(len(evidence), 1)
        self.assertEqual(dropped, ["The budget is $5M"])

    def _engine(self, state, cfg):
        engine = AnswerEngine(state, lambda _m: None, cfg)
        engine._chain = self._Chain([{
            "name": "groq", "client": self._FakeClient(),
            "chat_model": "m", "rolling_model": "m", "structured": True,
        }])
        return engine

    def test_json_mode_400_falls_back_to_plain_text(self) -> None:
        cfg = HudConfig(answers_backend="groq", rolling_enabled=False,
                        kb_enabled=False, question_rewrite=False)
        state = LiveState()
        engine = self._engine(state, cfg)
        engine._buffer = [Turn(1, 0.0, "Client", "What is the rollout plan?")]
        engine._execute({"kind": "question", "question": "What is the rollout plan?",
                         "context": "[00:00:00] Client: What is the rollout plan?",
                         "window": engine._context_text()})

        # Tried structured first, then retried plain.
        self.assertEqual(engine._chain.entries[0]["client"].response_formats,
                         [{"type": "json_object"}, None])
        answers = state.snapshot()["answers"]
        self.assertEqual(answers[0]["bullets"], ["first point", "second point"])
        self.assertEqual(answers[0]["kind"], "question")

    def test_rate_limited_primary_uses_answer_fallback(self) -> None:
        class RateLimitedClient:
            def chat(self, *args, **kwargs):
                raise LLMError("slow down", status=429, retry_after=0.01)

            def chat_stream(self, *args, **kwargs):
                raise LLMError("slow down", status=429, retry_after=0.01)

        cfg = HudConfig(answers_backend="groq", answers_fallback=["openrouter"],
                        rolling_enabled=False, kb_enabled=False)
        engine = AnswerEngine(LiveState(), lambda _m: None, cfg)
        engine._chain = self._Chain([
            {"name": "groq", "client": RateLimitedClient(),
             "chat_model": "groq-model", "rolling_model": "groq-model",
             "structured": True},
            {"name": "openrouter", "client": self._EchoClient(),
             "chat_model": "fallback-model", "rolling_model": "fallback-model",
             "structured": False},
        ])
        result = engine._chat_stream("question", "stable instructions", "dynamic question")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.model, "fallback-model")

    def test_tick_enqueues_indirect_question_for_answer_worker(self) -> None:
        cfg = HudConfig(answers_backend="groq", kb_enabled=False,
                        rolling_enabled=False)
        state = LiveState()
        engine = AnswerEngine(state, lambda _m: None, cfg)
        state.add("transcript", source="live", speaker="Client",
                  text="I was wondering whether your team supports staged rollouts")
        engine._tick()
        _priority, _seq, job = engine._queue.get_nowait()
        self.assertEqual(job["kind"], "question")
        self.assertIn("supports staged rollouts", job["question"])

    def test_partial_question_draft_updates_without_enqueuing_answer(self) -> None:
        cfg = HudConfig(answers_backend="groq", kb_enabled=False,
                        rolling_enabled=False)
        state = LiveState()
        engine = AnswerEngine(state, lambda _m: None, cfg)
        state.set_transcript_partial("Client", "what is the plan?", "Client", 1)
        engine._tick()
        self.assertEqual(state.snapshot()["meta"]["question_draft"], "what is the plan?")
        with self.assertRaises(queue.Empty):
            engine._queue.get_nowait()

    def test_stable_prefix_waits_for_final_boundary_before_answering(self) -> None:
        cfg = HudConfig(answers_backend="groq", kb_enabled=False,
                        rolling_enabled=False)
        state = LiveState()
        engine = AnswerEngine(state, lambda _m: None, cfg)
        state.add("transcript", source="live", speaker="Client",
                  text="what is the", finalized=False)
        engine._tick()
        with self.assertRaises(queue.Empty):
            engine._queue.get_nowait()
        state.add("transcript_boundary", source_key="Client", speaker="Client")
        engine._tick()
        _priority, _seq, job = engine._queue.get_nowait()
        self.assertEqual(job["question"], "what is the")

    def test_interim_question_mark_still_waits_for_boundary(self) -> None:
        cfg = HudConfig(answers_backend="groq", kb_enabled=False,
                        rolling_enabled=False)
        state = LiveState()
        engine = AnswerEngine(state, lambda _m: None, cfg)
        state.add("transcript", source="live", speaker="Client",
                  text="what is the plan?", finalized=False)
        engine._tick()
        with self.assertRaises(queue.Empty):
            engine._queue.get_nowait()

    def test_rolling_emits_talking_points_not_answers(self) -> None:
        cfg = HudConfig(answers_backend="groq", kb_enabled=False,
                        talking_points_grounded=False)
        state = LiveState()
        engine = AnswerEngine(state, lambda _m: None, cfg)
        engine._chain = self._Chain([{
            "name": "groq", "client": self._EchoClient(),
            "chat_model": "m", "rolling_model": "m", "structured": False,
        }])
        engine._buffer = [Turn(1, 0.0, "Client", "We should talk about pricing.")]

        engine._rolling_payload = {"window": engine._context_text()}
        engine._execute({"kind": "rolling"})
        snap = state.snapshot()
        self.assertEqual(snap["answers"], [])
        self.assertEqual(len(snap["talking_points"]), 2)
        # A second refresh with identical bullets adds nothing (dedupe).
        engine._rolling_payload = {"window": engine._context_text()}
        engine._execute({"kind": "rolling"})
        self.assertEqual(len(state.talking_points()), 2)

    def test_ambiguous_question_is_rewritten_first(self) -> None:
        cfg = HudConfig(answers_backend="groq", kb_enabled=False,
                        question_rewrite=True)
        state = LiveState()
        engine = AnswerEngine(state, lambda _m: None, cfg)
        client = self._FakeClient()
        engine._chain = self._Chain([{
            "name": "groq", "client": client,
            "chat_model": "m", "rolling_model": "m", "structured": False,
        }])
        # Very short ambiguous question with no context triggers the rewrite.
        engine._buffer = [Turn(1, 0.0, "Client", "What about that?")]
        engine._execute({"kind": "question", "question": "What?",
                         "context": "",
                         "window": engine._context_text()})
        answers = state.snapshot()["answers"]
        self.assertEqual(len(answers), 1)
        self.assertIsNotNone(answers[0]["rewritten_question"])

    def test_self_questions_gated_by_default(self) -> None:
        engine = AnswerEngine(LiveState(), lambda _m: None, HudConfig(self_name="Dana"))
        self.assertFalse(engine._should_answer(
            {"question": "Why is that?", "speaker": "Dana"}))
        self.assertTrue(engine._should_answer(
            {"question": "Why is that?", "speaker": "Client"}))
        self.assertFalse(engine._should_answer(
            {"question": "That is great, right?", "speaker": "Client"}))

    def test_self_questions_can_be_enabled(self) -> None:
        engine = AnswerEngine(LiveState(), lambda _m: None,
                              HudConfig(self_name="Dana", answer_self_questions=True))
        self.assertTrue(engine._should_answer(
            {"question": "Why is that?", "speaker": "Dana"}))

    def test_semantic_dedupe_filters_paraphrase(self) -> None:
        cfg = HudConfig(answers_backend="groq", kb_enabled=False,
                        point_dedupe_score=0.9)
        engine = AnswerEngine(LiveState(), lambda _m: None, cfg)
        engine._embedder = self._KeywordEmbedder()
        existing = ["pricing is ten dollars per seat"]
        picked = engine._filter_new_points(
            ["pricing costs 10 dollars per seat", "latency budget is 200ms"], existing)
        self.assertEqual([t for t, _v in picked], ["latency budget is 200ms"])

    def test_qa_chain_added_to_prompt(self) -> None:
        cfg = HudConfig(answers_backend="groq", kb_enabled=False)
        state = LiveState()
        engine = self._engine(state, cfg)
        engine._buffer = [Turn(1, 0.0, "Client", "What is the price?")]
        engine._execute({"kind": "question", "question": "What is the price?",
                         "context": "", "window": engine._context_text()})
        qa = engine._qa_recent()
        self.assertEqual(len(qa), 1)
        prompt = engine._build_prompt("question", "What about support?", "",
                                      engine._context_text(), [], qa=qa)
        self.assertIn("Earlier questions this call", prompt)
        self.assertIn("What is the price?", prompt)

    def test_worker_processes_manual_ask(self) -> None:
        cfg = HudConfig(answers_backend="groq", kb_enabled=False,
                        question_rewrite=False)
        state = LiveState()
        engine = self._engine(state, cfg)
        worker = threading.Thread(target=engine._worker_loop, daemon=True)
        worker.start()
        self.assertTrue(engine.ask("What about pricing?"))
        for _ in range(60):
            if state.snapshot()["answers"]:
                break
            time.sleep(0.05)
        engine._stop.set()
        engine._queue.put((0, next(engine._job_seq), None))
        worker.join(timeout=2)
        answers = state.snapshot()["answers"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["question"], "What about pricing?")


class TalkingPointGroundingTests(unittest.TestCase):
    class _PointClient:
        def __init__(self, payload):
            self.payload = payload

        def chat(self, messages, model, max_tokens=600, temperature=0.2,
                 response_format=None, timeout=None):
            return LLMResult(text=json.dumps(self.payload), model=model,
                             usage={"total_tokens": 5})

    class _Chain:
        def __init__(self, entries):
            self.entries = entries

    WINDOW = ("[12:02:21] Odin: this is just part of the computer feature for "
              "tagging things with color. Fascinating and a little delightful.")

    def _engine(self, state, payload, **cfg_kwargs):
        cfg = HudConfig(answers_backend="groq", kb_enabled=False, **cfg_kwargs)
        engine = AnswerEngine(state, lambda _m: None, cfg)
        engine._chain = self._Chain([{
            "name": "groq", "client": self._PointClient(payload),
            "chat_model": "m", "rolling_model": "m", "structured": False,
        }])
        return engine

    def test_parse_point_objects(self) -> None:
        raw = json.dumps({"bullets": [
            {"text": "Pricing is final", "quote": "the pricing is final"},
            "plain string bullet",
        ]})
        parsed = parse_point_objects(raw)
        self.assertEqual(parsed[0], {"text": "Pricing is final",
                                     "quote": "the pricing is final"})
        self.assertEqual(parsed[1]["text"], "plain string bullet")
        self.assertEqual(parsed[1]["quote"], "")
        md = parse_point_objects("- one thing\n- another thing")
        self.assertEqual([p["text"] for p in md], ["one thing", "another thing"])

    def test_grounded_accepts_supported_quote(self) -> None:
        self.assertTrue(grounded_in(
            "The feature is part of the computer",
            "this is just part of the computer", self.WINDOW))

    def test_grounded_rejects_fabricated_quote(self) -> None:
        self.assertFalse(grounded_in(
            "Color tags sync across devices",
            "they sync across devices using cloud storage", self.WINDOW))

    def test_grounded_falls_back_to_point_tokens(self) -> None:
        self.assertTrue(grounded_in(
            "tagging things with color", "", self.WINDOW))
        self.assertFalse(grounded_in(
            "Users can create custom color palettes", "", self.WINDOW))

    def test_refresh_drops_ungrounded_points(self) -> None:
        state = LiveState()
        payload = {"bullets": [
            {"text": "Color tags sync across devices via cloud storage",
             "quote": "they sync across devices via cloud storage"},
            {"text": "This is part of the computer",
             "quote": "this is just part of the computer"},
        ]}
        engine = self._engine(state, payload)
        engine._refresh_talking_points({"window": self.WINDOW})
        self.assertEqual([p["text"] for p in state.talking_points()],
                         ["This is part of the computer"])

    def test_refresh_drops_all_invented_points(self) -> None:
        state = LiveState()
        payload = {"bullets": [
            "Color tags are synced across devices via cloud storage",
            "Users can create custom color palettes for their workflow",
            "Color coding integrates with Trello or Asana",
        ]}
        engine = self._engine(state, payload)
        engine._refresh_talking_points({"window": self.WINDOW})
        self.assertEqual(state.talking_points(), [])

    def test_refresh_caps_points(self) -> None:
        state = LiveState()
        quote = "tagging things with color"
        payload = {"bullets": [
            {"text": "A", "quote": quote},
            {"text": "B", "quote": quote},
            {"text": "C", "quote": quote},
        ]}
        engine = self._engine(state, payload, talking_points_max=2)
        engine._refresh_talking_points({"window": self.WINDOW})
        self.assertEqual(len(state.talking_points()), 2)

    def test_substance_gate_blocks_sparse_refresh(self) -> None:
        cfg = HudConfig(answers_backend="groq", kb_enabled=False, answer_interval=0,
                        talking_points_min_new_words=10, talking_points_min_words=1)
        engine = AnswerEngine(LiveState(), lambda _m: None, cfg)
        engine._buffer = [Turn(1, time.time(), "You", "just a few words here")]
        engine._words_since_rolling = 4
        engine._tick()
        self.assertFalse(engine._rolling_queued)
        engine._words_since_rolling = 25
        engine._tick()
        self.assertTrue(engine._rolling_queued)


class SummaryTests(unittest.TestCase):
    class _SummaryClient:
        def chat(self, messages, model, max_tokens=600, temperature=0.2,
                 response_format=None, timeout=None):
            payload = {
                "summary": "We agreed to ship in Q3.",
                "action_items": ["Dana: finalize pricing"],
                "follow_up_email": "Hi team,\nRecap below.",
            }
            return LLMResult(text=json.dumps(payload), model=model,
                             usage={"total_tokens": 30})

    class _Chain:
        def __init__(self, entries):
            self.entries = entries

    def _engine(self, state):
        engine = AnswerEngine(state, lambda _m: None, HudConfig(kb_enabled=False))
        engine._chain = self._Chain([{
            "name": "groq", "client": self._SummaryClient(),
            "chat_model": "m", "rolling_model": "m", "structured": False,
        }])
        return engine

    def test_parse_summary_fallbacks(self) -> None:
        from hud.answers import parse_summary

        parsed = parse_summary('{"summary": "s", "action_items": ["a"]}')
        self.assertEqual(parsed["summary"], "s")
        self.assertEqual(parsed["action_items"], ["a"])
        plain = parse_summary("just some text")
        self.assertEqual(plain["summary"], "just some text")
        self.assertEqual(plain["action_items"], [])

    def test_summarize_returns_structured_output(self) -> None:
        state = LiveState()
        state.add("transcript", text="Dana: we should ship in Q3.", speaker="Dana")
        engine = self._engine(state)
        summary = engine.finish()
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary["summary"], "We agreed to ship in Q3.")
        self.assertEqual(summary["action_items"], ["Dana: finalize pricing"])
        self.assertTrue(summary["follow_up_email"])

    def test_summary_disabled(self) -> None:
        cfg = HudConfig(kb_enabled=False, summary_enabled=False)
        engine = AnswerEngine(LiveState(), lambda _m: None, cfg)
        engine._chain = self._Chain([{
            "name": "groq", "client": self._SummaryClient(),
            "chat_model": "m", "rolling_model": "m", "structured": False,
        }])
        self.assertIsNone(engine.finish())

    def test_summary_respects_live_no_answers(self) -> None:
        cfg = HudConfig(kb_enabled=False, answers_enabled=False)
        engine = AnswerEngine(LiveState(), lambda _m: None, cfg)
        engine._chain = self._Chain([{
            "name": "groq", "client": self._SummaryClient(),
            "chat_model": "m", "rolling_model": "m", "structured": False,
        }])
        self.assertIsNone(engine.finish())

    def test_session_writes_outputs_and_summary(self) -> None:
        from hud.session import LiveSession

        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp) / "2026-01-01_uid"
            outdir.mkdir(parents=True)
            session = LiveSession(HudConfig(), outdir, lambda _m: None, "Mic")
            session.state.add("transcript", text="hello there", speaker="You")
            session.state.add_talking_points(["price is final"])
            session._persist(summary={"summary": "done", "action_items": ["ship"],
                                      "follow_up_email": "hi"})
            derived = outdir / "derived"
            self.assertTrue((derived / "live_transcript.txt").is_file())
            self.assertTrue((derived / "live_conversation.md").is_file())
            summary_md = (derived / "live_summary.md").read_text()
            self.assertIn("done", summary_md)
            self.assertIn("ship", summary_md)
            self.assertIn("hi", summary_md)
            # No leftover temp files from the atomic writes.
            self.assertEqual(list(derived.glob("*.tmp")), [])


class DevicesTests(unittest.TestCase):
    def _topology(self, items, default_input=None, default_output=None):
        from hud import devices

        topo = devices.Topology(default_input=default_input, default_output=default_output)
        topo.devices = [devices.AudioDevice(**d) for d in items]
        for dev in topo.devices:
            if dev.default_input:
                topo.default_input = topo.default_input or dev.name
            if dev.default_output:
                topo.default_output = topo.default_output or dev.name
        return topo

    def test_classification(self) -> None:
        topo = self._topology([
            {"name": "MacBook Air Microphone", "transport": "coreaudio_device_type_builtin",
             "input_channels": 1, "default_input": True},
            {"name": "BlackHole 2ch", "transport": "coreaudio_device_type_virtual",
             "input_channels": 2, "output_channels": 2},
            {"name": "ZoomAudioDevice", "transport": "coreaudio_device_type_virtual",
             "input_channels": 2, "output_channels": 2},
            {"name": "External Headphones", "transport": "coreaudio_device_type_builtin",
             "output_channels": 2, "default_output": True},
        ])
        self.assertTrue(topo.device("MacBook Air Microphone").is_mic)
        self.assertFalse(topo.device("BlackHole 2ch").is_mic)
        self.assertTrue(topo.device("BlackHole 2ch").is_loopback)
        self.assertTrue(topo.device("ZoomAudioDevice").is_loopback)
        self.assertEqual([d.name for d in topo.mics()], ["MacBook Air Microphone"])

    def test_output_path_detection(self) -> None:
        with_loopback_out = self._topology([
            {"name": "BlackHole 2ch", "transport": "virtual", "input_channels": 2,
             "output_channels": 2, "default_output": True},
        ])
        self.assertTrue(with_loopback_out.system_in_output_path)
        headphones_out = self._topology([
            {"name": "BlackHole 2ch", "transport": "virtual", "input_channels": 2},
            {"name": "External Headphones", "transport": "builtin",
             "output_channels": 2, "default_output": True},
        ])
        self.assertFalse(headphones_out.system_in_output_path)

    def test_read_system_profiler_fixture(self) -> None:
        from hud import devices

        payload = {"SPAudioDataType": [{"_items": [
            {"_name": "BlackHole 2ch", "coreaudio_device_transport": "coreaudio_device_type_virtual",
             "coreaudio_device_input": 2, "coreaudio_device_output": 2},
            {"_name": "MacBook Air Microphone",
             "coreaudio_device_transport": "coreaudio_device_type_builtin",
             "coreaudio_device_input": 1,
             "coreaudio_default_audio_input_device": "spaudio_yes"},
            {"_name": "External Headphones",
             "coreaudio_device_transport": "coreaudio_device_type_builtin",
             "coreaudio_device_output": 2,
             "coreaudio_default_audio_output_device": "spaudio_yes"},
        ]}]}

        class _Proc:
            stdout = json.dumps(payload)

        with mock.patch("hud.devices.subprocess.run", return_value=_Proc()):
            topo = devices.read_system_profiler()
        self.assertEqual(topo.default_input, "MacBook Air Microphone")
        self.assertEqual(topo.default_output, "External Headphones")
        self.assertTrue(topo.device("BlackHole 2ch").is_loopback)

    def test_advice_when_only_zoom_loopback(self) -> None:
        from hud import devices

        topo = self._topology([
            {"name": "ZoomAudioDevice", "transport": "virtual", "input_channels": 2,
             "output_channels": 2},
        ])
        self.assertIn("BlackHole", devices.system_advice(topo))

    def test_advice_when_loopback_not_in_output_path(self) -> None:
        from hud import devices

        topo = self._topology([
            {"name": "BlackHole 2ch", "transport": "virtual", "input_channels": 2},
            {"name": "External Headphones", "transport": "builtin",
             "output_channels": 2, "default_output": True},
        ])
        self.assertIn("Multi-Output", devices.system_advice(topo))

    def test_priorities_prefer_blackhole_over_zoom(self) -> None:
        from hud import devices

        self.assertGreater(devices.system_priority("BlackHole 2ch"),
                           devices.system_priority("ZoomAudioDevice"))

    def test_update_devices_updates_names(self) -> None:
        tr = LiveTranscriber(LiveState(), lambda _m: None, HudConfig(), "MicA", "SysA")
        tr._stt = None
        tr._sources = []
        tr.update_devices("MicB", "SysB")
        self.assertEqual(tr.mic_name, "MicB")
        self.assertEqual(tr.system_name, "SysB")
        tr.update_devices("MicB", "SysB")  # no-op must not raise

    def test_publish_devices_warns_without_system(self) -> None:
        from hud.session import LiveSession

        with tempfile.TemporaryDirectory() as tmp:
            session = LiveSession(HudConfig(), Path(tmp), lambda _m: None, "Mic", None)
            session._publish_devices()
            self.assertIn("microphone only",
                          session.state.meta["system_audio_warning"])


class WavTests(unittest.TestCase):
    def test_pcm_to_wav_roundtrip(self) -> None:
        import io
        import wave

        data = pcm_to_wav(tone(0.1))
        with wave.open(io.BytesIO(data), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.getsampwidth(), 2)


class RoutingFixTests(unittest.TestCase):
    def _topology(self, items):
        from hud import devices

        topo = devices.Topology()
        topo.devices = [devices.AudioDevice(**d) for d in items]
        for dev in topo.devices:
            if dev.default_input:
                topo.default_input = dev.name
            if dev.default_output:
                topo.default_output = dev.name
        return topo

    def _broken_topo(self) -> None:
        return self._topology([
            {"name": "BlackHole 2ch", "transport": "coreaudio_device_type_virtual",
             "input_channels": 2, "output_channels": 2},
            {"name": "External Headphones", "transport": "coreaudio_device_type_builtin",
             "output_channels": 2, "default_output": True},
            {"name": "MacBook Air Speakers", "transport": "coreaudio_device_type_builtin",
             "output_channels": 2},
        ])

    def test_needs_fix_when_real_output_is_default(self) -> None:
        from hud.routing_fix import MULTI_OUTPUT_NAME, needs_fix

        topo = self._broken_topo()
        self.assertTrue(needs_fix(topo))

        topo2 = self._topology([
            {"name": MULTI_OUTPUT_NAME, "transport": "coreaudio_device_type_aggregate",
             "input_channels": 2, "output_channels": 2, "default_output": True},
        ])
        self.assertFalse(needs_fix(topo2))

    def test_choose_physical_output_prefers_headphones(self) -> None:
        from hud.routing_fix import choose_physical_output

        topo = self._broken_topo()
        # Default output is the headphones; but if the misroute is a plain
        # loopback device, the ranked choice still finds a real output.
        self.assertEqual(choose_physical_output(topo).name, "External Headphones")

    def test_choose_physical_output_override(self) -> None:
        from hud.routing_fix import choose_physical_output

        topo = self._broken_topo()
        self.assertEqual(choose_physical_output(topo, "MacBook Air Speakers").name,
                         "MacBook Air Speakers")
        self.assertIsNone(choose_physical_output(topo, "Nonexistent Device"))

    def test_choose_physical_output_prefers_real_default(self) -> None:
        from hud.routing_fix import choose_physical_output

        topo = self._broken_topo()
        # Headphones are the default output; they must win the ranked choice.
        self.assertEqual(choose_physical_output(topo).name, "External Headphones")

    def test_fix_routing_requires_loopback(self) -> None:
        from hud import routing_fix

        topo = self._topology([
            {"name": "ZoomAudioDevice", "transport": "virtual",
             "input_channels": 2, "output_channels": 2},
        ])
        result = routing_fix.fix_routing(topo)
        self.assertFalse(result.ok)
        self.assertIn("brew install blackhole-2ch", result.message)
        self.assertIn("Click-by-click", result.message)

    def test_walkthrough_steps(self) -> None:
        from hud.routing_fix import walkthrough

        text = walkthrough("BlackHole 2ch", "External Headphones", "zoom-recorder Multi-Output")
        for step in ["Audio MIDI Setup", "Create Multi-Output Device",
                     "Drift Correction", "self-test", "Audio Out"]:
            self.assertIn(step, text)
        self.assertIn("BlackHole 2ch", text)
        self.assertIn("External Headphones", text)

    def test_real_outputs_filters_virtual_and_sorts(self) -> None:
        from hud.routing_fix import real_outputs

        topo = self._topology([
            {"name": "BlackHole 2ch", "transport": "virtual", "output_channels": 2},
            {"name": "CU34G2XP", "transport": "displayport", "output_channels": 2},
            {"name": "External Headphones", "transport": "builtin", "output_channels": 2},
            {"name": "MacBook Air Speakers", "transport": "builtin", "output_channels": 2},
        ])
        self.assertEqual([d.name for d in real_outputs(topo)],
                         ["External Headphones", "MacBook Air Speakers", "CU34G2XP"])

    def test_decide_action_matrix(self) -> None:
        from hud.routing_fix import MULTI_OUTPUT_NAME, decide_action

        state = {"multi_output_name": MULTI_OUTPUT_NAME,
                 "loopback_uid": "lb", "physical_uid": "ph"}
        self.assertEqual(decide_action(state, MULTI_OUTPUT_NAME, "lb", "ph"), "reuse")
        # stored pairing no longer matches the desired pairing
        self.assertEqual(decide_action(state, MULTI_OUTPUT_NAME, "lb", "other"), "rebuild")
        # device with our name missing / foreign name
        self.assertEqual(decide_action(state, None, "lb", "ph"), "rebuild")
        self.assertEqual(decide_action(state, "Someone Else's Aggregate", "lb", "ph"),
                         "rebuild")
        # corrupt/missing state
        self.assertEqual(decide_action({}, MULTI_OUTPUT_NAME, "lb", "ph"), "rebuild")

    def test_state_roundtrip(self) -> None:
        from hud.routing_fix import load_state, save_state

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertEqual(load_state(path), {})
            save_state("lb", "BlackHole 2ch", "ph", "External Headphones", path)
            self.assertEqual(load_state(path),
                             {"multi_output_name": "zoom-recorder Multi-Output",
                              "loopback_uid": "lb", "loopback_name": "BlackHole 2ch",
                              "physical_uid": "ph", "physical_name": "External Headphones"})
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_state(path), {})

    def test_choose_physical_output_prefers_stored_and_falls_back(self) -> None:
        from hud.routing_fix import choose_physical_output

        topo = self._broken_topo()
        # Stored preference wins over the ranked choice.
        self.assertEqual(
            choose_physical_output(topo, None,
                                   {"physical_name": "MacBook Air Speakers"}).name,
            "MacBook Air Speakers")
        # Stored device unplugged -> best available (preference not deleted).
        self.assertEqual(
            choose_physical_output(topo, None,
                                   {"physical_name": "USB Studio Monitors"}).name,
            "External Headphones")
        # Explicit override beats the stored preference.
        self.assertEqual(
            choose_physical_output(topo, "MacBook Air Speakers",
                                   {"physical_name": "External Headphones"}).name,
            "MacBook Air Speakers")

    def test_fix_routing_rebuilds_when_state_mismatches(self) -> None:
        from hud import routing_fix

        # BlackHole + a multi-output already exists as default output, but the
        # stored pairing names a device that is no longer present -> must
        # rebuild, not just re-select the stale device.
        topo = self._topology([
            {"name": "BlackHole 2ch", "transport": "coreaudio_device_type_virtual",
             "input_channels": 2, "output_channels": 2},
            {"name": "MacBook Air Speakers", "transport": "builtin",
             "output_channels": 2},
            {"name": routing_fix.MULTI_OUTPUT_NAME, "transport": "aggregate",
             "input_channels": 2, "output_channels": 2, "default_output": True},
        ])
        state = {"multi_output_name": routing_fix.MULTI_OUTPUT_NAME,
                 "loopback_uid": "BlackHole2ch_UID",
                 "physical_uid": "BuiltInHeadphoneOutputDevice",
                 "physical_name": "External Headphones"}

        calls = []

        class FakeCA:
            def devices(self):
                return [routing_fix.CaDevice(1, "BlackHole 2ch", "BlackHole2ch_UID"),
                        routing_fix.CaDevice(2, "MacBook Air Speakers",
                                             "BuiltInSpeakerDevice"),
                        routing_fix.CaDevice(3, routing_fix.MULTI_OUTPUT_NAME,
                                             "zoom-recorder-multi-output")]

            def destroy_aggregate(self, did):
                calls.append(("destroy", did))

            def create_multi_output(self, name, sub_uids, master_uid):
                calls.append(("create", name, tuple(sub_uids), master_uid))
                return 42

            def set_default_output(self, did):
                calls.append(("default", did))

        with mock.patch.object(routing_fix, "load_state", return_value=state), \
                mock.patch.object(routing_fix, "save_state"), \
                mock.patch.object(routing_fix, "backend", return_value=FakeCA()), \
                mock.patch.object(routing_fix, "_verified_multi_output",
                                  side_effect=lambda _ca, did, changed, detail:
                                  routing_fix.FixResult(True, changed, detail)):
            result = routing_fix.fix_routing(topo, assume_yes=True)

        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertIn(("destroy", 3), calls)
        self.assertIn(("create", routing_fix.MULTI_OUTPUT_NAME,
                       ("BlackHole2ch_UID", "BuiltInSpeakerDevice"),
                       "BuiltInSpeakerDevice"), calls)

    def test_fix_routing_reuse_when_state_matches(self) -> None:
        from hud import routing_fix

        topo = self._topology([
            {"name": "BlackHole 2ch", "transport": "coreaudio_device_type_virtual",
             "input_channels": 2, "output_channels": 2},
            {"name": "MacBook Air Speakers", "transport": "builtin",
             "output_channels": 2},
            {"name": routing_fix.MULTI_OUTPUT_NAME, "transport": "aggregate",
             "input_channels": 2, "output_channels": 2, "default_output": True},
        ])
        state = {"multi_output_name": routing_fix.MULTI_OUTPUT_NAME,
                 "loopback_uid": "BlackHole2ch_UID",
                 "physical_uid": "BuiltInSpeakerDevice",
                 "physical_name": "MacBook Air Speakers"}
        calls = []

        class FakeCA:
            def devices(self):
                return [routing_fix.CaDevice(1, "BlackHole 2ch", "BlackHole2ch_UID"),
                        routing_fix.CaDevice(2, "MacBook Air Speakers",
                                             "BuiltInSpeakerDevice"),
                        routing_fix.CaDevice(3, routing_fix.MULTI_OUTPUT_NAME,
                                             "zoom-recorder-multi-output")]

            def destroy_aggregate(self, did):
                calls.append(("destroy", did))

            def create_multi_output(self, *args):
                calls.append(("create",))
                return 42

            def set_default_output(self, did):
                calls.append(("default", did))

        with mock.patch.object(routing_fix, "load_state", return_value=state), \
                mock.patch.object(routing_fix, "save_state"), \
                mock.patch.object(routing_fix, "backend", return_value=FakeCA()), \
                mock.patch.object(routing_fix, "_verified_multi_output",
                                  side_effect=lambda _ca, did, changed, detail:
                                  routing_fix.FixResult(True, changed, detail)):
            result = routing_fix.fix_routing(topo, assume_yes=True)

        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertNotIn(("create", 42), calls)
        self.assertNotIn(("destroy", 3), calls)
        self.assertIn(("default", 3), calls)


class AudioMenuTests(unittest.TestCase):
    def test_specs_mark_current_pairing(self) -> None:
        from menubar import audio_menu_specs

        specs = audio_menu_specs(["MacBook Air Speakers", "External Headphones"],
                                 "External Headphones")
        self.assertEqual(specs[0], ("MacBook Air Speakers", "MacBook Air Speakers"))
        self.assertEqual(specs[1], ("✓ External Headphones", "External Headphones"))
        self.assertEqual(specs[-2], (None, None))  # separator
        self.assertEqual(specs[-1], ("Rebuild Routing", None))

    def test_populate_submenu_attaches_nsmenu(self) -> None:
        """Regression: rumps 0.4.0's MenuItem.menu assignment never calls
        setSubmenu_, so the parent rendered greyed out with no children."""
        from AppKit import NSMenuItem
        from menubar import populate_submenu

        class Fake:
            def __init__(self, title):
                self._menuitem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    title, None, "")

        parent = Fake("Audio Out")
        populate_submenu(parent, [Fake("Speakers"), None, Fake("Rebuild Routing")])
        menu = parent._menuitem.submenu()
        self.assertIsNotNone(menu)
        self.assertTrue(parent._menuitem.isEnabled())
        self.assertEqual(menu.numberOfItems(), 3)


class SystemTapTests(unittest.TestCase):
    def _args(self, capture="auto", system=None, no_system=False):
        import argparse

        return argparse.Namespace(system_capture=capture, system=system,
                                  no_system=no_system)

    def test_resolve_loopback_when_system_override(self) -> None:
        from zoom_record import resolve_system_capture

        self.assertEqual(resolve_system_capture(
            self._args(capture="auto", system="BlackHole 2ch")), "loopback")
        self.assertEqual(resolve_system_capture(self._args(capture="loopback")),
                         "loopback")

    def test_resolve_system_capture_is_loopback_by_default(self) -> None:
        import zoom_record as zr

        # Auto is loopback: the tap is strictly opt-in so a shared install
        # never touches the audio-capture permission path by surprise.
        self.assertEqual(zr.resolve_system_capture(self._args()), "loopback")
        self.assertEqual(zr.resolve_system_capture(
            self._args(capture="auto", system="BlackHole 2ch")), "loopback")
        self.assertEqual(zr.resolve_system_capture(self._args(capture="loopback")),
                         "loopback")
        self.assertEqual(zr.resolve_system_capture(self._args(capture="tap")), "tap")

    def test_usable_in_this_context_needs_terminal(self) -> None:
        import hud.system_tap as st

        with mock.patch.object(st, "available", return_value=True), \
                mock.patch.dict(os.environ, {"TERM_PROGRAM": "Apple_Terminal"}):
            self.assertTrue(st.usable_in_this_context())
        with mock.patch.object(st, "available", return_value=True), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(st.usable_in_this_context())
        with mock.patch.object(st, "available", return_value=False), \
                mock.patch.dict(os.environ, {"TERM_PROGRAM": "Apple_Terminal"}):
            self.assertFalse(st.usable_in_this_context())

    def test_capture_cmd_with_pcm_pipe(self) -> None:
        from zoom_record import build_capture_cmd

        class FakePCM:
            read_fd = 7

            def ffmpeg_args(self):
                return ["-thread_queue_size", "4096", "-f", "f32le",
                        "-ar", "48000", "-ac", "2", "-i", "pipe:7"]

            def name(self):
                return "System Tap (macOS)"

        cmd, pass_fds = build_capture_cmd(
            "MacBook Air Microphone", None, FakePCM(), Path("/tmp/s"), 60)
        self.assertIn("pipe:7", cmd)
        self.assertIn("-map", cmd)
        self.assertEqual(cmd[cmd.index("-map") + 1], "0:a")
        self.assertEqual(pass_fds, (7,))
        # mic + system device path must not request the pipe fd
        cmd2, fds2 = build_capture_cmd(
            "Mic", "BlackHole 2ch", None, Path("/tmp/s"), 60)
        self.assertIn(":BlackHole 2ch", cmd2)
        self.assertEqual(fds2, ())

    def test_tap_stall_detection_pure(self) -> None:
        from hud.system_tap import SystemTap

        tap = SystemTap()
        tap._started = True
        tap._last_bytes_at = time.time() - 10
        tap.bytes_total = 1000
        self.assertTrue(tap.stalled_after(5.0))
        tap._last_bytes_at = time.time()
        self.assertFalse(tap.stalled_after(5.0))
        # never-started tap never reports stalled
        tap2 = SystemTap()
        tap2.bytes_total = 0
        self.assertFalse(tap2.stalled_after(0.0))

    def test_tap_format_parsing(self) -> None:
        from hud.system_tap import _read_tap_format

        asbd = struct.pack("<dIIIIIIII", 48000.0, 0x6C70636D, 0x29, 8, 1, 8, 2, 32, 0)

        class FakeCA:
            def AudioObjectGetPropertyDataSize(self, obj, addr, q, qual, size):
                size._obj.value = len(asbd)  # CArgObject wraps the real buffer
                return 0

            def AudioObjectGetPropertyData(self, obj, addr, q, qual, n, buf):
                buf.raw = asbd  # buffer is passed by value (not byref)
                n._obj.value = len(asbd)
                return 0

        fmt = _read_tap_format(FakeCA(), 1)
        self.assertEqual(fmt.sample_rate, 48000)
        self.assertEqual(fmt.channels, 2)
        self.assertTrue(fmt.is_float)

    def test_tap_feature_detection(self) -> None:
        from hud.system_tap import available, _macos_version

        import platform as _platform
        with mock.patch.object(_platform_module(), "mac_ver", return_value=("15.7.8", "", "")):
            self.assertTrue(available())
        with mock.patch.object(_platform_module(), "mac_ver", return_value=("13.6.0", "", "")):
            self.assertFalse(available())

    def test_available_on_non_darwin(self) -> None:
        from hud.system_tap import available

        with mock.patch.object(_platform_module(), "system", return_value="Linux"):
            self.assertFalse(available())

    def test_hud_suppresses_loopback_advice_in_tap_mode(self) -> None:
        from hud.session import LiveSession
        from hud.system_tap import SOURCE_NAME
        from hud.config import HudConfig

        with tempfile.TemporaryDirectory() as tmp:
            session = LiveSession(HudConfig(), Path(tmp), lambda _m: None, "Mic", SOURCE_NAME)
            session._publish_devices()
            self.assertEqual(session.state.meta.get("system_audio_warning"), "")
            session.update_devices("Mic", "BlackHole 2ch")
            # loopback source still gets real advice when routing is wrong
            warning = session.state.meta.get("system_audio_warning")
            self.assertIsInstance(warning, str)


def _platform_module():
    import hud.system_tap as st
    return st.platform


class VolumeControlTests(unittest.TestCase):
    def _topo(self, default_output):
        from hud import devices

        topo = devices.Topology(default_output=default_output)
        topo.devices = [
            devices.AudioDevice(name="BlackHole 2ch", transport="virtual",
                                input_channels=2, output_channels=2),
            devices.AudioDevice(name="MacBook Air Speakers", transport="builtin",
                                output_channels=2),
        ]
        return topo

    def test_volume_math(self) -> None:
        from hud.routing_fix import clamp_percent, stepped_volume

        self.assertEqual(clamp_percent(-5), 0.0)
        self.assertEqual(clamp_percent(150), 100.0)
        self.assertEqual(stepped_volume(98, 5), 100.0)
        self.assertEqual(stepped_volume(2, -5), 0.0)
        self.assertEqual(stepped_volume(50, 5), 55.0)

    def test_volume_labels(self) -> None:
        from hud.routing_fix import format_volume_label, format_volume_title

        self.assertEqual(format_volume_title(44), "Volume 44%")
        self.assertEqual(format_volume_title(44, muted=True), "Volume (muted)")
        self.assertEqual(format_volume_label(44), "Volume: 44%")
        self.assertEqual(format_volume_label(44, muted=True), "Volume: muted")

    def test_is_loopback_active(self) -> None:
        from hud.routing_fix import MULTI_OUTPUT_NAME, is_loopback_active

        self.assertTrue(is_loopback_active(self._topo(MULTI_OUTPUT_NAME)))
        self.assertFalse(is_loopback_active(self._topo("MacBook Air Speakers")))

    def test_volume_target_prefers_default_when_not_loopback(self) -> None:
        from hud.routing_fix import CaDevice, _volume_target

        class FakeCA:
            def default_output_id(self):
                return 2

        ca_devices = [CaDevice(1, "BlackHole 2ch", "bh"),
                      CaDevice(2, "MacBook Air Speakers", "spk")]
        target = _volume_target(FakeCA(), ca_devices,
                                self._topo("MacBook Air Speakers"))
        self.assertEqual(target.name, "MacBook Air Speakers")

    def test_volume_target_uses_multi_output_member_in_loopback(self) -> None:
        from hud.routing_fix import MULTI_OUTPUT_NAME, CaDevice, _volume_target

        class FakeCA:
            def default_output_id(self):
                return 9  # the aggregate has no volume

        ca_devices = [CaDevice(1, "BlackHole 2ch", "bh"),
                      CaDevice(2, "MacBook Air Speakers", "spk")]
        target = _volume_target(FakeCA(), ca_devices,
                                self._topo(MULTI_OUTPUT_NAME),
                                physical_output="MacBook Air Speakers")
        self.assertEqual(target.name, "MacBook Air Speakers")


class HardeningTests(unittest.TestCase):
    def tearDown(self) -> None:
        from hud import llm
        import zoom_record
        llm.set_offline(False)
        zoom_record.set_notifications(True)

    def test_offline_blocks_remote_and_allows_loopback(self) -> None:
        from hud.llm import LLMClient, LLMError, set_offline

        set_offline(True)
        remote = LLMClient("https://api.example.invalid/v1", "k")
        with self.assertRaises(LLMError) as ctx:
            remote._request("/chat", b"{}", "application/json")
        self.assertIn("offline mode", str(ctx.exception))

        local = LLMClient("http://127.0.0.1:1/v1", None)
        try:
            local._request("/x", b"{}", "application/json")
        except LLMError as exc:
            # Nothing is listening on port 1, so a network error is expected;
            # what matters is that the offline guard did not block loopback.
            self.assertNotIn("offline mode", str(exc))

    def test_privacy_config_roundtrip(self) -> None:
        from hud.config import config_from_dict, config_to_dict

        cfg = config_from_dict({"privacy": {"offline": True, "notifications": False}})
        self.assertTrue(cfg.offline)
        self.assertFalse(cfg.notifications)
        out = config_to_dict(cfg)
        self.assertEqual(out["privacy"], {"offline": True, "notifications": False})

    def test_notifications_gated(self) -> None:
        import zoom_record

        with mock.patch("zoom_record.subprocess.run") as run:
            zoom_record.set_notifications(False)
            zoom_record.notify_user("hidden")
            run.assert_not_called()
            zoom_record.set_notifications(True)
            zoom_record.notify_user("shown")
            run.assert_called_once()

    def test_doctor_reports_and_fails_on_critical(self) -> None:
        from hud import doctor

        good = doctor.Check("thing", True, "fine")
        bad = doctor.Check("thing", False, "broken", "do the fix")
        with mock.patch.object(doctor, "check_macos", return_value=good), \
                mock.patch.object(doctor, "check_python", return_value=good), \
                mock.patch.object(doctor, "check_tools", return_value=[good]), \
                mock.patch.object(doctor, "check_rumps", return_value=good), \
                mock.patch.object(doctor, "check_blackhole", return_value=good), \
                mock.patch.object(doctor, "check_routing", return_value=good), \
                mock.patch.object(doctor, "check_output_volume", return_value=good), \
                mock.patch.object(doctor, "check_microphone", return_value=good), \
                mock.patch.object(doctor, "check_tap", return_value=good):
            self.assertTrue(doctor.run_doctor())
        with mock.patch.object(doctor, "check_macos", return_value=bad), \
                mock.patch.object(doctor, "check_python", return_value=good), \
                mock.patch.object(doctor, "check_tools", return_value=[]), \
                mock.patch.object(doctor, "check_rumps", return_value=good), \
                mock.patch.object(doctor, "check_blackhole", return_value=good), \
                mock.patch.object(doctor, "check_routing", return_value=good), \
                mock.patch.object(doctor, "check_output_volume", return_value=good), \
                mock.patch.object(doctor, "check_microphone", return_value=good), \
                mock.patch.object(doctor, "check_tap", return_value=good):
            self.assertFalse(doctor.run_doctor())

    def test_doctor_non_critical_failure_still_ok(self) -> None:
        from hud import doctor

        good = doctor.Check("thing", True, "fine")
        warn = doctor.Check("thing", False, "missing", "optional", critical=False)
        with mock.patch.object(doctor, "check_macos", return_value=good), \
                mock.patch.object(doctor, "check_python", return_value=good), \
                mock.patch.object(doctor, "check_tools", return_value=[good]), \
                mock.patch.object(doctor, "check_rumps", return_value=warn), \
                mock.patch.object(doctor, "check_blackhole", return_value=good), \
                mock.patch.object(doctor, "check_routing", return_value=good), \
                mock.patch.object(doctor, "check_output_volume", return_value=good), \
                mock.patch.object(doctor, "check_microphone", return_value=good), \
                mock.patch.object(doctor, "check_tap", return_value=good):
            self.assertTrue(doctor.run_doctor())


class RecordingModeTests(unittest.TestCase):
    def test_build_capture_cmd_shapes(self) -> None:
        from zoom_record import build_capture_cmd

        class FakePCM:
            read_fd = 9

            def ffmpeg_args(self):
                return ["-f", "f32le", "-ar", "48000", "-ac", "2", "-i", "pipe:9"]

        both, fds = build_capture_cmd("Mic", "BlackHole 2ch", None, Path("/tmp/s"), 60)
        self.assertIn(":Mic", both)
        self.assertIn(":BlackHole 2ch", both)
        self.assertEqual(both.count("-map"), 2)
        self.assertEqual(fds, ())

        mic_only, _ = build_capture_cmd("Mic", None, None, Path("/tmp/s"), 60)
        self.assertIn(":Mic", mic_only)
        self.assertEqual(mic_only.count("-map"), 1)
        self.assertIn("seg_%05d_mic.wav", mic_only[-1])

        sys_only, _ = build_capture_cmd(None, "BlackHole 2ch", None, Path("/tmp/s"), 60)
        self.assertNotIn(":Mic", sys_only)
        self.assertEqual(sys_only.count("-map"), 1)
        self.assertIn("seg_%05d_sys.wav", sys_only[-1])

        sys_pcm, fds = build_capture_cmd(None, None, FakePCM(), Path("/tmp/s"), 60)
        self.assertIn("pipe:9", sys_pcm)
        self.assertEqual(fds, (9,))
        self.assertEqual(sys_pcm.count("-map"), 1)
        self.assertIn("seg_%05d_sys.wav", sys_pcm[-1])

    def test_resolve_recording_mode(self) -> None:
        from zoom_record import resolve_recording_mode

        self.assertEqual(resolve_recording_mode(True, False, "both"), "system")
        self.assertEqual(resolve_recording_mode(False, True, "both"), "mic")
        self.assertEqual(resolve_recording_mode(False, False, "system"), "system")
        self.assertEqual(resolve_recording_mode(False, False, "bogus"), "both")

    def test_recorder_defaults_roundtrip(self) -> None:
        from hud.config import (_defaults, config_from_dict, config_to_dict,
                                recorder_defaults)

        cfg = config_from_dict(_defaults())
        out = config_to_dict(cfg)
        self.assertEqual(out["recorder"]["mode"], "both")
        self.assertFalse(out["onboarded"])

        rec = recorder_defaults({"recorder": {"mode": "system",
                                              "basedir": "~/Recs",
                                              "transcription_model": "~/m.bin"}})
        self.assertEqual(rec.mode, "system")
        self.assertFalse(rec.record_mic())
        self.assertTrue(rec.record_system())

        # invalid mode falls back to both
        self.assertEqual(recorder_defaults({"recorder": {"mode": "nope"}}).mode, "both")


class RecordingsTests(unittest.TestCase):
    def test_list_recordings(self) -> None:
        from hud.recordings import format_duration, list_recordings

        self.assertEqual(format_duration(None), "--:--")
        self.assertEqual(format_duration(65), "1:05")
        self.assertEqual(format_duration(3725), "1:02:05")

        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026-09-18"
            session = day / "17-27-11_ab12cd34"
            named = day / "16-20-10_enrollment-planning_cd34ef56"
            (session / "derived").mkdir(parents=True)
            (named / "derived").mkdir(parents=True)
            (session / "recording_mic.wav").write_bytes(b"RIFF")
            (session / "recording_sys.wav").write_bytes(b"RIFF")
            (session / "transcript.txt").write_text("hello", encoding="utf-8")
            (session / "derived" / "live_summary.md").write_text("# s", encoding="utf-8")
            (named / "session.json").write_text(json.dumps({
                "started_at": "2026-09-18T16:20:10",
                "title": "Enrollment planning",
                "participants": ["Dana", "Client"],
            }), encoding="utf-8")
            (day / "not-a-session").mkdir()
            items = list_recordings(tmp, probe=False)
            self.assertEqual(len(items), 2)
            rec = next(item for item in items if item.name == session.name)
            self.assertEqual(rec.started, "2026-09-18 17:27:11")
            self.assertTrue(rec.has_mic)
            self.assertTrue(rec.has_system)
            self.assertTrue(rec.transcript)
            self.assertTrue(rec.summary)
            self.assertEqual(rec.as_dict()["duration"], "--:--")  # no probe
            named_rec = next(item for item in items if item.name == named.name)
            self.assertEqual(named_rec.title, "Enrollment planning")
            self.assertEqual(named_rec.participants, ["Dana", "Client"])
            self.assertEqual(named_rec.started, "2026-09-18T16:20:10")


class ControlCenterTests(unittest.TestCase):
    def _server(self, tmp):
        from hud.control import ControlApp

        app = ControlApp(port=0, config_path=Path(tmp) / "config.json",
                         markers=False, idle_timeout=0, open_browser=False)
        port = app.start()
        self.addCleanup(app.stop)
        return app, port

    def test_health_header_and_config_roundtrip(self) -> None:
        import urllib.request
        from hud.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            app, port = self._server(tmp)
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/health?token={}".format(port, app.token),
                    timeout=5) as resp:
                self.assertEqual(resp.headers.get("Connection"), "close")
                self.assertTrue(json.loads(resp.read())["ok"])
            body = json.dumps({"config": {"recorder": {"mode": "mic"},
                                          "privacy": {"offline": True}}}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:{}/api/config?token={}".format(port, app.token),
                method="POST", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertTrue(json.loads(resp.read())["ok"])
            cfg = load_config(Path(tmp) / "config.json")
            self.assertEqual(cfg.recorder.mode, "mic")
            self.assertTrue(cfg.offline)

    def test_login_agent_uses_nondestructive_modes(self) -> None:
        """Regression: a full install would bootout the agent and kill the
        menu bar (and this Control Center) mid-request."""
        import urllib.request

        with tempfile.TemporaryDirectory() as tmp:
            app, port = self._server(tmp)
            calls = []

            class FakeProc:
                returncode = 0
                stdout = "ok"
                stderr = ""

            def fake_run(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return FakeProc()

            with mock.patch("hud.control.subprocess.run", side_effect=fake_run):
                for enabled in (False, True):
                    req = urllib.request.Request(
                        "http://127.0.0.1:{}/api/login-agent?token={}".format(port, app.token),
                        method="POST",
                        data=json.dumps({"enabled": enabled}).encode(),
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        self.assertTrue(json.loads(resp.read())["ok"])
            self.assertIn("--disable-autostart", calls[0][0])
            self.assertIn("--enable-autostart", calls[1][0])
            self.assertTrue(all(kwargs.get("start_new_session")
                                for _cmd, kwargs in calls))

    def test_status_reports_login_agent_state(self) -> None:
        import urllib.request

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "Library" / "LaunchAgents").mkdir(parents=True)
            app, port = self._server(tmp)
            url = "http://127.0.0.1:{}/api/status?token={}".format(port, app.token)
            with mock.patch.object(Path, "home", return_value=home):
                with urllib.request.urlopen(url, timeout=5) as resp:
                    status = json.loads(resp.read())
                    self.assertFalse(status["login_agent"])
                    # the Setup/Settings controls read these back on load
                    for key in ("mode", "paired_output", "stt_backend",
                                "answers_enabled", "transcription_model",
                                "diarization", "api_keys_set"):
                        self.assertIn(key, status)
                # a saved key is reported so the UI can tell the user
                (Path(tmp) / "config.json").write_text(
                    json.dumps({"api_keys": {"groq": "test-key"}}), encoding="utf-8")
                with urllib.request.urlopen(url, timeout=5) as resp:
                    keys = json.loads(resp.read())["api_keys_set"]
                    self.assertTrue(keys["groq"])
                    self.assertFalse(keys["openai"])
                (home / "Library" / "LaunchAgents" /
                 "com.zoomrecorder.menubar.plist").write_text("<plist/>", encoding="utf-8")
                with urllib.request.urlopen(url, timeout=5) as resp:
                    self.assertTrue(json.loads(resp.read())["login_agent"])

    def test_status_and_checks_are_split(self) -> None:
        import urllib.request

        with tempfile.TemporaryDirectory() as tmp:
            app, port = self._server(tmp)
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/api/status?token={}".format(port, app.token),
                    timeout=5) as resp:
                status = json.loads(resp.read())
            self.assertNotIn("checks", status)          # no mic probe on the poll
            with urllib.request.urlopen(
                    "http://127.0.0.1:{}/api/checks?token={}".format(port, app.token),
                    timeout=30) as resp:
                checks = json.loads(resp.read())
            self.assertTrue(checks["ok"])
            self.assertTrue(checks["checks"])

    def test_test_recording_refused_while_recording(self) -> None:
        import urllib.request

        with tempfile.TemporaryDirectory() as tmp:
            app, port = self._server(tmp)
            with mock.patch("hud.control.recording_active", return_value=True):
                req = urllib.request.Request(
                    "http://127.0.0.1:{}/api/test-recording?token={}".format(port, app.token),
                    method="POST", data=b'{"seconds": 1}',
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    out = json.loads(resp.read())
            self.assertFalse(out["ok"])
            self.assertIn("already in progress", out["error"])

    def test_autostart_script_dry_runs(self) -> None:
        import platform
        import subprocess

        if platform.system() != "Darwin":
            self.skipTest("macOS only")
        script = Path(__file__).resolve().parent.parent / "install-launch-agent.sh"
        for flag in ("--enable-autostart", "--disable-autostart"):
            proc = subprocess.run([str(script), flag, "--dry-run"],
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("dry-run", proc.stdout)


class PairingTests(unittest.TestCase):
    def _topo(self):
        from hud import devices

        topo = devices.Topology(default_output="MacBook Air Speakers")
        topo.devices = [
            devices.AudioDevice(name="BlackHole 2ch", transport="virtual",
                                input_channels=2, output_channels=2),
            devices.AudioDevice(name="MacBook Air Speakers", transport="builtin",
                                output_channels=2),
        ]
        return topo

    def test_pair_output_does_not_touch_default_output(self) -> None:
        """Regression: choosing an output must not activate the Multi-Output
        Device (that undid the auto-restore-to-real-device behavior)."""
        from hud import routing_fix

        calls = []

        class FakeCA:
            def devices(self):
                return [routing_fix.CaDevice(1, "BlackHole 2ch", "bh"),
                        routing_fix.CaDevice(2, "MacBook Air Speakers", "spk")]

            def create_multi_output(self, name, subs, master):
                calls.append(("create", name, tuple(subs), master))
                return 42

            def destroy_aggregate(self, did):
                calls.append(("destroy", did))

            def set_default_output(self, did):
                calls.append(("default", did))

        with mock.patch.object(routing_fix, "backend", return_value=FakeCA()), \
                mock.patch.object(routing_fix, "load_state", return_value={}), \
                mock.patch.object(routing_fix, "save_state") as save:
            result = routing_fix.pair_output("MacBook Air Speakers", topo=self._topo())

        self.assertTrue(result.ok)
        self.assertIn(("create", routing_fix.MULTI_OUTPUT_NAME,
                       ("bh", "spk"), "spk"), calls)
        self.assertNotIn(("default", 42), calls)
        self.assertFalse(any(c[0] == "default" for c in calls))
        save.assert_called_once()

    def test_pair_output_rebuilds_when_stored_pairing_changed(self) -> None:
        from hud import routing_fix

        calls = []

        class FakeCA:
            def devices(self):
                return [routing_fix.CaDevice(1, "BlackHole 2ch", "bh"),
                        routing_fix.CaDevice(2, "MacBook Air Speakers", "spk"),
                        routing_fix.CaDevice(3, routing_fix.MULTI_OUTPUT_NAME, "agg")]

            def create_multi_output(self, *args):
                calls.append("create")
                return 42

            def destroy_aggregate(self, did):
                calls.append(("destroy", did))

            def set_default_output(self, did):
                calls.append(("default", did))

        state = {"multi_output_name": routing_fix.MULTI_OUTPUT_NAME,
                 "loopback_uid": "bh", "physical_uid": "old-device"}
        with mock.patch.object(routing_fix, "backend", return_value=FakeCA()), \
                mock.patch.object(routing_fix, "load_state", return_value=state), \
                mock.patch.object(routing_fix, "save_state"):
            result = routing_fix.pair_output("MacBook Air Speakers", topo=self._topo())
        self.assertTrue(result.ok)
        self.assertTrue(result.changed)
        self.assertIn(("destroy", 3), calls)
        self.assertIn("create", calls)
        self.assertFalse(any(c == "default" or (isinstance(c, tuple) and c[0] == "default")
                             for c in calls))

    def test_doctor_routing_is_capability_aware(self) -> None:
        from hud import doctor, devices

        def topo(default_name, aggregate=False):
            t = devices.Topology(default_output=default_name)
            t.devices = [
                devices.AudioDevice(name="BlackHole 2ch", transport="virtual",
                                    input_channels=2, output_channels=2),
                devices.AudioDevice(name="MacBook Air Speakers", transport="builtin",
                                    output_channels=2),
                devices.AudioDevice(name=default_name, transport="aggregate",
                                    input_channels=2, output_channels=2),
            ]
            return t

        with mock.patch.object(doctor, "read_system_profiler",
                               return_value=topo("MacBook Air Speakers")):
            self.assertTrue(doctor.check_routing().ok)          # real device = normal
        with mock.patch.object(doctor, "read_system_profiler",
                               return_value=topo("BlackHole 2ch")):
            check = doctor.check_routing()
            self.assertFalse(check.ok)                          # bare loopback = problem
            self.assertFalse(check.critical)
        with mock.patch.object(doctor, "read_system_profiler",
                               return_value=topo("zoom-recorder Multi-Output", True)):
            self.assertTrue(doctor.check_routing().ok)          # active setup


class AuditHardeningTests(unittest.TestCase):
    def _server(self, tmp):
        from hud.control import ControlApp

        app = ControlApp(port=0, config_path=Path(tmp) / "config.json",
                         markers=False, idle_timeout=0, open_browser=False)
        app.start()
        self.addCleanup(app.stop)
        return app, app.port

    def _post(self, port, token, path, body):
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:{}/{}?token={}".format(port, path, token),
            method="POST", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def test_terminal_commands_are_whitelisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, port = self._server(tmp)
            bad = self._post(port, app.token, "api/fix",
                             {"action": "open-terminal", "key": "rm -rf /"})
            self.assertFalse(bad["ok"])
            good = self._post(port, app.token, "api/fix",
                             {"action": "open-terminal", "key": "install-blackhole"})
            self.assertTrue(good["ok"])
            self.assertEqual(good["command"], "brew install blackhole-2ch")
            setup = self._post(port, app.token, "api/fix",
                               {"action": "open-terminal", "key": "setup-diarization"})
            self.assertTrue(setup["ok"])
            self.assertIn("install-diarization", setup["command"])

    def test_download_model_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, port = self._server(tmp)
            out = self._post(port, app.token, "api/fix",
                             {"action": "download-model", "model": "../../evil"})
            self.assertFalse(out["ok"])

    def test_turbo_model_is_an_allowed_download(self) -> None:
        from hud.control import WHISPER_MODELS

        self.assertIn("ggml-large-v3-turbo-q5_0.bin", WHISPER_MODELS)
        self.assertEqual(WHISPER_MODELS["ggml-large-v3-turbo-q5_0.bin"],
                         "e050f7970618a659205450ad97eb95a18d69c9ee")

    def test_open_is_restricted_to_recordings_and_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, port = self._server(tmp)
            outside = self._post(port, app.token, "api/open",
                                 {"path": str(Path.home() / ".ssh")})
            self.assertFalse(outside["ok"])
            unknown_doc = self._post(port, app.token, "api/open", {"doc": "passwd"})
            self.assertFalse(unknown_doc["ok"])
            good_doc = self._post(port, app.token, "api/open", {"doc": "SECURITY.md"})
            self.assertTrue(good_doc["ok"])

    def test_recorder_lock(self) -> None:
        from zoom_record import (acquire_recorder_lock, recorder_lock_path,
                                 recorder_running, release_recorder_lock)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertFalse(recorder_running(base))
            self.assertTrue(acquire_recorder_lock(base))
            self.assertTrue(recorder_running(base))          # our own live pid
            release_recorder_lock(base)
            self.assertFalse(recorder_running(base))
            # a stale pid is cleaned up
            recorder_lock_path(base).write_text("999999", encoding="utf-8")
            self.assertFalse(recorder_running(base))

    def test_tcc_protected(self) -> None:
        from hud.config import tcc_protected

        home = Path.home()
        self.assertTrue(tcc_protected(home / "Documents" / "ZoomRecordings"))
        self.assertTrue(tcc_protected(home / "Downloads"))
        self.assertFalse(tcc_protected(home / "ZoomRecordings"))

    def test_recordings_duration_cache(self) -> None:
        from hud import recordings

        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026-09-18"
            session = day / "10-00-00_abcd1234"
            (session / "derived").mkdir(parents=True)
            mixed = session / "derived" / "recording_mixed.wav"
            mixed.write_bytes(b"RIFF")
            # seed the cache with this file's mtime
            mtime = mixed.stat().st_mtime
            (Path(tmp) / ".recordings_index.json").write_text(json.dumps({
                str(session): {"mtime": mtime, "duration": 42.0}}), encoding="utf-8")
            with mock.patch.object(recordings, "_probe_duration") as probe:
                items = recordings.list_recordings(tmp)
            probe.assert_not_called()
            self.assertEqual(items[0].duration_s, 42.0)


if __name__ == "__main__":
    unittest.main()
