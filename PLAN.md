# Hardening plan (post-incident)

> **Status (2026-09-17):** all phases below are implemented, committed, and
> annotated-tagged — `v1.0-core`, `v1.1-hud`, `v1.2-settings`, `v1.3-grounding`,
> `v1.4-stt-hallucination`, `v1.5-routing`. 108 unit tests pass
> (`python3 -m unittest discover -s tests`). This file is the design history and
> rationale; [`README.md`](README.md) is the user-facing reference.

Context: on 2026-09-16, a well-intentioned but reckless move (running a third-party
audio "enhancer" with its output directory pointed at the *same* folder as the
source file) silently zeroed out the only copy of a 32-minute recording. No
backups existed. The raw 5-minute capture segments had already been deleted by
the recorder's own cleanup step, hours earlier, per its normal (successful)
operation. Net result: an interview's audio is permanently gone.

None of that was the recorder's fault directly, but the recorder made total
loss *possible* by not keeping a second copy of anything, ever. This plan
closes that gap first, then addresses the other asks (device-change survival,
self-checking, easy activation).

Decisions already made (per Odin, 2026-09-16):
- **Activation**: one-keystroke manual start/stop. No background daemon that
  watches for calls and auto-arms itself — a persistent process with mic
  access and silent file writes is exactly the shape of thing Falcon (or any
  EDR) is tuned to flag, and it's a consent risk if it ever records a call it
  shouldn't. Simpler is safer here.
- **Versioning**: immutable originals + a checksum manifest. Not git-annex,
  not restic/Borg — just: write once, chmod to read-only, log a hash. Lowest
  complexity that still makes today's failure mode structurally impossible.

## Where the current code already stands

The `zoom_record.py` rewrite already covers a surprising amount of this well:

- Segment-based recording (crash only costs the current segment).
- Automatic mic selection, ranked by quality, preferring whichever device is
  actually delivering signal.
- Continuous health probing of the **active** mic every `--chunk-seconds`,
  with automatic failover to another device after `--fail-threshold`
  consecutive silent checks — this already handles "the input changed mid-call."
- Periodic probing of **inactive** candidates too, so failover has fresh data
  to decide from.
- System/loopback input is also monitored and re-selected if it drops.
- `capture.log` records every probe, every switch, every decision.
- `--self-test` verifies the loopback path end-to-end before you ever rely on it.

So "survive changing inputs/outputs mid-call" and "self-check that audio is
actually being captured" are largely **already solved** by the rewrite. What's
missing is what happens *after* recording stops, and a couple of structural
gaps that made today's incident possible.

## Gap 1 (critical): segments get deleted, originals aren't protected

`main()`'s `finally` block does:
```python
shutil.rmtree(workdir, ignore_errors=True)
```
right after merging — every single run, success or not. And nothing ever
stops another program (or a future version of this script, or a typo) from
opening `recording.wav` for writing again.

**Fix:**
1. Never delete segments. After a successful, *verified* merge (see Gap 2),
   move — don't delete — the segment directory into
   `~/ZoomRecordings/<date>/.segments/`. Disk is cheap; a lost interview isn't.
   Segments are also useful because merge is a lossless `-c copy` concat —
   they're a bit-exact fallback if the merge itself is ever bad.
2. Immediately after a verified merge, `os.chmod(merged, 0o444)`. Read-only,
   before any transcription or downstream tool ever touches it.
3. Every tool downstream (transcription, enhancement, whatever comes next)
   must be pointed at a **copy** in a `derived/` subfolder, never the original
   path. Cheap guard: a small wrapper that refuses to run if the requested
   output path resolves inside the dated recording folder itself outside
   `derived/`.

## Gap 2: nothing checks the recording is actually good before trusting it

Today's `transcript.txt` silently degrading into `[Pause]` for 90% of a file
was itself a warning sign that sat unnoticed for weeks. The recorder should
catch that itself, immediately.

**Fix — add a `verify_recording()` pass right after merge, before archiving
segments or chmod'ing:**
- Duration sanity check: merged duration ≈ sum of segment durations (catches
  a broken concat).
- Full-file `volumedetect` + a coarse silence scan (`silencedetect` at a
  couple of thresholds): compute `% of file with detectable signal`.
- Write a one-line summary to `capture.log` and print it on exit, e.g.:
  `Coverage: 68% of recording has signal above -60dB. 3 stretches (>60s) below threshold — see capture.log.`
- If coverage is suspiciously low (say <50%), this is exactly the kind of
  thing that should be **loud**, not a thing you discover during transcription
  three weeks later — flash a terminal bell / macOS notification.

## Gap 3: mic and system audio are pre-mixed — should be separate tracks

The current pipeline does `amix=inputs=2` immediately, producing one blended
mono-ish signal. That decision is exactly what made today's recovery effort so
hard: once two voices are summed together, you can't boost one speaker without
boosting the noise floor under the *other* one identically — there's no way to
separately gain, EQ, or denoise "my mic" vs "their voice through the system
loopback" after the fact.

**Fix:** write mic and system to two channels of a stereo file instead of
mixing to mono (`-filter_complex "[0:a]pan=stereo|c0=c0|c1=0[m];[1:a]pan=stereo|c0=0|c1=c0[s];[m][s]amix=inputs=2:duration=longest[a]"`
or simpler: just don't mix at all — write **two mono files per segment**
(`seg_00000_mic.wav`, `seg_00000_sys.wav`) and merge each track independently.
Two mono files is simpler to reason about and means any future
enhancement/leveling pass can be applied per-source instead of on an
already-blended signal. Downstream transcription can still mix them for a
single transcript pass, but the untouched dual tracks stay archived.

## Gap 4: no versioning / manifest

**Fix — dead simple, per the "immutable + manifest" decision:**
- On successful verify: append one line to `~/ZoomRecordings/manifest.jsonl`:
  `{"date": "...", "path": "...", "sha256": "...", "duration_s": ..., "coverage_pct": ..., "segments": N}`
- That's it. No git, no LFS, no backup daemon. A flat, append-only, human-
  readable audit log that answers "does this file still match what was
  originally recorded" with one `sha256sum` check, forever.
- Optional, cheap extra: since the manifest itself is tiny text, *that* can
  live in an actual git repo (`~/ZoomRecordings/.git`, manifest-only, `.gitignore`
  the actual audio) — free history of "what was recorded when," basically zero
  cost since it's never more than a few KB.

## Gap 5: one-keystroke activation

No background watcher (per the decision above). Two reasonable options,
roughly equal effort:

- **A global hotkey via Hammerspoon** (or `skhd`) bound to a small shell
  function: press once to start (spawns the recorder script detached with a
  pidfile), press again to stop (sends SIGINT to the pidfile, which the
  existing `trap`/`signal.signal` handlers already turn into a clean merge).
  No persistent process exists except while actively recording.
- **A tiny menu-bar toggle** using `rumps` (a ~50-line Python menu-bar app):
  shows a mic icon, click to start/stop, turns red while recording so it's
  never ambiguous whether it's on — and is a normal, visible menu-bar app the
  whole time, not a hidden background service, which matters for the
  Falcon/policy question.

Either is fine; the menu-bar option is slightly friendlier (visible state,
no memorizing a key combo) and doubles as the "is this actually recording
right now" indicator you'd otherwise have to check a log for.

## Suggested order of work

1. **Now, before anything else touches this script again:** Gap 1 (stop
   deleting segments, chmod originals read-only) and Gap 2 (verify + coverage
   report). This is the direct fix for what happened today and is a small,
   contained change.
2. Gap 4 (manifest) — trivial once Gap 2's verify step exists, since it's
   computing most of the needed data already.
3. Gap 3 (separate mic/system tracks) — slightly bigger change to the ffmpeg
   command construction, worth doing before the next important recording.
4. Gap 5 (hotkey or menu-bar launcher) — independent of the others, do
   whenever it's convenient.

Items 1–2 are the ones I'd actually implement first; everything else is
valuable but not "prevents a repeat of today" critical.

---

# Phase 2: Live transcript + AI answer HUD

Goal: a small two-column window during recording — live transcript on the left,
generative bullet answers/talking points on the right, grounded in both the
conversation and a background database of `.md` files. Opt-in via `--live`.

## Design decisions

- **Isolation above all.** The HUD is a separate ffmpeg tap plus separate
  threads. It never touches the capture path, the originals, or verification.
  If it fails to start (or crashes), recording proceeds exactly as before.
  Implemented under `hud/`; `zoom_record.py` only calls `LiveSession.start()` /
  `.stop()`.
- **Local web UI, not a native window.** The recorder's main thread is already
  owned by the monitor loop / rumps. A `127.0.0.1` HTTP + SSE page avoids any
  GUI-thread contention and gives the two-column layout for free.
- **Pluggable providers.** Groq is the default for both STT and answers
  (fastest + cheapest, usable free tier). OpenRouter, OpenAI and Ollama are
  wired in through the same OpenAI-compatible client; `local` STT uses
  whisper.cpp. The HUD header always shows what is leaving the machine.
- **Budget-aware answers.** Detected questions use the stronger model; rolling
  talking points use the cheap, high-quota model and are dropped first when the
  daily token budget runs low. This is what makes an aggressive (~35 s) cadence
  viable on a free tier.
- **KB is local.** Embeddings (`sentence-transformers`) are computed on-device
  and cached; only the handful of retrieved snippets join the prompt.
- **Partial diarization via channels.** Mic and system/loopback are separate
  captures, so each is transcribed independently and labelled (`--self-name` /
  `--remote-name`, default "You" / "Others"). Exact for two parties with no
  diarization model; multiple remote speakers share the loopback label.
  `--no-speaker-labels` restores the single mixed stream.

## Consent / privacy note

This is the first feature that can send data off the machine. With a remote STT
backend the **audio** leaves; with answers enabled the **transcript text** (and
relevant snippets from the user's own `.md` files) is sent to the answer
provider. Both are opt-in, both are surfaced live in the HUD, and
`--live-no-answers` (or a local STT + Ollama combination) keeps everything
on-device. The no-background-daemon decision from Phase 1 still holds: the HUD
exists only while a recording is in progress.

## Order of work

1. `hud/` scaffolding: config, OpenAI-compatible client, event state, budget.
2. HTTP/SSE server + two-column page.
3. Isolated live tap + chunked STT.
4. Local markdown knowledge base + embeddings.
5. Answer engine (question detection, rolling points, provider fallback).
6. Recorder/menu-bar integration + docs.
7. Tests, plus a check that `--live` off is byte-identical to before.

All of the above is implemented; see `hud/` and `tests/test_hud.py`.

Menu bar: the HUD gets its own distinct options (`Start with Live HUD`,
`Open Live HUD…`) rather than reusing the plain recording toggle, with a
three-state icon (🎙 idle / 🔴 recording / 🧠 recording + live window).
The state logic is kept in `hud/menu_state.py` so it is testable without
importing `rumps`. The icon is deliberately a single glyph: on notched Macs
macOS hides menu-bar items that don't fit, so the README documents the
Command-drag and Ice (menu-bar manager) remedies.

Settings GUI: `hud/settings.py` + `hud/static/settings.html` serve a loopback
form (launched via `./settings.py` or the menu bar) that edits the config with
live model dropdowns, connection tests, and a KB folder picker. It uses a
one-time token, never exposes stored keys, writes atomically with a backup and
idle-shuts-down, so it fits the no-persistent-daemon constraint.

## Phase 2.1: context-aware Q&A, talking-point pane, KB enablement

Follow-up work driven by real use of the HUD:

- **Question detection now spans the conversation, not one chunk.** The engine
  keeps structured, speaker-labelled turns and scans a rolling window
  (`answers.question_lookback_seconds`, default 90 s), so a question split
  across STT chunks is still caught. A question is answered once per new turn,
  with retry-after-cooldown on failure instead of a per-tick hammer.
- **Real context in the prompt.** The window is rendered as timestamped
  `[hh:mm:ss] Speaker: …` lines with a larger char budget
  (`answers.context_max_chars`) and the turns immediately preceding the
  question, plus an instruction to resolve references (`it`, `that`, `the other
  one`). Fixes answers that only addressed the literal sentence containing `?`.
- **Hybrid follow-up resolution.** Short / connector-led / pronoun-heavy
  questions (`is_ambiguous_question`) get one cheap rewrite call into a
  self-contained question before the strong model answers. Toggle with
  `answers.question_rewrite`.
- **Talking points are their own stream and pane.** Rolling refreshes no longer
  emit Q&A cards. They append only *new* bullets (server-side normalized +
  token-overlap dedupe) to a canonical list that survives the event-ring
  eviction and page reloads. The HUD's right column is now a stacked
  **Talking points** list over a **Q&A** column.
- **KB is pluggable and on by default when a backend exists.** Embeddings can
  come from `sentence-transformers` (local), Ollama (local) or OpenAI (remote),
  selected by `kb.embed_backend: auto`; vectors are stored/compared in pure
  Python, so the heavy `torch`/`numpy` stack is no longer required. Retrieval
  queries on the question plus its context and uses a configurable score floor
  (`kb.min_score`). The settings GUI and `--kb-embed-backend` expose the choice.

All of the above is covered by `tests/test_hud.py` (`python3 -m unittest
discover -s tests`).

## Phase 2.2: latency, live-robustness and usefulness pass

Second round of improvements (all except remote multi-speaker diarization,
source deep-links and a cost meter):

- **Connection reuse.** `LLMClient` now keeps a thread-local `http.client`
  keep-alive connection per provider instead of a fresh TLS handshake per STT
  chunk/answer, retrying once on a dropped socket.
- **STT no longer blocks the audio tap.** Each source has a bounded queue and a
  dedicated worker; if transcription falls behind, the oldest queued chunk is
  dropped and a `stt_lag_seconds` metric is published to the HUD. Chunking is
  phrase-aware (emits on a natural pause after ~6 s, capped at
  `stt.chunk_seconds`, now 10).
- **Better transcription.** Chunks are seeded with the previous text plus a
  `stt.glossary` of names/acronyms, and cross-chunk word de-duplication is now
  fuzzy (`difflib`) so ASR variants like *roll out*/*rollout* stop duplicating
  or dropping words.
- **Detection is decoupled from generation.** A single priority worker answers
  *all* newly detected questions in order (not just the latest) and coalesces
  talking-point refreshes; the KB/embedder is built on a side thread so the
  first answers aren't blocked. The transcript window and preceding turns are
  snapshotted at enqueue time so a slow call can't drift.
- **Answer quality.** Prompts now include the last few Q&A pairs, instruct the
  model to resolve references, ground factual claims in notes and admit
  uncertainty. Only remote questions are answered by default
  (`answers.answer_self_questions`); rhetorical questions are skipped; the model
  flags follow-ups (`parent_id`) so the UI can group them. Talking-point dedupe
  is lexical **and** semantic via the shared embedder.
- **Write actions, safely.** The HUD now takes a per-session token (validated on
  every route, plus a Host check) before exposing `POST /ask` (manual question /
  expand) and `POST /pause`. The UI gained an Ask box, pause toggle, copy/pin on
  points and answers, and the STT lag pill.
- **Crash-safe outputs + wrap-up.** `derived/` is flushed atomically every
  `hud.persist_seconds` (default 20), and on stop the engine produces
  `live_summary.md` (summary, action items, follow-up email); `--no-live-summary`
  opts out. The budget governor now trues its reservation up to actual usage.

## Phase 2.3: talking-point grounding (hallucination fix)

Real-use feedback: with sparse/quirky audio the cheap rolling model invented a
whole product feature set ("syncs across devices", "Trello/Asana integration")
from a single sentence. Fixes:

- **Strict prompt.** The rolling task is now "a passive note-taker" that may use
  *only* what was said, must not use outside knowledge, must supply a verbatim
  quote per point, and is told that an **empty list is the correct answer** for
  casual/fragmentary audio. Capped at `talking_points_max` (default 3);
  temperature 0.0.
- **Local quote verification.** Response schema is
  `{"bullets":[{"text","quote"}]}`; `grounded_in()` checks the quote (normalised
  span, else token overlap ≥ `talking_points_quote_overlap`, default 0.7)
  against the transcript window and drops unsupported points. No extra API call.
- **Substance gate.** A refresh only fires once `talking_points_min_new_words`
  (60) of *new* speech have arrived and the window has
  `talking_points_min_words` (40) words, so idle/noisy stretches add nothing.
- **Lighter Q&A rule.** Answers may use general knowledge but must cite notes and
  not invent specific numbers/names/integrations.
- All exposed in the settings GUI and covered by `TalkingPointGroundingTests`.

## Phase 2.4: silence / noise hallucination fix

Real-use feedback: after the mic test the last transcript entries were silence
and an air conditioner, yet Whisper emitted repetitive filler ("we can see that
we can see that…"). Causes: a fixed peak-energy gate let steady hum through as
"speech", the remote call requested no segment confidences, the transcript fed
the hallucination back as the next Whisper prompt, and there was no text-level
filter. Fixes:

- **`hud/vad.py`**: per-source adaptive noise floor with a short ambient
  calibration that uses the **quietest** startup frame as the floor (so it can
  never calibrate above the talker) and **peak** rather than RMS level (so
  quiet call/loopback audio still passes). `webrtcvad` is used when importable,
  else the stdlib energy VAD (`stt.vad_backend`, `stt.vad_margin_db`,
  `stt.silence_db`, `stt.adaptive_vad`).
- **Chunker** now classifies frames through the VAD and carries a per-chunk
  margin so marginal chunks can be judged more strictly.
- **Segment confidence gating**: remote STT requests `verbose_json` and drops
  segments over `stt.no_speech_prob_max` / under `stt.avg_logprob_min` / over
  `stt.compression_ratio_max`, falling back to plain `json` if a provider
  rejects the format.
- **Text hallucination filter** (`looks_hallucinated`): canned silence phrases,
  bare interjections on marginal chunks, and repetition loops (low unique-word
  ratio, or a 3-gram repeated ≥3×).
- **Prompt hygiene**: only accepted text updates the context tail, and the
  prompt is trimmed back to the last sentence boundary; `stt.context_prompt`
  disables it entirely.
- Covered by `VADTests` / `HallucinationTests`, including a hum-vs-speech
  Chunker integration test.

## Phase 2.5: source detection / macOS audio routing

Real-use feedback: with headphones connected, the HUD read YouTube/room audio
as the user and missed the remote party. Root cause: macOS system audio is only
capturable through a loopback that mirrors the default output, and the previous
logic ranked `ZoomAudioDevice` first (it only carries Zoom's own shared audio)
and accepted any loopback that merely *opened*, even a silent one.

- **`hud/devices.py`**: parse `system_profiler SPAudioDataType -json` (stdlib)
  for transport, channels, and the **default input/output** flags; classify
  each device as real mic / loopback / aggregate; expose `system_advice()`.
- **Selection**: BlackHole → Loopback app → Soundflower → Multi-Output →
  `ZoomAudioDevice` (demoted). Mics prefer the system default and exclude
  virtual devices via topology, not just names.
- **Honest system capture**: a currently-silent loopback is still selected (so
  audio that starts a moment later isn't missed) but it is logged/flagged; if
  there is no loopback in the output path at all, the recorder records
  mic-only, prints the exact fix, and the HUD shows a warning banner rather
  than mislabelling room audio as the remote party.
- **Route changes**: `monitor()` re-reads the default input/output and
  re-resolves the mic/system when headphones or Bluetooth change the route.
- **HUD follows the recorder**: `Recorder.on_restart` → `LiveSession.update_devices`
  → `LiveTranscriber.update_devices` restarts only the changed STT source. The
  HUD header shows `mic … · sys …` and a warning banner when the other party
  cannot be captured.
- **Diagnostics**: `--list` annotates transport/defaults and prints the fix;
  new `--check-routing` plays a tone and verifies it reaches a loopback.
- **Automated routing fix (`v1.6-routing-fix`)**: creating the Multi-Output
  Device and switching the default output are both public CoreAudio APIs, so
  `hud/routing_fix.py` does it directly via ctypes
  (`AudioHardwareCreateAggregateDevice` with `stacked=1`, then
  `kAudioHardwarePropertyDefaultOutputDevice`) — no sudo, no GUI scripting.
  `--fix-routing` runs it (and re-runs with `--fix-output NAME` to re-track
  headphones/speakers); the menu-bar app gets an `Audio Out ▸` dropdown that
  pairs the multi-output with any present real output device in one click
  (safe mid-call: the recorder only captures BlackHole, which never changes).
  It also detects the inverse misroute (a bare loopback as default output, so
  audio is captured but inaudible) and, when macOS refuses, falls back to a
  click-by-click Audio MIDI Setup walkthrough. The multi-output's subdevice
  list is not readable for stacked aggregates, so the pairing is persisted to
  `~/.zoom_recorder_routing.json` instead and drives reuse-vs-rebuild:
  anything stale (missing device, changed pairing) is destroyed and recreated.
- **Tap capture (`v1.7-system-tap`)**: the loopback approach costs the user
  normal volume control (Multi-Output Devices have none). macOS 14.2+ adds
  process taps: `hud/system_tap.py` creates a private, observe-only global tap
  (PyObjC `CATapDescription` -> `AudioHardwareCreateProcessTap`), wraps it in a
  private aggregate (tap list entry with drift compensation), and pulls float32
  PCM through an ObjC block IOProc (raw ctypes block literal) into a pipe that
  ffmpeg reads as `-f f32le -i pipe:N`. The tap sees pre-volume audio, so
  recording level is independent of the volume slider, and it follows default
  output changes automatically — no `Audio Out` dropdown needed in tap mode.
  `--system-capture auto|tap|loopback` selects the path (auto = tap when
  macOS >= 14.2, loopback otherwise); the tap keeps running across ffmpeg
  restarts and a stall watchdog warns if callbacks stop. First capture
  triggers the one-time System Audio Recording TCC prompt; denial shows up as
  flowing-but-silent buffers, which `--self-test` detects with guidance.
  `--restore-routing` undoes all routing changes (real default output/input,
  destroy the tool-owned multi-output).
- **Tap/mic ordering + wedge recovery (`v1.7` follow-up)**: starting tap IO
  while an avfoundation mic open is in flight can wedge the open (live ffmpeg
  writes nothing, and every concurrent mic open hangs), and starting it after
  the mic stream began kills the stream (0.1s merged track). Order is
  therefore: tap IO fully up (0.5s settle) -> open the mic. A startup gate
  waits for the first mic segment and restarts the capture once if ffmpeg is
  wedged; the monitor distinguishes open-failures (wedge -> restart capture)
  from silence (failover), and logs probe error text plus the ffmpeg.log tail.
  The failure path now preserves segments and ffmpeg stderr under
  `.segments/` instead of deleting them, and a merged track under 2s is a
  failed capture, never a 100%-coverage success. The HUD suppresses the
  loopback advice in tap mode.
- **Permission attribution root cause (`v1.7` follow-up 2)**: a tap probe run
  through a temporary LaunchAgent reproduced the menubar failures exactly:
  under the launchd context (`com.apple.python3`) the tap starts and flows but
  delivers only silence -- the missing **System Audio Recording** TCC grant
  for that context (Terminal has it, so CLI tests pass while menu-bar
  recordings wedge the mic open). Launchd agents also lack brew's PATH, which
  is why the recorder is unaffected (its agent sets PATH) but bare probes
  need `EnvironmentVariables`. Handling: the monitor logs tap health each
  cycle, warns with exact guidance (and a notification) when the tap flows
  but stays silent, starts the HUD server before the capture gate (URL ready
  in ~2s), probes the active mic with a short timeout so wedges surface in
  one chunk, and -- if the mic is still wedged after one restart -- falls
  back to loopback capture (routing fix + fresh device scan) for the rest of
  the session. `python3 -m hud.system_tap --open-settings` opens the privacy
  pane.
- **Context-based capture mode (`v1.7` final)**: the AudioCapture grant cannot
  be created for the launchd/`com.apple.python3` context on macOS 15 (pane
  additions do not match the Apple-signed identity; no prompt mechanism
  works), so the recorder detects its context (`TERM_PROGRAM`): tap for
  permission-bearing contexts (Apple Terminal), loopback everywhere else —
  chosen *before* startup so the menubar path never wedges. Loopback startup
  auto-runs the routing fix (idempotent) so the route is always in place, and
  the menu bar gains a **volume slider** (`routing_fix.set_output_volume`)
  because macOS volume keys do nothing for Multi-Output Devices. The menu-bar
  app now runs from `zoom-recorder.app` (bundle identity + audio usage
  descriptions for future macOS releases; **removed in v1.9-hardening** --
  see below).
- **ScreenCaptureKit evaluated and rejected (2026-09-18)**: an SCK audio-only
  helper (Swift, `NSAudioCaptureUsageDescription` embedded, ad-hoc signed,
  bundled in the .app) was built and exhaustively tested: screen samples flow
  in every context, but SCK *audio* never delivered a single sample on macOS
  15.7 -- TCC reports the screen/audio grant as declined for every context
  (Terminal CLI, launchd agent, LaunchServices-launched bundle, manually
  granted pane entries), and ad-hoc signing invalidates the grant on every
  rebuild (cdhash pinning). Beyond the technical wall, the SCK path forces a
  **Screen Recording** grant just to obtain audio: a least-privilege violation
  and a self-signed-certificate/EDR red flag on a managed endpoint. The
  loopback architecture (public CoreAudio HAL, signed notarized BlackHole, no
  TCC) is the deliberately boring, compliant choice; the volume slider covers
  its only real trade-off. No SCK code is shipped (experiment deleted).
- **Volume as a first-class control (`v1.8-volume`)**: the Multi-Output Device
  has no volume at all, so the menu bar gains a top-level **Volume** item whose
  title always shows the level (`Volume 44%` / `Volume (muted)`), with a
  slider, `Volume +5%` / `Volume −5%`, `Mute`/`Unmute` and 25/50/75/100%
  presets. rumps 0.4.0's `MenuItem.menu = [...]` never attaches an NSMenu (the
  parent renders greyed out with no children) and cannot carry a slider, so
  submenus are built directly with `setSubmenu_`/`addItem_` (`populate_submenu`).
  Volume actions target the *audible* device: the real output inside the
  Multi-Output Device in loopback mode, otherwise whatever is the current
  default output -- so the menu and the remapped keys behave like the system
  volume in both modes. The slider/label/title update live (debounced drags,
  poll sync with a drag guard), fixing the frozen percentage. The recorder
  now hands the default output back to the real device when it stops
  (`deactivate_loopback`, keeping the device and pairing so the next recording
  re-selects it instantly), so native volume keys work between calls. Mute
  silences the speakers/headphones only; BlackHole and the tap are unaffected,
  so recordings continue. `karabiner/zoom-recorder-volume.json` maps the
  hardware volume keys to the CLI actions (no Accessibility TCC -- consistent
  with the Falcon/least-privilege decision).
- Covered by `DevicesTests` (classification, output-path detection, advice,
  system_profiler parsing, HUD device updates), `RoutingFixTests` (output
  choice, rebuild decision matrix, state roundtrip, fake-backend fix flows),
  `AudioMenuTests` (dropdown specs, submenu attachment), `VolumeControlTests`
  (volume math, labels, target selection) and `SystemTapTests` (mode
  resolution, capture command assembly, stall watchdog, tap format parsing,
  feature detection).
- **Hardening for sharing (`v1.9-hardening`)**: an InfoSec alert on the
  original setup traced to EDR-visible patterns, so the tool is now
  deployable to a colleague without re-raising them. Removed the unsigned
  `zoom-recorder.app` (a bash script inside a fake bundle, launched by a
  `RunAtLoad`/`KeepAlive` agent) -- the agent now runs `/usr/bin/python3
  menubar.py` directly and login autostart is opt-in (`install.sh
  --autostart`); the default launch is `run-menubar.command`, so a stock
  install has no persistence. Capture defaults to loopback (microphone is
  the only TCC permission needed); the Core Audio tap is strictly opt-in
  (`--system-capture tap`), keeping the System Audio Recording path out of
  normal operation, and ScreenCaptureKit was already rejected. `--offline`
  is a hard egress kill-switch in the HTTP client (loopback addresses such
  as a local Ollama stay allowed) that also disables answers/KB and forces
  local STT; `--no-notifications` (implied by `--offline`) gates the
  `osascript` notifications; the self-test tone uses a temp dir. New
  `--doctor` (backed by `hud/doctor.py`) checks tools, BlackHole, routing,
  output volume and a real-microphone probe, printing actionable fixes.
  `install.sh`/`uninstall.sh` wrap setup/removal (`--dry-run`, `--install-deps`),
  and `SECURITY.md` + `INSTALL.md` give reviewers and colleagues the full
  access/write/network inventory.
- **Non-technical UI (`v2.0-ui`)**: designed so a colleague with no technical
  skill can operate it without a terminal. A local **Control Center**
  (`hud/control.py`, loopback + per-run token, same security model as the
  settings GUI) with four tabs: **Setup** (wraps `hud/doctor.py` with
  one-click fixes, a live microphone test, an output picker, a ten-second test
  recording that is played back and verified, and transcription setup for
  both local whisper.cpp and online providers), **Recordings**
  (`hud/recordings.py` lists sessions with transcript/summary/play/Finder
  actions), **Settings** (plain-language Basics plus a button to the existing
  advanced form), and **Help** (FAQ + `QUICKSTART.md`). The menu bar is
  rewritten in plain language with a top-level live **Volume**, a recording
  timer, and items that open the Control Center; the transcript window gains a
  **Stop recording** button (`HudServer.on_stop` -> recorder stop event) and a
  **Details** toggle hiding the technical pills. Recording modes are now a
  first-class setting (`both`/`mic`/`system`, `--system-only`), the recorder
  reads a new `recorder` config section (basedir, mic, mode, notifications,
  offline, transcription model) so the GUI can control it, and
  `install.sh`/`install-launch-agent.sh` gained whisper.cpp + model setup and
  a `--disable` login toggle.
- **Control Center fixes (`v2.0.1`)**: the Settings autostart toggle called
  the full installer, whose `launchctl bootout` killed the menu bar -- and the
  Control Center with it (it is a child of that job) -- before the re-bootstrap
  ran, leaving the app dead and the browser request unsettled ("Updating…"
  forever). `install-launch-agent.sh` now has non-destructive
  `--enable-autostart` (writes the plist, enables it, bootstraps only when
  nothing is loaded) and `--disable-autostart` (removes the plist, disables,
  never bootout), which the Control Center uses; the helper runs detached
  (`start_new_session`) with a 20s timeout. The page's `api()` no longer
  throws: it has a timeout and always resolves, a `guarded()` wrapper
  guarantees interim "Saving…/Updating…" messages are replaced (with
  "lost connection — reopen from 🎙" on failure), the header pill reflects
  disconnection instead of staying stale, the Settings autostart has its own
  status element, and the server sends `Connection: close` to avoid
  hand-rolled keep-alive edge cases.
